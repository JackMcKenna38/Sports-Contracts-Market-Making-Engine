"""Replay accuracy: books rebuilt from recorded WebSocket data against REST checkpoints.

Each token's book is rebuilt from its recorded `book` messages and `price_change` deltas only
and compared, level by level, with every REST checkpoint at the checkpoint's exchange timestamp.

Live data shows that a REST book can already contain changes the stream stamps a few
milliseconds later (a maker replacing several levels in one burst), so a checkpoint that does
not match immediately stays pending while later deltas arrive and passes if the rebuilt book
reaches the same levels within `analytics.replay_skew_ms`. A checkpoint that never matches is
a mismatch: the stream lost or never sent a change. The rebuilt book is then reset from that
checkpoint, as the recorder does, so one lost change is counted once.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, select

from mm.recorder.book import LocalBook
from mm.recorder.market_ws import LOST_UPDATE
from mm.recorder.writer import parse_levels
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import BookDelta, BookSide, BookSnapshot

_WS_BOOK, _DELTA, _REST = 0, 1, 2
_Item = tuple[int, int, BookSnapshot | BookDelta]


@dataclass(frozen=True, slots=True)
class Mismatch:
    token_id: str
    ts: int
    recv: int
    hash: str | None


@dataclass(slots=True)
class ReplayAccuracy:
    checkpoints: int = 0
    matched: int = 0
    matched_after_skew: int = 0
    no_stream_book: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def verified(self) -> int:
        return self.matched + self.matched_after_skew + len(self.mismatches)

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def unlogged(self, gaps: Iterable[Mapping[Any, Any]]) -> list[Mismatch]:
        """Mismatches the recorder did not itself detect and log as a lost update."""
        logged = {(g["token_id"], g["started_ts"]) for g in gaps if g["reason"] == LOST_UPDATE}
        return [m for m in self.mismatches if (m.token_id, m.recv) not in logged]


def _snapshots(store: Store, start: int, end: int, source: str) -> Iterator[_Item]:
    """Snapshots in exchange-time order. REST rows are selected by when they were received."""
    b = s.book_snapshots.c
    window = b.ts_recv if source == "rest" else b.ts_exchange
    stmt = (
        select(s.book_snapshots)
        .where(and_(window >= start, window <= end, b.source == source))
        .order_by(b.ts_exchange, b.id)
    )
    kind = _REST if source == "rest" else _WS_BOOK
    with store.engine.connect() as conn:
        for chunk in conn.execution_options(stream_results=True).execute(stmt).partitions(2000):
            for r in chunk:
                yield (
                    r.ts_exchange,
                    kind,
                    BookSnapshot(
                        r.ts_exchange,
                        r.ts_recv,
                        r.token_id,
                        parse_levels(r.bids_json),
                        parse_levels(r.asks_json),
                        r.hash,
                        "rest" if source == "rest" else "ws",
                    ),
                )


def _deltas(store: Store, end: int) -> Iterator[_Item]:
    for r in store.iter_deltas(0, end):
        side: BookSide = "BUY" if r["side"] == "BUY" else "SELL"
        yield (
            r["ts_exchange"],
            _DELTA,
            BookDelta(
                r["ts_exchange"],
                r["ts_recv"],
                r["token_id"],
                side,
                r["price"],
                r["size"],
                r["hash"],
            ),
        )


def check_replay(store: Store, start: int, end: int, skew_ms: int) -> ReplayAccuracy:
    """Check every REST checkpoint received in ``[start, end]``."""
    result = ReplayAccuracy()
    books: dict[str, LocalBook] = {}
    pending: dict[str, list[BookSnapshot]] = defaultdict(list)

    def settle(token: str, now: int | None) -> None:
        """Resolve pending checkpoints for a token; ``now=None`` means the stream has ended."""
        book = books[token]
        waiting = pending.get(token)
        if not waiting:
            return
        still: list[BookSnapshot] = []
        for snap in waiting:
            if book.same_levels(snap):
                result.matched_after_skew += 1
            elif now is None or now > snap.ts + skew_ms:
                result.mismatches.append(Mismatch(token, snap.ts, snap.recv, snap.hash))
                book.reset()
                book.apply_snapshot(snap)
            else:
                still.append(snap)
        pending[token] = still

    sources: list[Iterator[_Item]] = [
        _snapshots(store, 0, end, "ws"),
        _deltas(store, end),
        _snapshots(store, start, end, "rest"),
    ]
    for ts, kind, event in heapq.merge(*sources, key=lambda item: (item[0], item[1])):
        token = event.token_id
        book = books.setdefault(token, LocalBook(token))
        if kind == _REST:
            assert isinstance(event, BookSnapshot)
            result.checkpoints += 1
            if not book.ready:
                result.no_stream_book += 1
            elif not pending[token] and book.same_levels(event):
                result.matched += 1
            else:
                pending[token].append(event)
            continue
        if pending.get(token) and ts > pending[token][0].ts + skew_ms:
            settle(token, ts)
        if isinstance(event, BookDelta):
            book.apply_delta(event)
        else:
            book.apply_snapshot(event)
        settle(token, ts)
    for token in list(pending):
        settle(token, None)
    return result

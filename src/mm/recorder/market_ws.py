from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from mm.config import Config
from mm.recorder.book import LocalBook
from mm.recorder.messages import (
    MARKET_MSG,
    BookMsg,
    LastTradeMsg,
    PriceChangeMsg,
    ResolvedMsg,
    TickSizeMsg,
    market_subscribe,
)
from mm.recorder.writer import Writer, market_event_row
from mm.recorder.ws import Connector, GapLog, WsConn, cancel, default_connector, recv_text
from mm.recorder.ws import run_with_reconnect as reconnect_loop
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import BookSnapshot, Event, Gap, now_ms

log = logging.getLogger(__name__)

LOST_UPDATE = "lost_update"

Listener = Callable[[Event], None]
SnapshotFn = Callable[[Sequence[str]], Coroutine[Any, Any, int]]


def quarantine_row(recv: int, stream: str, error: str, payload: str) -> dict[str, object]:
    return {"ts_recv": recv, "stream": stream, "error": error[:4000], "payload": payload}


@dataclass
class _Conn:
    index: int
    tokens: set[str] = field(default_factory=set)
    ws: WsConn | None = None
    task: asyncio.Task[None] | None = None


class MarketFeed:
    """CLOB market-channel recorder: books, deltas, trades, tick changes and resolutions."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        writer: Writer,
        *,
        connector: Connector = default_connector,
        listeners: Iterable[Listener] = (),
    ) -> None:
        self._cfg = cfg
        self._rec = cfg.recorder
        self._store = store
        self._writer = writer
        self._connector = connector
        self.listeners = list(listeners)
        self.books: dict[str, LocalBook] = {}
        self.snapshot: SnapshotFn | None = None
        self._conns: list[_Conn] = []
        self._token_gaps: dict[str, tuple[int, str, str]] = {}
        self._suspects: dict[str, BookSnapshot] = {}
        self._skew_ms = cfg.analytics.replay_skew_ms
        self._last_drift_log = 0
        self.mismatches = 0
        self.quarantined = 0
        self.resolved: list[tuple[str, str]] = []

    @property
    def tokens(self) -> list[str]:
        return sorted(t for c in self._conns for t in c.tokens)

    def _subscribed(self, token: str) -> bool:
        return any(token in conn.tokens for conn in self._conns)

    # message handling ----------------------------------------------------------

    def _emit(self, event: Event) -> None:
        for fn in self.listeners:
            fn(event)

    def _book(self, token: str) -> LocalBook:
        book = self.books.get(token)
        if book is None:
            book = self.books[token] = LocalBook(token)
        return book

    def _open_token_gap(self, token: str, ts: int, reason: str, detail: str = "") -> None:
        if token not in self._token_gaps:
            self._token_gaps[token] = (ts, reason, detail)
            self._emit(Gap(ts, "market_ws", token, True, reason))
        self._book(token).reset()

    def _close_token_gap(self, token: str, ts: int) -> None:
        opened = self._token_gaps.pop(token, None)
        if opened is not None:
            start, reason, detail = opened
            self._emit(Gap(ts, "market_ws", token, False, reason))
            self._writer.add(
                s.recorder_gaps,
                {
                    "stream": "market_ws",
                    "token_id": token,
                    "started_ts": start,
                    "ended_ts": ts,
                    "reason": reason,
                    "detail": detail[:2000],
                },
            )

    def flush_open_gaps(self, ts: int) -> None:
        for token in list(self._token_gaps):
            self._close_token_gap(token, ts)

    def _check_drift(self, recv: int, ts: int) -> None:
        drift = recv - ts
        if abs(drift) > self._rec.drift_alert_ms and recv - self._last_drift_log > 60_000:
            self._last_drift_log = recv
            log.warning("receive minus exchange time is %d ms; check NTP on this host", drift)

    def handle_text(self, text: str, recv: int) -> None:
        if text.strip().upper() == "PONG":
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            self._quarantine(recv, f"invalid JSON: {exc}", text)
            return
        for item in payload if isinstance(payload, list) else [payload]:
            self.handle_item(item, recv)

    def _quarantine(self, recv: int, error: str, payload: str) -> None:
        self.quarantined += 1
        self._writer.add(s.quarantine, quarantine_row(recv, "market_ws", error, payload))
        log.error("market_ws message quarantined: %s", error[:300])

    def handle_item(self, item: object, recv: int) -> None:
        try:
            msg = MARKET_MSG.validate_python(item)
        except ValidationError as exc:
            self._quarantine(recv, str(exc), json.dumps(item, default=str))
            token = item.get("asset_id") if isinstance(item, dict) else None
            if isinstance(token, str):
                self._open_token_gap(token, recv, "quarantine")
            return
        if isinstance(msg, BookMsg):
            snap = msg.to_event(recv)
            self._writer.add_snapshot(snap)
            self._book(snap.token_id).apply_snapshot(snap)
            self._close_token_gap(snap.token_id, recv)
            self._emit(snap)
        elif isinstance(msg, PriceChangeMsg):
            self._check_drift(recv, msg.timestamp)
            for delta in msg.to_events(recv):
                suspect = self._suspects.get(delta.token_id)
                if suspect is not None and delta.ts <= suspect.ts + self._skew_ms:
                    del self._suspects[delta.token_id]
                self._writer.add_delta(delta)
                self._book(delta.token_id).apply_delta(delta)
                self._emit(delta)
        elif isinstance(msg, LastTradeMsg):
            trade = msg.to_event(recv)
            self._writer.add_trade(trade)
            self._emit(trade)
        elif isinstance(msg, TickSizeMsg):
            self._writer.add(
                s.market_events,
                market_event_row(
                    recv, msg.timestamp, msg.market, msg.asset_id, msg.event_type, item
                ),
            )
            log.info("tick size %s: %g -> %g", msg.asset_id, msg.old_tick_size, msg.new_tick_size)
            self._emit(msg.to_event())
        elif isinstance(msg, ResolvedMsg):
            if not self._subscribed(msg.winning_asset_id):
                return
            self._writer.add(
                s.market_events,
                market_event_row(recv, msg.timestamp, msg.market, None, msg.event_type, item),
            )
            self.resolved.append((msg.market, msg.winning_asset_id))
            self._emit(msg.to_event())

    def reconcile(self, snap: BookSnapshot) -> None:
        """Check a REST checkpoint against the local book and adopt it when it is not older.

        Equal hashes with different levels is a lost update, logged at once. A newer REST book
        with different levels may only be ahead of deltas still in flight, so it is adopted and
        kept as a suspect: a delta stamped up to the checkpoint's time clears it, and one still
        unexplained at the token's next checkpoint is logged as a lost update.
        """
        book = self._book(snap.token_id)
        unexplained = self._suspects.pop(snap.token_id, None)
        if unexplained is not None:
            self._log_lost_update(unexplained)
        if book.ready and snap.hash is not None and book.hash == snap.hash:
            if not book.same_levels(snap):
                self._log_lost_update(snap)
                book.reset()
                book.apply_snapshot(snap)
                self._emit(snap)
            return
        differs = book.ready and not book.same_levels(snap)
        if book.apply_snapshot(snap):
            if differs:
                self._suspects[snap.token_id] = snap
            self._close_token_gap(snap.token_id, snap.recv)
            self._emit(snap)

    def _log_lost_update(self, snap: BookSnapshot) -> None:
        self.mismatches += 1
        log.warning(
            "lost book update on %s: REST checkpoint %s differs from the stream; reset from REST",
            snap.token_id,
            snap.hash,
        )
        self._writer.add(
            s.recorder_gaps,
            {
                "stream": "market_ws",
                "token_id": snap.token_id,
                "started_ts": snap.recv,
                "ended_ts": snap.recv,
                "reason": LOST_UPDATE,
                "detail": f"hash {snap.hash}",
            },
        )

    # connections ----------------------------------------------------------------

    def set_tokens(self, wanted: Iterable[str]) -> tuple[list[str], list[str]]:
        target = set(wanted)
        current = set(self.tokens)
        added = sorted(target - current)
        removed = sorted(current - target)
        for conn in self._conns:
            gone = conn.tokens & set(removed)
            if gone:
                conn.tokens -= gone
                self._send_soon(conn, market_subscribe(sorted(gone), unsubscribe=True))
        for token in removed:
            self.books.pop(token, None)
        cap = self._rec.max_assets_per_connection
        for token in added:
            free = next((c for c in self._conns if len(c.tokens) < cap), None)
            if free is None:
                free = _Conn(index=len(self._conns))
                self._conns.append(free)
            free.tokens.add(token)
        for conn in self._conns:
            fresh = sorted(conn.tokens & set(added))
            if fresh and conn.ws is not None:
                self._send_soon(conn, market_subscribe(fresh))
                if self.snapshot is not None:
                    asyncio.get_running_loop().create_task(self.snapshot(fresh))
        return added, removed

    def _send_soon(self, conn: _Conn, payload: dict[str, object]) -> None:
        ws = conn.ws
        if ws is not None:
            asyncio.get_running_loop().create_task(ws.send(json.dumps(payload)))

    def start(self) -> None:
        for conn in self._conns:
            if conn.tokens and (conn.task is None or conn.task.done()):
                conn.task = asyncio.get_running_loop().create_task(self._run(conn))

    async def stop(self) -> None:
        for conn in self._conns:
            await cancel(conn.task)
        self.flush_open_gaps(now_ms())

    async def _run(self, conn: _Conn) -> None:
        gaps = GapLog(self._store, "market_ws", self.listeners)
        await reconnect_loop(
            f"market_ws[{conn.index}]",
            lambda: self._session(conn, gaps),
            gaps,
            self._rec.reconnect_base_seconds,
            self._rec.reconnect_max_seconds,
        )

    async def _session(self, conn: _Conn, gaps: GapLog) -> None:
        async with self._connector(self._cfg.endpoints.clob_ws) as ws:
            tokens = sorted(conn.tokens)
            await ws.send(json.dumps(market_subscribe(tokens)))
            conn.ws = ws
            pinger = asyncio.get_running_loop().create_task(self._ping(ws))
            try:
                if self.snapshot is not None:
                    await self.snapshot(tokens)
                await gaps.close()
                while True:
                    text = await recv_text(ws, self._rec.market_ws_idle_timeout_seconds)
                    self.handle_text(text, now_ms())
            finally:
                conn.ws = None
                await cancel(pinger)

    async def _ping(self, ws: WsConn) -> None:
        while True:
            await asyncio.sleep(self._rec.market_ws_ping_seconds)
            await ws.send("PING")

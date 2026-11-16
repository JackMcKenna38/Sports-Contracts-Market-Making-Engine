"""Recorder health and gap-log summary read straight from the database."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select

from mm.config import Config
from mm.storage import schema as s
from mm.storage.store import Store

RECORDED_TABLES = (
    s.markets,
    s.book_snapshots,
    s.book_deltas,
    s.trades,
    s.market_events,
    s.odds_snapshots,
    s.game_status,
    s.quarantine,
)


@dataclass(frozen=True, slots=True)
class GapLine:
    stream: str
    reason: str
    count: int
    seconds: float
    open: int


@dataclass(frozen=True, slots=True)
class Status:
    now: int
    heartbeat: int | None
    heartbeat_limit_ms: int
    data_window: tuple[int, int] | None
    rows: dict[str, int]
    markets_open: int
    mappings_confirmed: int
    mappings_pending: int
    last_odds: int | None
    gaps: list[GapLine] = field(default_factory=list)

    @property
    def heartbeat_age_ms(self) -> int | None:
        return None if self.heartbeat is None else self.now - self.heartbeat

    @property
    def live(self) -> bool:
        """True when a recorder has written its heartbeat recently enough to count as running."""
        age = self.heartbeat_age_ms
        return age is not None and age <= self.heartbeat_limit_ms


def gap_lines(store: Store, start: int, end: int) -> list[GapLine]:
    """Gap log per stream and reason, clipped to ``[start, end]``; open gaps run to ``end``."""
    count: dict[tuple[str, str], int] = defaultdict(int)
    millis: dict[tuple[str, str], int] = defaultdict(int)
    still_open: dict[tuple[str, str], int] = defaultdict(int)
    for gap in store.gaps(start, end):
        key = (gap["stream"], gap["reason"])
        count[key] += 1
        stop = end if gap["ended_ts"] is None else min(end, gap["ended_ts"])
        millis[key] += max(0, stop - max(start, gap["started_ts"]))
        still_open[key] += gap["ended_ts"] is None
    return [
        GapLine(stream, reason, count[(stream, reason)], millis[(stream, reason)] / 1000,
                still_open[(stream, reason)])
        for stream, reason in sorted(count)
    ]  # fmt: skip


def read_status(cfg: Config, store: Store, now: int) -> Status:
    with store.engine.connect() as conn:
        last_odds = conn.execute(select(func.max(s.odds_snapshots.c.ts_recv))).scalar()
        markets_open = conn.execute(
            select(func.count()).select_from(s.markets).where(s.markets.c.closed.is_(False))
        ).scalar_one()
    mappings = store.mappings()
    confirmed = sum(1 for m in mappings if m["confirmed"])
    window = store.data_bounds()
    return Status(
        now=now,
        heartbeat=store.heartbeat(),
        heartbeat_limit_ms=int(cfg.recorder.heartbeat_stale_seconds * 1000),
        data_window=window,
        rows={table.name: store.count(table) for table in RECORDED_TABLES},
        markets_open=int(markets_open),
        mappings_confirmed=confirmed,
        mappings_pending=len(mappings) - confirmed,
        last_odds=None if last_odds is None else int(last_odds),
        gaps=gap_lines(store, 0, now),
    )

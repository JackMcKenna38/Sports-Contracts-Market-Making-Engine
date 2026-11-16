from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any

from sqlalchemy import Table

from mm.config import Config
from mm.storage import schema as s
from mm.storage.store import Row, Store
from mm.types import BookDelta, BookSnapshot, GameStatus, Levels, OddsQuote, Trade

log = logging.getLogger(__name__)


class WriterFailure(RuntimeError):
    """The database rejected writes for too long; the process must exit so it is restarted."""


def levels_json(levels: Levels) -> str:
    return json.dumps([[p, s] for p, s in levels], separators=(",", ":"))


def parse_levels(text: str) -> Levels:
    return tuple((float(p), float(sz)) for p, sz in json.loads(text))


def snapshot_row(e: BookSnapshot) -> Row:
    return {
        "ts_recv": e.recv,
        "ts_exchange": e.ts,
        "token_id": e.token_id,
        "hash": e.hash,
        "bids_json": levels_json(e.bids),
        "asks_json": levels_json(e.asks),
        "source": e.source,
    }


def delta_row(e: BookDelta) -> Row:
    return {
        "ts_recv": e.recv,
        "ts_exchange": e.ts,
        "token_id": e.token_id,
        "side": e.side,
        "price": e.price,
        "size": e.size,
        "hash": e.hash,
    }


def trade_row(e: Trade, source: str) -> Row:
    return {
        "ts": e.ts,
        "ts_recv": e.recv,
        "condition_id": e.condition_id,
        "token_id": e.token_id,
        "side": e.side,
        "price": e.price,
        "size": e.size,
        "tx_hash": e.tx_hash,
        "source": source,
    }


def odds_row(e: OddsQuote, source: str) -> Row:
    return {
        "ts_recv": e.ts,
        "sport": e.sport,
        "event_id": e.event_id,
        "commence_time": e.commence_time,
        "home_team": e.home_team,
        "away_team": e.away_team,
        "bookmaker": e.bookmaker,
        "home_price": e.home_price,
        "away_price": e.away_price,
        "source": source,
    }


def game_row(e: GameStatus) -> Row:
    return {
        "ts_recv": e.ts,
        "game_id": e.game_id,
        "league": e.league,
        "status": e.status,
        "live": e.live,
        "ended": e.ended,
        "score": e.score,
        "period": e.period,
    }


def market_event_row(
    recv: int, ts: int, condition_id: str, token_id: str | None, kind: str, payload: Any
) -> Row:
    return {
        "ts_recv": recv,
        "ts_exchange": ts,
        "condition_id": condition_id,
        "token_id": token_id,
        "event_type": kind,
        "payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
    }


class Writer:
    """Buffered batch writer. Buffers through short outages, then fails loudly."""

    def __init__(self, cfg: Config, store: Store, clock: Any = time.monotonic) -> None:
        self._store = store
        self._cfg = cfg.recorder
        self._clock = clock
        self._buf: dict[Table, list[Row]] = defaultdict(list)
        self._failing_since: float | None = None
        self.written = 0

    def add(self, table: Table, row: Row) -> None:
        self._buf[table].append(row)

    def extend(self, table: Table, rows: list[Row]) -> None:
        self._buf[table].extend(rows)

    def add_snapshot(self, e: BookSnapshot) -> None:
        self.add(s.book_snapshots, snapshot_row(e))

    def add_delta(self, e: BookDelta) -> None:
        self.add(s.book_deltas, delta_row(e))

    def add_trade(self, e: Trade, source: str = "ws") -> None:
        self.add(s.trades, trade_row(e, source))

    def add_odds(self, e: OddsQuote, source: str = "live") -> None:
        self.add(s.odds_snapshots, odds_row(e, source))

    def add_game(self, e: GameStatus) -> None:
        self.add(s.game_status, game_row(e))

    @property
    def pending(self) -> int:
        return sum(len(rows) for rows in self._buf.values())

    def flush(self) -> int:
        if not self.pending:
            return 0
        batch = {table: list(rows) for table, rows in self._buf.items() if rows}
        n = sum(len(rows) for rows in batch.values())
        try:
            self._store.insert_many(batch)
        except Exception as exc:
            now = self._clock()
            if self._failing_since is None:
                self._failing_since = now
            log.error("database write failed (%d rows buffered): %s", self.pending, exc)
            if (
                self.pending > self._cfg.writer_max_buffer_rows
                or now - self._failing_since > self._cfg.writer_max_fail_seconds
            ):
                raise WriterFailure(f"database writes failing: {exc}") from exc
            return 0
        for table, rows in batch.items():
            del self._buf[table][: len(rows)]
        self._failing_since = None
        self.written += n
        return n

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.writer_flush_seconds)
            await asyncio.to_thread(self.flush)

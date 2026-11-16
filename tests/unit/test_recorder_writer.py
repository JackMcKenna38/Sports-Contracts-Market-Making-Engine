from __future__ import annotations

from typing import Any

import pytest

from mm.config import Config
from mm.recorder.writer import Writer, WriterFailure, levels_json, parse_levels
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import BookDelta, BookSnapshot, GameStatus, OddsQuote, Trade


def test_writer_flushes_every_event_kind(cfg: Config, store: Store) -> None:
    w = Writer(cfg, store)
    w.add_snapshot(BookSnapshot(1, 2, "t", ((0.4, 1.0),), (), "h", "ws"))
    w.add_delta(BookDelta(3, 4, "t", "BUY", 0.41, 2.0, "h2"))
    w.add_trade(Trade(5, 6, "c", "t", "SELL", 0.4, 1.0))
    w.add_odds(OddsQuote(7, "basketball_nba", "e", 9, "H", "A", "pinnacle", 1.9, 2.0))
    w.add_game(GameStatus(8, "g", "nba", "Scheduled", False, False, None, None))
    assert w.flush() == 5
    assert w.pending == 0
    for table in (s.book_snapshots, s.book_deltas, s.trades, s.odds_snapshots, s.game_status):
        assert store.count(table) == 1


def test_levels_round_trip() -> None:
    levels = ((0.5, 10.0), (0.49, 2.5))
    assert parse_levels(levels_json(levels)) == levels


class _BrokenStore:
    def insert_many(self, _batch: Any) -> None:
        raise OSError("disk full")


def test_writer_buffers_then_fails_loudly(cfg: Config) -> None:
    now = [0.0]
    w = Writer(cfg, _BrokenStore(), clock=lambda: now[0])  # type: ignore[arg-type]
    w.add_delta(BookDelta(1, 1, "t", "BUY", 0.4, 1.0, None))
    assert w.flush() == 0
    assert w.pending == 1
    now[0] = cfg.recorder.writer_max_fail_seconds + 1
    with pytest.raises(WriterFailure):
        w.flush()

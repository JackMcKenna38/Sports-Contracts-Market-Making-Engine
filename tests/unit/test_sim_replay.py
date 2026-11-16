from __future__ import annotations

import pytest

from mm.config import Config
from mm.discovery.gamma import market_row
from mm.discovery.models import GammaMarket
from mm.recorder.writer import Writer, market_event_row
from mm.sim.replay import NoLatencyData, measured_latency, replay
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import (
    BookDelta,
    BookSnapshot,
    GameStatus,
    Gap,
    MappingChange,
    MarketResolved,
    MetaChange,
    OddsQuote,
    Tick,
    TickSizeChange,
    Trade,
)
from tests.conftest import fixture_json


def _seed(cfg: Config, store: Store) -> None:
    raw = fixture_json("gamma_markets_synthetic.json")[0]
    row = market_row(GammaMarket.model_validate(raw), "nba")
    store.upsert_market(row, 0)
    store.upsert_market({**row, "tick_size": 0.01}, 1500)
    store.upsert_mapping(
        {
            "condition_id": row["condition_id"],
            "odds_event_id": "ev1",
            "match_method": "teams",
            "confidence": 1.0,
            "confirmed": True,
            "yes_is_home": False,
            "updated_ts": 0,
        }
    )
    w = Writer(cfg, store)
    w.add_snapshot(BookSnapshot(1000, 1040, "111", ((0.48, 100.0),), ((0.52, 80.0),), "h0", "ws"))
    w.add_snapshot(BookSnapshot(990, 1100, "111", ((0.48, 90.0),), ((0.52, 80.0),), "r", "rest"))
    w.add_delta(BookDelta(1050, 1090, "111", "BUY", 0.49, 30.0, "h1"))
    w.add_trade(Trade(1200, 1230, row["condition_id"], "111", "SELL", 0.49, 5.0))
    w.add_trade(Trade(1201, 1231, row["condition_id"], "111", "SELL", 0.49, 5.0), "data_api")
    w.add_odds(
        OddsQuote(1300, "basketball_nba", "ev1", 9000, "Owls", "Hawks", "pinnacle", 1.8, 2.1)
    )
    w.add_game(GameStatus(1400, "g-1", "nba", "Scheduled", False, False, None, None))
    w.add(
        s.market_events,
        market_event_row(
            1600, 1600, row["condition_id"], "111", "tick_size_change",
            {"new_tick_size": "0.001"},
        ),
    )  # fmt: skip
    w.add(
        s.market_events,
        market_event_row(
            1900, 1900, row["condition_id"], None, "market_resolved",
            {"winning_asset_id": "111"},
        ),
    )  # fmt: skip
    w.flush()
    store.log_gap("market_ws", None, 1700, 1800, "error")


def test_replay_merges_every_table_in_time_order(cfg: Config, store: Store) -> None:
    _seed(cfg, store)
    events = list(replay(store, 1000, 2000, tick_ms=500))
    kinds = [
        (type(e).__name__, e.ts if not isinstance(e, BookSnapshot) else e.recv) for e in events
    ]
    assert kinds == [
        ("MetaChange", 1000),
        ("MappingChange", 1000),
        ("BookSnapshot", 1040),
        ("Tick", 1000),
        ("BookDelta", 1050),
        ("BookSnapshot", 1100),
        ("Trade", 1200),
        ("OddsQuote", 1300),
        ("GameStatus", 1400),
        ("MetaChange", 1500),
        ("Tick", 1500),
        ("TickSizeChange", 1600),
        ("Gap", 1700),
        ("Gap", 1800),
        ("MarketResolved", 1900),
        ("Tick", 2000),
    ]
    first, meta_later = events[0], events[9]
    assert isinstance(first, MetaChange) and first.meta.tick_size == 0.001
    assert isinstance(meta_later, MetaChange) and meta_later.meta.tick_size == 0.01
    mapping = events[1]
    assert isinstance(mapping, MappingChange) and mapping.yes_is_home is False
    gap_open, gap_close = events[12], events[13]
    assert isinstance(gap_open, Gap) and gap_open.open
    assert isinstance(gap_close, Gap) and not gap_close.open
    tick = events[11]
    assert isinstance(tick, TickSizeChange) and tick.new_tick_size == 0.001
    resolved = events[14]
    assert isinstance(resolved, MarketResolved) and resolved.winning_token_id == "111"
    assert isinstance(events[-1], Tick)


def test_replay_is_deterministic(cfg: Config, store: Store) -> None:
    _seed(cfg, store)
    assert list(replay(store, 0, 3000, 250)) == list(replay(store, 0, 3000, 250))


def test_open_gap_closes_at_window_end(cfg: Config, store: Store) -> None:
    store.open_gap("sports_ws", None, 100, "idle_timeout")
    gaps = [e for e in replay(store, 0, 500, 1000) if isinstance(e, Gap)]
    assert [(g.ts, g.open) for g in gaps] == [(100, True), (500, False)]


def test_latency_is_median_of_recorded_gaps(cfg: Config, store: Store) -> None:
    with pytest.raises(NoLatencyData):
        measured_latency(store, 0, 10_000)
    _seed(cfg, store)
    assert measured_latency(store, 0, 10_000) == 40


def test_bad_window_rejected(store: Store) -> None:
    with pytest.raises(ValueError):
        list(replay(store, 10, 5, 1))

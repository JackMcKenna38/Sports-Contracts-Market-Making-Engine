from __future__ import annotations

from typing import Any

from mm.config import Risk, load_config
from mm.risk.engine import (
    DAILY_LOSS,
    EVENT_EXPOSURE,
    LOSS_THROTTLE,
    PORTFOLIO,
    STALE_BOOK,
    STALE_GAME_FEED,
    STALE_ODDS,
    RiskEngine,
    RiskEvent,
)

CID = "0xcond_synthetic_a"
DAY = 86_400_000
T0 = 20_000 * DAY + 3_600_000
ODDS_AGE_MS = 420 * 60_000


def _engine(**overrides: Any) -> tuple[RiskEngine, list[RiskEvent]]:
    cfg = Risk.model_validate({**load_config().risk.model_dump(), **overrides})
    events: list[RiskEvent] = []
    return RiskEngine(cfg, ODDS_AGE_MS, events.append), events


def _fresh(engine: RiskEngine, ts: int, **kwargs: Any) -> Any:
    args: dict[str, Any] = {
        "net_position": 0.0,
        "exposure": 0.0,
        "book_ts": ts,
        "odds_ts": ts,
        "feed_down_since": None,
    }
    return engine.check(ts, CID, **{**args, **kwargs})


def test_nothing_binds_on_fresh_data() -> None:
    engine, events = _engine()
    decision = _fresh(engine, T0)
    assert decision.quote and decision.allow_bid and decision.allow_ask
    assert decision.size_multiplier == 1.0
    assert decision.reasons == ()
    assert events == []


def test_daily_loss_cap_halts_until_the_next_utc_day() -> None:
    engine, events = _engine(daily_loss_limit=250)
    assert not engine.update_equity(T0, 10_000)
    assert not engine.update_equity(T0 + 1000, 9_750.01)
    assert _fresh(engine, T0 + 1000).quote
    assert engine.update_equity(T0 + 2000, 9_750)
    assert engine.update_equity(T0 + 3000, 9_990), "a recovery does not lift the halt"
    decision = _fresh(engine, T0 + 3000)
    assert not decision.quote and decision.reasons == (DAILY_LOSS,)
    assert [(e.condition_id, e.rule, e.value, e.limit, e.action) for e in events] == [
        (PORTFOLIO, DAILY_LOSS, -250, -250, "halt_day")
    ]
    tomorrow = T0 + DAY
    assert not engine.update_equity(tomorrow, 9_990)
    assert _fresh(engine, tomorrow).quote
    assert engine.update_equity(tomorrow + 1, 9_700)
    assert len(events) == 2


def test_exposure_limit_blocks_only_the_side_that_adds_risk() -> None:
    engine, events = _engine(max_event_exposure=400)
    assert _fresh(engine, T0, net_position=700, exposure=399.99).allow_bid
    long = _fresh(engine, T0 + 1, net_position=800, exposure=400)
    assert (long.allow_bid, long.allow_ask) == (False, True)
    assert long.reasons == (EVENT_EXPOSURE,)
    _fresh(engine, T0 + 2, net_position=800, exposure=410)
    assert len(events) == 1, "one event per breach, not per tick"
    assert (events[0].value, events[0].limit, events[0].action) == (400, 400, "block_increase")
    assert _fresh(engine, T0 + 3, net_position=100, exposure=50).allow_bid
    short = _fresh(engine, T0 + 4, net_position=-900, exposure=450)
    assert (short.allow_bid, short.allow_ask) == (True, False)
    assert len(events) == 2


def test_loss_throttle_cuts_size_then_stops_the_event() -> None:
    engine, events = _engine(loss_size_cut_fraction=0.5, max_losses_per_event=3)
    cash = 0.0
    for trip, result in enumerate([-5.0, 2.0, -1.0, -0.5]):
        ts = T0 + trip * 10
        engine.observe_position(ts, CID, flat=False, cash_flow=cash - 40)
        engine.observe_position(ts + 1, CID, flat=False, cash_flow=cash - 80)
        cash += result
        engine.observe_position(ts + 2, CID, flat=True, cash_flow=cash)
    assert engine.losses(CID) == 3
    assert [e.action for e in events] == ["cut_size", "cut_size", "stop_event"]
    assert [e.value for e in events] == [1, 2, 3]
    assert engine.size_multiplier(CID) == 0.0
    decision = _fresh(engine, T0 + 100)
    assert not decision.quote and decision.reasons == (LOSS_THROTTLE,)
    assert _fresh_other(engine, T0 + 100).quote


def _fresh_other(engine: RiskEngine, ts: int) -> Any:
    return engine.check(
        ts, "0xother", net_position=0, exposure=0, book_ts=ts, odds_ts=ts, feed_down_since=None
    )


def test_loss_throttle_multiplier_after_one_and_two_losses() -> None:
    engine, _ = _engine(loss_size_cut_fraction=0.5, max_losses_per_event=3)
    engine.observe_position(T0, CID, flat=False, cash_flow=-10)
    engine.observe_position(T0 + 1, CID, flat=True, cash_flow=-1)
    assert engine.size_multiplier(CID) == 0.5
    assert _fresh(engine, T0 + 2).size_multiplier == 0.5
    engine.observe_position(T0 + 3, CID, flat=False, cash_flow=-20)
    engine.observe_position(T0 + 4, CID, flat=True, cash_flow=-1.5)
    assert engine.size_multiplier(CID) == 0.25


def test_winning_or_breakeven_round_trip_is_not_a_loss() -> None:
    engine, events = _engine()
    engine.observe_position(T0, CID, flat=True, cash_flow=0)
    engine.observe_position(T0 + 1, CID, flat=False, cash_flow=-48)
    engine.observe_position(T0 + 2, CID, flat=True, cash_flow=0)
    engine.observe_position(T0 + 3, CID, flat=False, cash_flow=-48)
    engine.observe_position(T0 + 4, CID, flat=True, cash_flow=4)
    assert engine.losses(CID) == 0 and events == []


def test_stale_book_pulls_quotes_and_clears() -> None:
    engine, events = _engine(stale_book_seconds=180)
    assert _fresh(engine, T0, book_ts=T0 - 180_000).quote
    stale = _fresh(engine, T0, book_ts=T0 - 180_001)
    assert not stale.quote and stale.reasons == (STALE_BOOK,)
    _fresh(engine, T0 + 1, book_ts=T0 - 180_001)
    assert len(events) == 1 and events[0].action == "pull_quotes"
    assert _fresh(engine, T0 + 2).quote
    never = _fresh(engine, T0 + 3, book_ts=None)
    assert not never.quote
    assert len(events) == 2 and events[1].value == float("inf")


def test_stale_odds_pulls_quotes() -> None:
    engine, events = _engine()
    assert _fresh(engine, T0, odds_ts=T0 - ODDS_AGE_MS).quote
    stale = _fresh(engine, T0, odds_ts=T0 - ODDS_AGE_MS - 1)
    assert not stale.quote and stale.reasons == (STALE_ODDS,)
    assert not _fresh(engine, T0, odds_ts=None).quote
    assert [e.rule for e in events] == [STALE_ODDS]


def test_game_feed_outage_pulls_every_market_once_it_outlasts_the_limit() -> None:
    engine, events = _engine(stale_game_feed_seconds=60)
    assert _fresh(engine, T0, feed_down_since=T0 - 60_000).quote
    down = _fresh(engine, T0, feed_down_since=T0 - 60_001)
    assert not down.quote and down.reasons == (STALE_GAME_FEED,)
    other = engine.check(
        T0,
        "0xother",
        net_position=0,
        exposure=0,
        book_ts=T0,
        odds_ts=T0,
        feed_down_since=T0 - 60_001,
    )
    assert not other.quote
    assert [(e.condition_id, e.rule, e.action) for e in events] == [
        (PORTFOLIO, STALE_GAME_FEED, "pull_all")
    ]
    assert _fresh(engine, T0 + 10).quote
    assert engine.feed_stale(T0 + 100_000, T0 + 20)
    assert len(events) == 2


def test_several_rules_can_bind_at_once() -> None:
    engine, events = _engine(max_event_exposure=400)
    decision = _fresh(engine, T0, book_ts=None, odds_ts=None, net_position=900, exposure=500)
    assert not decision.quote
    assert decision.reasons == (STALE_BOOK, STALE_ODDS, EVENT_EXPOSURE)
    assert {e.rule for e in events} == {STALE_BOOK, STALE_ODDS, EVENT_EXPOSURE}

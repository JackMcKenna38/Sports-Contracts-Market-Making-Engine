from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mm.config import Config, Quoting, load_config
from mm.strategy.quoter import Quoter, on_tick, round_down, round_up

BASE = load_config()


def _quoter(**overrides: float) -> Quoter:
    return Quoter(Quoting.model_validate({**BASE.quoting.model_dump(), **overrides}))


def test_rounding_handles_binary_float_noise() -> None:
    assert round_down(0.58, 0.01) == 0.58
    assert round_up(0.58, 0.01) == 0.58
    assert round_down(0.5849, 0.001) == 0.584
    assert round_up(0.5841, 0.001) == 0.585
    assert round_down(0.1 + 0.2, 0.1) == 0.3
    assert on_tick(0.584, 0.001) and not on_tick(0.5845, 0.001)


def test_baseline_quotes_fixed_distance_either_side_of_fair() -> None:
    quotes = _quoter(half_spread=0.02, quote_size=100).quotes(
        fair=0.5, net_position=0, tick=0.01, min_size=5
    )
    assert quotes.bid is not None and quotes.ask is not None
    assert (quotes.bid.side, quotes.bid.price, quotes.bid.size) == ("BUY", 0.48, 100)
    assert (quotes.ask.side, quotes.ask.price, quotes.ask.size) == ("SELL", 0.52, 100)
    assert quotes.fair == 0.5


def test_off_tick_fair_rounds_away_from_fair() -> None:
    quotes = _quoter(half_spread=0.02).quotes(fair=0.5037, net_position=0, tick=0.01, min_size=5)
    assert quotes.bid is not None and quotes.ask is not None
    assert (quotes.bid.price, quotes.ask.price) == (0.48, 0.53)


def test_inventory_skew_leans_both_quotes_towards_flat() -> None:
    quoter = _quoter(half_spread=0.02, skew_strength=0.02, max_position_shares=500)
    long = quoter.quotes(fair=0.5, net_position=250, tick=0.001, min_size=5)
    short = quoter.quotes(fair=0.5, net_position=-250, tick=0.001, min_size=5)
    assert long.bid is not None and long.ask is not None
    assert short.bid is not None and short.ask is not None
    assert (long.bid.price, long.ask.price) == (0.47, 0.51)
    assert (short.bid.price, short.ask.price) == (0.49, 0.53)
    flat = _quoter(skew_strength=0.0).quotes(fair=0.5, net_position=250, tick=0.001, min_size=5)
    assert flat.bid is not None and flat.bid.price == 0.48


def test_position_cap_limits_size_and_removes_a_side() -> None:
    quoter = _quoter(quote_size=100, max_position_shares=500)
    near = quoter.quotes(fair=0.5, net_position=460, tick=0.01, min_size=5)
    assert near.bid is not None and near.bid.size == 40
    assert near.ask is not None and near.ask.size == 100
    full = quoter.quotes(fair=0.5, net_position=500, tick=0.01, min_size=5)
    assert full.bid is None and full.ask is not None
    dust = quoter.quotes(fair=0.5, net_position=-497, tick=0.01, min_size=5)
    assert dust.ask is None and dust.bid is not None


def test_quotes_stay_passive_against_the_book() -> None:
    quotes = _quoter(half_spread=0.02).quotes(
        fair=0.5, net_position=0, tick=0.01, min_size=5, best_bid=0.53, best_ask=0.47
    )
    assert quotes.bid is not None and quotes.ask is not None
    assert quotes.bid.price == 0.46
    assert quotes.ask.price == 0.54


def test_quotes_never_leave_price_bounds() -> None:
    quoter = _quoter(half_spread=0.02)
    low = quoter.quotes(fair=0.02, net_position=0, tick=0.01, min_size=5)
    assert low.bid is None
    assert low.ask is not None and low.ask.price == 0.04
    high = quoter.quotes(fair=0.985, net_position=0, tick=0.01, min_size=5)
    assert high.ask is None
    assert high.bid is not None and high.bid.price == 0.96


def test_risk_flags_and_size_multiplier() -> None:
    quoter = _quoter(quote_size=100)
    quotes = quoter.quotes(
        fair=0.5, net_position=0, tick=0.01, min_size=5, size_multiplier=0.5, allow_ask=False
    )
    assert quotes.ask is None
    assert quotes.bid is not None and quotes.bid.size == 50
    assert [q.side for q in quotes] == ["BUY"]
    tiny = quoter.quotes(fair=0.5, net_position=0, tick=0.01, min_size=5, size_multiplier=0.01)
    assert tiny.bid is None and tiny.ask is None


def test_requote_and_cancel_thresholds() -> None:
    quoter = _quoter(requote_threshold=0.005, cancel_threshold=0.02)
    assert not quoter.should_requote(0.503, 0.5)
    assert quoter.should_requote(0.505, 0.5)
    assert not quoter.should_cancel(0.515, 0.5)
    assert quoter.should_cancel(0.48, 0.5)


def test_quoting_stops_before_game_start() -> None:
    quoter = _quoter(stop_minutes_before_start=10)
    start = 1_794_875_400_000
    assert quoter.stop_time(start) == start - 600_000
    assert quoter.quoting_open(start - 600_001, start)
    assert not quoter.quoting_open(start - 600_000, start)
    assert not quoter.quoting_open(start - 1, None)


def test_named_strategies_differ_only_in_skew() -> None:
    cfg: Config = BASE
    assert cfg.with_strategy("baseline").quoting.skew_strength == 0
    assert cfg.with_strategy("skew").quoting.skew_strength > 0


_tick = st.sampled_from([0.01, 0.001])
_level = st.one_of(st.none(), st.integers(min_value=1, max_value=999).map(lambda m: m / 1000))


@given(
    fair=st.floats(min_value=0.0, max_value=1.0),
    net=st.floats(min_value=-800, max_value=800),
    tick=_tick,
    min_size=st.sampled_from([1.0, 5.0, 15.0]),
    best_bid=_level,
    best_ask=_level,
    skew=st.floats(min_value=0, max_value=0.2),
    half_spread=st.floats(min_value=0.001, max_value=0.2),
    multiplier=st.floats(min_value=0.0, max_value=1.0),
)
def test_quotes_are_always_valid(
    fair: float,
    net: float,
    tick: float,
    min_size: float,
    best_bid: float | None,
    best_ask: float | None,
    skew: float,
    half_spread: float,
    multiplier: float,
) -> None:
    quoter = _quoter(skew_strength=skew, half_spread=half_spread)
    cfg = BASE.quoting
    quotes = quoter.quotes(
        fair=fair,
        net_position=net,
        tick=tick,
        min_size=min_size,
        best_bid=best_bid,
        best_ask=best_ask,
        size_multiplier=multiplier,
    )
    for quote in quotes:
        assert on_tick(quote.price, tick)
        assert cfg.min_price <= quote.price <= cfg.max_price
        assert quote.size >= min_size
    if quotes.bid is not None:
        assert net + quotes.bid.size <= cfg.max_position_shares + 1e-9
        assert best_ask is None or quotes.bid.price < best_ask
    if quotes.ask is not None:
        assert -net + quotes.ask.size <= cfg.max_position_shares + 1e-9
        assert best_bid is None or quotes.ask.price > best_bid
    if quotes.bid is not None and quotes.ask is not None:
        assert quotes.bid.price < quotes.ask.price


def test_quote_iteration_order() -> None:
    quotes = _quoter().quotes(fair=0.5, net_position=0, tick=0.01, min_size=5)
    assert [q.side for q in quotes] == ["BUY", "SELL"]
    with pytest.raises(AttributeError):
        quotes.fair = 0.4  # type: ignore[misc]

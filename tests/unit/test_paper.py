from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from rich.console import Console

from mm.config import Config
from mm.paper.dashboard import render
from mm.paper.trader import PaperTrader, paper_latency, paper_run_id
from mm.sim.backtest import run_backtest
from mm.sim.replay import NoLatencyData, replay
from mm.storage import schema as s
from mm.storage.store import Store, meta_from_row
from mm.types import BookSnapshot, Gap, MarketMeta, MetaChange, Tick
from tests.unit.test_engine_backtest import CID, HOUR, MIN, _cfg, _seed

FILL_KEYS = ("seq", "ts", "order_id", "side", "price", "size", "yes_side", "yes_price", "fair")


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _trader(cfg: Config, store: Store, clock: Clock, strategy: str = "baseline") -> PaperTrader:
    return PaperTrader(cfg, store, strategy, latency_ms=100, clock=clock, sha="abc1234")


def _metas(store: Store) -> list[MarketMeta]:
    return [meta_from_row(r) for r in store.markets()]


def test_paper_run_matches_backtest_of_the_same_data(cfg: Config, store: Store) -> None:
    """Same events, same engine: the paper path must reproduce the backtest's fills and PnL."""
    cfg = _cfg(cfg)
    start = _seed(cfg, store)
    lo, hi = start - 3 * HOUR, start + HOUR
    backtest = run_backtest(cfg, store, "baseline", lo, hi, sha="abc1234")

    clock = Clock(lo)
    trader = _trader(cfg, store, clock)
    trader.begin()
    tick_ms = int(cfg.sim.tick_seconds * 1000)
    warmup = int(cfg.fairvalue.max_odds_age_minutes * 60_000)
    for event in replay(store, lo, hi, tick_ms, warmup):
        if isinstance(event, Tick):
            clock.now = event.ts
            trader.tick()
        else:
            trader.on_event(event)
    clock.now = hi
    summary = trader.finish()

    def fills(run_id: str) -> list[tuple[Any, ...]]:
        return [tuple(r[k] for k in FILL_KEYS) for r in store.run_rows(s.sim_fills, run_id)]

    assert fills(trader.run_id) == fills(backtest.run_id)
    assert fills(trader.run_id), "the fixture is expected to produce fills"
    for key in ("pnl", "equity", "fills", "settled", "rewards_estimate"):
        assert summary[key] == pytest.approx(backtest.summary[key]), key
    run = store.run(trader.run_id)
    assert run is not None
    assert (run["mode"], run["strategy"], run["git_sha"]) == ("paper", "baseline", "abc1234")
    assert (run["data_start"], run["data_end"], run["finished"]) == (lo, hi, hi)
    assert trader.finish() == {}, "finishing twice writes nothing more"


def test_sync_pushes_markets_and_confirmed_mappings_once(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    start = _seed(cfg, store)
    trader = _trader(cfg, store, Clock(start - 2 * HOUR))
    trader.begin()
    trader.sync(_metas(store))
    state = trader.engine.markets[CID]
    assert (state.event_id, state.yes_is_home) == ("ev1", False)
    assert state.fair is not None, "odds already in the database warm fair value at start"
    seen: list[Any] = []
    trader.engine.on_event = seen.append  # type: ignore[method-assign, assignment]
    trader.sync(_metas(store))
    assert seen == []
    changed = _metas(store)
    trader.sync([type(changed[0])(**{**_as_dict(changed[0]), "tick_size": 0.01})])
    assert [type(e) for e in seen] == [MetaChange]


def _as_dict(meta: MarketMeta) -> dict[str, Any]:
    return {name: getattr(meta, name) for name in meta.__slots__}


def test_unconfirmed_mapping_is_never_used(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    start = _seed(cfg, store, mapped=False)
    store.upsert_mapping(
        {"condition_id": CID, "odds_event_id": "ev1", "match_method": "loose_time",
         "confidence": 0.5, "confirmed": False, "yes_is_home": False, "updated_ts": 0}
    )  # fmt: skip
    trader = _trader(cfg, store, Clock(start - 2 * HOUR))
    trader.begin()
    trader.sync(_metas(store))
    trader.tick()
    assert trader.engine.markets[CID].event_id is None
    assert trader.engine.markets[CID].stop_reason == "no_odds_match"
    assert not trader.engine.exchange.open_orders()


def test_view_shows_live_quotes_and_feed_state(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    start = _seed(cfg, store)
    now = start - 2 * HOUR
    clock = Clock(now)
    trader = _trader(cfg, store, clock)
    trader.begin()
    trader.sync(_metas(store))
    trader.on_event(Gap(now, "market_ws", None, False, ""))
    trader.on_event(BookSnapshot(now, now, "111", ((0.43, 100.0),), ((0.50, 100.0),), None, "ws"))
    trader.tick()
    clock.now = now + 1000
    trader.tick()
    view = trader.view()
    assert view.feeds == {"market_ws": True, "sports_ws": False}
    (market,) = view.markets
    assert market.state == "quoting"
    assert market.bid is not None and market.ask is not None
    assert market.bid[0] < market.fair < market.ask[0]  # type: ignore[operator]
    assert (market.best_bid, market.best_ask) == (0.43, 0.50)
    assert view.cash == cfg.risk.starting_capital and not view.halted
    console = Console(width=200, record=True)
    console.print(render(view))
    assert "Hawks v Owls" in console.export_text()
    trader.on_event(Gap(now + 2000, "market_ws", None, True, "error"))
    assert trader.view().feeds["market_ws"] is False
    assert trader.view().markets[0].state == "data_gap"


def test_flush_writes_run_rows_and_keeps_risk_events_for_the_dashboard(
    cfg: Config, store: Store
) -> None:
    cfg = _cfg(cfg)
    start = _seed(cfg, store)
    clock = Clock(start - 2 * HOUR)
    trader = _trader(cfg, store, clock)
    trader.begin()
    trader.sync(_metas(store))
    trader.tick()
    assert trader.flush() > 0
    assert store.run_rows(s.pnl_ledger, trader.run_id)
    events = store.run_rows(s.risk_events, trader.run_id)
    assert [e["rule"] for e in events] == ["stale_book"], "no book has arrived yet"
    assert [e.rule for e in trader.view().risk_events] == ["stale_book"]


class FakeSource:
    def __init__(self, markets: list[MarketMeta], body: Callable[[], Awaitable[None]]) -> None:
        self.markets = {m.condition_id: m for m in markets}
        self.refresh_hooks: list[Callable[[Any], Awaitable[None]]] = []
        self._body = body

    async def run(self) -> None:
        for hook in self.refresh_hooks:
            await hook(None)
        await self._body()


def _fast(cfg: Config) -> Config:
    return cfg.model_copy(
        update={
            "sim": cfg.sim.model_copy(update={"latency_ms": 100, "tick_seconds": 0.01}),
            "paper": cfg.paper.model_copy(
                update={"flush_seconds": 0.01, "dashboard_refresh_seconds": 0.01}
            ),
        }
    )


def test_run_ticks_flushes_and_finishes_when_time_is_up(cfg: Config, store: Store) -> None:
    cfg = _fast(cfg)
    start = _seed(cfg, store)
    trader = _trader(cfg, store, Clock(start - 2 * HOUR))
    source = FakeSource(_metas(store), lambda: asyncio.sleep(3600))
    console = Console(width=160, record=True, force_terminal=False)
    summary = asyncio.run(trader.run(source, console=console, duration=0.2))
    assert summary["markets"] == 1
    run = store.run(trader.run_id)
    assert run is not None and run["finished"] is not None
    assert store.run_rows(s.pnl_ledger, trader.run_id)


def test_engine_failure_stops_the_run_and_still_closes_it(cfg: Config, store: Store) -> None:
    cfg = _fast(cfg)
    start = _seed(cfg, store)
    trader = _trader(cfg, store, Clock(start - 2 * HOUR))

    async def body() -> None:
        trader.on_event(
            BookSnapshot(start, start, "111", ((0.43, 1.0),), ((0.5, 1.0),), None, "ws")
        )
        trader.engine.markets[CID].meta = None  # type: ignore[assignment]
        await asyncio.sleep(3600)

    source = FakeSource(_metas(store), body)
    with pytest.raises(AttributeError):
        asyncio.run(trader.run(source, dashboard=False, duration=5))
    run = store.run(trader.run_id)
    assert run is not None


def test_latency_comes_from_config_or_recent_recordings(cfg: Config, store: Store) -> None:
    assert paper_latency(_cfg(cfg), store, 10**13) == 100
    unset = cfg.model_copy(update={"sim": cfg.sim.model_copy(update={"latency_ms": None})})
    with pytest.raises(NoLatencyData, match=r"set sim\.latency_ms"):
        paper_latency(unset, store, 10**13)
    assert paper_run_id("skew", 123) == "paper-skew-123"
    assert MIN == 60_000

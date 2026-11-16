"""Live paper trading: the backtest's engine fed by the recorder's live events.

Nothing here can reach an exchange. Orders exist only inside `mm.sim.exchange.Exchange`; the
recorder is the only component with network access and it only reads public data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from rich.console import Console
from rich.live import Live

from mm.config import Config
from mm.engine import Engine, MarketState
from mm.paper.dashboard import DashboardView, MarketView, render
from mm.risk.engine import RiskEvent
from mm.sim.backtest import git_sha
from mm.sim.replay import NoLatencyData, measured_latency
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import (
    Event,
    Gap,
    MappingChange,
    MarketMeta,
    MetaChange,
    OddsQuote,
    Tick,
    iso,
    now_ms,
)

log = logging.getLogger(__name__)

LATENCY_LOOKBACK_MS = 86_400_000


class LiveSource(Protocol):
    """The part of `mm.recorder.service.Recorder` the paper trader depends on."""

    markets: dict[str, MarketMeta]
    refresh_hooks: list[Callable[[Any], Awaitable[None]]]

    async def run(self) -> None: ...


def paper_run_id(strategy: str, start: int) -> str:
    return f"paper-{strategy}-{start}"


def paper_latency(cfg: Config, store: Store, now: int) -> int:
    """Configured order latency, else the median receive gap recorded over the last day."""
    if cfg.sim.latency_ms is not None:
        return int(cfg.sim.latency_ms)
    try:
        return measured_latency(store, now - LATENCY_LOOKBACK_MS, now)
    except NoLatencyData as exc:
        raise NoLatencyData(
            "sim.latency_ms is null and the database has no deltas from the last 24 hours to "
            "measure it from; record first or set sim.latency_ms"
        ) from exc


class PaperTrader:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        strategy: str,
        *,
        latency_ms: int,
        clock: Callable[[], int] = now_ms,
        sha: str | None = None,
    ) -> None:
        self.cfg = cfg.with_strategy(strategy)
        self.store = store
        self.strategy = strategy
        self._clock = clock
        self.started = clock()
        self.run_id = paper_run_id(strategy, self.started)
        self.engine = Engine(self.cfg, self.run_id, latency_ms)
        self._sha = sha if sha is not None else git_sha()
        self._metas: dict[str, MarketMeta] = {}
        self._mappings: dict[str, tuple[str, bool]] = {}
        self._feeds: dict[str, bool] = {"market_ws": False, "sports_ws": False}
        self._risk_events: list[RiskEvent] = []
        self._finished = False
        self._failure: Exception | None = None

    # event intake ---------------------------------------------------------------------

    def on_event(self, event: Event) -> None:
        """Recorder listener. Runs on the event loop thread, in arrival order."""
        if isinstance(event, Gap) and event.token_id is None and event.stream in self._feeds:
            self._feeds[event.stream] = not event.open
        self._feed(event)

    def _feed(self, event: Event) -> None:
        """An engine error must stop the run; the feeds would otherwise treat it as a disconnect."""
        if self._failure is not None:
            return
        try:
            self.engine.on_event(event)
        except Exception as exc:
            log.exception("engine failed on %r", event)
            self._failure = exc

    def begin(self) -> None:
        """Write the run row and warm fair value with odds already in the database."""
        self.store.start_run(
            {
                "run_id": self.run_id,
                "mode": "paper",
                "strategy": self.strategy,
                "git_sha": self._sha,
                "config_hash": self.cfg.digest(),
                "data_start": self.started,
                "data_end": self.started,
                "started": self.started,
            }
        )
        horizon = int(self.cfg.fairvalue.max_odds_age_minutes * 60_000)
        for r in self.store.iter_odds(self.started - horizon, self.started):
            self.engine.on_event(
                OddsQuote(
                    r["ts_recv"],
                    r["sport"],
                    r["event_id"],
                    r["commence_time"],
                    r["home_team"],
                    r["away_team"],
                    r["bookmaker"],
                    r["home_price"],
                    r["away_price"],
                )
            )

    def sync(self, markets: Sequence[MarketMeta]) -> None:
        """Push new or changed market metadata and confirmed odds mappings into the engine."""
        now = self._clock()
        for meta in markets:
            if self._metas.get(meta.condition_id) != meta:
                self._metas[meta.condition_id] = meta
                self._feed(MetaChange(now, meta))
        for row in self.store.mappings(confirmed_only=True):
            cid = row["condition_id"]
            mapping = (row["odds_event_id"], bool(row["yes_is_home"]))
            if cid in self._metas and self._mappings.get(cid) != mapping:
                self._mappings[cid] = mapping
                self._feed(MappingChange(now, cid, *mapping))

    def tick(self) -> None:
        self._feed(Tick(self._clock()))
        if self._failure is not None:
            raise self._failure

    def flush(self) -> int:
        rows = self.engine.drain()
        for row in rows.get(s.risk_events, []):
            self._risk_events.append(
                RiskEvent(
                    row["ts"],
                    row["condition_id"],
                    row["rule"],
                    row["value"],
                    row["limit"],
                    row["action"],
                )
            )
        self.store.insert_many(rows)
        return sum(len(batch) for batch in rows.values())

    def finish(self) -> dict[str, Any]:
        if self._finished:
            return {}
        self._finished = True
        summary = self.engine.finish()
        self.flush()
        end = self._clock()
        self.store.finish_run(self.run_id, end, summary, data_end=end)
        log.info("paper run %s finished at %s: %s", self.run_id, iso(end), summary)
        return summary

    # dashboard ------------------------------------------------------------------------

    def _quote(self, order_id: int | None) -> tuple[float, float] | None:
        order = self.engine.exchange.orders.get(order_id) if order_id is not None else None
        if order is None or not order.open:
            return None
        return order.price, order.remaining if order.status == "live" else order.size

    def _market_view(self, m: MarketState) -> MarketView:
        book = self.engine.exchange.books.get(m.token_yes)
        pos = self.engine.portfolio.position(m.meta.condition_id)
        return MarketView(
            label=f"{m.meta.outcome_yes} v {m.meta.outcome_no}",
            league=m.meta.league,
            game_start=m.meta.game_start,
            fair=m.fair,
            bid=self._quote(m.bid),
            ask=self._quote(m.ask),
            best_bid=book.best_bid() if book else None,
            best_ask=book.best_ask() if book else None,
            inventory_yes=pos.yes,
            inventory_no=pos.no,
            pnl=pos.pnl(),
            state=m.stop_reason or "quoting",
        )

    def view(self) -> DashboardView:
        engine = self.engine
        now = self._clock()
        markets = sorted(
            engine.markets.values(), key=lambda m: (m.meta.game_start or 0, m.meta.condition_id)
        )
        return DashboardView(
            run_id=self.run_id,
            now=now,
            cash=engine.portfolio.cash,
            equity=engine.portfolio.equity(),
            pnl=engine.portfolio.pnl(),
            components=engine.portfolio.components(),
            rewards_estimate=engine.rewards.total(),
            fills=engine.fills,
            halted=engine.risk.halted(now),
            feeds=dict(self._feeds),
            markets=[self._market_view(m) for m in markets],
            risk_events=tuple(self._risk_events),
        )

    # run loop -------------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.sim.tick_seconds)
            self.tick()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.paper.flush_seconds)
            await asyncio.to_thread(self.flush)

    async def _dashboard_loop(self, console: Console) -> None:
        with Live(render(self.view()), console=console, screen=True, auto_refresh=False) as live:
            while True:
                await asyncio.sleep(self.cfg.paper.dashboard_refresh_seconds)
                live.update(render(self.view()), refresh=True)

    async def run(
        self,
        source: LiveSource,
        *,
        dashboard: bool = True,
        console: Console | None = None,
        duration: float | None = None,
    ) -> dict[str, Any]:
        """Trade on paper until cancelled, the source stops, or `duration` seconds pass."""

        async def on_refresh(_result: Any) -> None:
            self.sync(list(source.markets.values()))

        self.begin()
        source.refresh_hooks.append(on_refresh)
        tasks = [source.run(), self._tick_loop(), self._flush_loop()]
        if dashboard:
            tasks.append(self._dashboard_loop(console or Console()))
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*tasks), duration)
        finally:
            summary = self.finish()
        return summary

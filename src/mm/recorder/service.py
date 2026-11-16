from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from mm.config import Config
from mm.discovery.gamma import Discovery, DiscoveryResult, GammaClient
from mm.fairvalue.matcher import OddsEventRef, run_matching
from mm.net import Http, HttpError
from mm.recorder.backfill import TradeBackfill
from mm.recorder.market_ws import Listener, MarketFeed
from mm.recorder.odds import NoOddsKey, OddsClient, OddsPoller
from mm.recorder.snapshots import RestBooks
from mm.recorder.sports_ws import SportsFeed
from mm.recorder.writer import Writer
from mm.recorder.ws import Connector, default_connector
from mm.storage.store import Store
from mm.types import MarketMeta, MetaChange, now_ms

log = logging.getLogger(__name__)

RefreshHook = Callable[[DiscoveryResult], Awaitable[None]]


def wanted_tokens(
    markets: Iterable[MarketMeta],
    now: int,
    hours_after_start: float,
    hours_before_start: float | None = None,
) -> set[str]:
    """Tokens to record: open markets from ``hours_before_start`` ahead of the game (``None``
    means as soon as they are listed) until ``hours_after_start`` past it."""
    out: set[str] = set()
    for m in markets:
        if m.closed:
            continue
        if m.game_start is not None:
            if now > m.game_start + int(hours_after_start * 3_600_000):
                continue
            if (
                hours_before_start is not None
                and m.game_start - now > hours_before_start * 3_600_000
            ):
                continue
        out.update((m.token_yes, m.token_no))
    return out


class RecorderAlreadyRunning(RuntimeError):
    pass


class Recorder:
    """Owns every outside connection: discovery, market and sports streams, REST books, odds."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        http: Http,
        *,
        odds_key: SecretStr | None = None,
        connector: Connector = default_connector,
        listeners: Iterable[Listener] = (),
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.http = http
        self.listeners = list(listeners)
        self.writer = Writer(cfg, store)
        self.feed = MarketFeed(
            cfg, store, self.writer, connector=connector, listeners=self.listeners
        )
        self.rest = RestBooks(cfg, http, self.writer, self.feed)
        self.feed.snapshot = self.rest.snapshot
        self.sports = SportsFeed(
            cfg, store, self.writer, connector=connector, listeners=self.listeners
        )
        self.discovery = Discovery(cfg, GammaClient(cfg, http), store)
        self.backfill = TradeBackfill(cfg, http, store)
        self.markets: dict[str, MarketMeta] = {}
        self.refresh_hooks: list[RefreshHook] = [self.match_odds]
        self.odds_client: OddsClient | None
        self.odds: OddsPoller | None
        try:
            self.odds_client = OddsClient(cfg, http, odds_key)
            self.odds = OddsPoller(
                cfg,
                self.odds_client,
                self.writer,
                lambda: list(self.markets.values()),
                self.listeners,
            )
        except NoOddsKey as exc:
            log.error("%s; odds will not be recorded and no game can be quoted", exc)
            self.odds_client = self.odds = None

    async def odds_events(self) -> list[OddsEventRef]:
        rows = await asyncio.to_thread(self.store.odds_events)
        refs = {
            r["event_id"]: OddsEventRef(
                r["event_id"], r["sport"], r["commence_time"], r["home_team"], r["away_team"]
            )
            for r in rows
        }
        if self.odds_client is not None:
            for league in self.cfg.leagues:
                sport = self.cfg.odds.sport_keys[league]
                try:
                    events = await self.odds_client.events(sport)
                except Exception as exc:
                    log.error("odds /events for %s failed: %s", sport, exc)
                    continue
                for ev in events:
                    refs[ev.id] = OddsEventRef(
                        ev.id, ev.sport_key, ev.commence_ms, ev.home_team, ev.away_team
                    )
        return list(refs.values())

    async def match_odds(self, _result: DiscoveryResult) -> None:
        events = await self.odds_events()
        matches = await asyncio.to_thread(run_matching, self.cfg, self.store, events, now_ms())
        review = sum(1 for m in matches if not m.confirmed and m.event is not None)
        unmatched = sum(1 for m in matches if m.event is None)
        if review:
            log.warning(
                "%d odds matches need a person to confirm them in %s",
                review,
                self.cfg.matching.review_file,
            )
        if unmatched:
            log.info("%d markets have no odds event yet and cannot be quoted", unmatched)

    async def check_geoblock(self) -> None:
        try:
            result = await self.http.get(self.cfg.endpoints.geoblock)
        except Exception as exc:
            log.error("geoblock check failed: %s", exc)
            return
        log.info("geoblock: %s", result)
        if isinstance(result, dict) and result.get("blocked"):
            log.warning("this IP is geoblocked for trading; public reads continue, nothing else")

    async def refresh(self) -> DiscoveryResult:
        result = await self.discovery.run_once(datetime.now(UTC), now_ms())
        self.markets = {m.condition_id: m for m in result.markets}
        now = now_ms()
        added, removed = self.feed.set_tokens(
            wanted_tokens(
                result.markets,
                now,
                self.cfg.recorder.record_hours_after_start,
                self.cfg.recorder.record_hours_before_start,
            )
        )
        self.feed.start()
        log.info(
            "discovery: %d markets, %d changed, +%d/-%d tokens",
            len(result.markets),
            len(result.changed),
            len(added),
            len(removed),
        )
        for meta in result.changed:
            event = MetaChange(now, meta)
            for fn in self.listeners:
                fn(event)
        for hook in self.refresh_hooks:
            await hook(result)
        await self.backfill.run_once()
        return result

    async def _discovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.discovery.refresh_minutes * 60)
            try:
                await self.refresh()
            except (HttpError, httpx.TransportError) as exc:
                # An outage of the discovery API must not stop the books being recorded;
                # a response that no longer matches its model still stops the run.
                log.error("discovery refresh failed, keeping the current markets: %s", exc)

    async def _heartbeat(self) -> None:
        while True:
            resolved, self.feed.resolved = self.feed.resolved, []
            for condition_id, winner in resolved:
                await asyncio.to_thread(self.store.set_winner, condition_id, winner)
            await asyncio.to_thread(self.store.set_heartbeat, now_ms())
            await asyncio.sleep(self.cfg.recorder.heartbeat_seconds)

    async def run(self) -> None:
        self.store.init()
        beat = self.store.heartbeat()
        limit = int(self.cfg.recorder.heartbeat_stale_seconds * 1000)
        if beat is not None and 0 <= now_ms() - beat < limit:
            raise RecorderAlreadyRunning(
                f"a recorder wrote a heartbeat to this database {(now_ms() - beat) / 1000:.0f} s "
                "ago; two recorders on one database would store every book and trade twice. "
                f"Stop the other one (or wait {limit / 1000:.0f} s after it stopped) and retry"
            )
        closed = self.store.close_dangling_gaps(now_ms())
        if closed:
            log.warning("closed %d gaps left open by a previous run", closed)
        await self.check_geoblock()
        await self.refresh()
        tasks = [
            self.writer.run(),
            self.rest.run(),
            self.sports.run(),
            self._discovery_loop(),
            self._heartbeat(),
        ]
        if self.odds is not None:
            tasks.append(self.odds.run())
        try:
            await asyncio.gather(*tasks)
        finally:
            await self.feed.stop()
            self.writer.flush()

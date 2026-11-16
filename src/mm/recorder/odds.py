from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from mm.config import Config
from mm.net import Http, validate_list
from mm.recorder.writer import Writer
from mm.types import MarketMeta, OddsQuote, now_ms, parse_ts

log = logging.getLogger(__name__)

H2H = "h2h"


class _Odds(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class OddsOutcome(_Odds):
    name: str
    price: float = Field(gt=1.0)


class OddsMarket(_Odds):
    key: str
    outcomes: list[OddsOutcome]


class OddsBookmaker(_Odds):
    key: str
    markets: list[OddsMarket] = Field(default_factory=list)


class OddsEvent(_Odds):
    id: str
    sport_key: str
    commence_time: str
    home_team: str
    away_team: str
    bookmakers: list[OddsBookmaker] = Field(default_factory=list)

    @property
    def commence_ms(self) -> int:
        return parse_ts(self.commence_time)


def event_quotes(ev: OddsEvent, ts: int) -> tuple[list[OddsQuote], list[str]]:
    """Two-way h2h quotes per bookmaker, plus the keys of bookmakers that were skipped."""
    quotes: list[OddsQuote] = []
    skipped: list[str] = []
    for bm in ev.bookmakers:
        market = next((m for m in bm.markets if m.key == H2H), None)
        if market is None:
            continue
        prices = {o.name: o.price for o in market.outcomes}
        if len(market.outcomes) != 2 or set(prices) != {ev.home_team, ev.away_team}:
            skipped.append(bm.key)
            continue
        quotes.append(
            OddsQuote(
                ts,
                ev.sport_key,
                ev.id,
                ev.commence_ms,
                ev.home_team,
                ev.away_team,
                bm.key,
                prices[ev.home_team],
                prices[ev.away_team],
            )
        )
    return quotes, skipped


class NoOddsKey(RuntimeError):
    pass


class OddsClient:
    def __init__(self, cfg: Config, http: Http, key: SecretStr | None) -> None:
        if key is None or not key.get_secret_value():
            raise NoOddsKey("ODDS_API_KEY is not set (.env or environment)")
        self._cfg = cfg
        self._http = http
        self._key = key
        self._base = cfg.endpoints.odds_api.rstrip("/")
        self.remaining: float | None = None

    def _note_credits(self, headers: object) -> None:
        get = getattr(headers, "get", None)
        if get is None:
            return
        remaining, used = get("x-requests-remaining"), get("x-requests-used")
        log.info("odds api credits: remaining=%s used=%s", remaining, used)
        if remaining is not None:
            try:
                self.remaining = float(remaining)
            except ValueError:
                log.error("unparseable x-requests-remaining header: %r", remaining)

    async def odds(self, sport: str) -> list[OddsEvent]:
        params = {
            "apiKey": self._key.get_secret_value(),
            "regions": ",".join(self._cfg.odds.regions),
            "markets": self._cfg.odds.markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        data, headers = await self._http.request(
            "GET", f"{self._base}/sports/{sport}/odds", params=params
        )
        self._note_credits(headers)
        return validate_list(OddsEvent, data, f"odds_{sport}", self._cfg.paths.data_dir)

    async def events(self, sport: str) -> list[OddsEvent]:
        params = {"apiKey": self._key.get_secret_value(), "dateFormat": "iso"}
        data, headers = await self._http.request(
            "GET", f"{self._base}/sports/{sport}/events", params=params
        )
        self._note_credits(headers)
        return validate_list(OddsEvent, data, f"odds_events_{sport}", self._cfg.paths.data_dir)


@dataclass
class OddsSchedule:
    """Decides when each league is polled, within the credit budget."""

    cfg: Config
    done: set[tuple[str, str]]

    def offsets(self, remaining: float | None) -> Sequence[float]:
        o = self.cfg.odds
        if remaining is not None and remaining <= o.min_credits_reserve:
            return o.reduced_poll_minutes_before_start
        return o.poll_minutes_before_start

    def due_keys(
        self, markets: Sequence[MarketMeta], league: str, now: int, remaining: float | None
    ) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = []
        offsets = self.offsets(remaining)
        reduced = offsets != self.cfg.odds.poll_minutes_before_start
        for m in markets:
            if m.league != league or m.closed or m.game_start is None or now >= m.game_start:
                continue
            if self.cfg.odds.poll_on_new_game and not reduced:
                keys.append((m.condition_id, "new"))
            for minutes in offsets:
                if now >= m.game_start - int(minutes * 60_000):
                    keys.append((m.condition_id, f"T-{minutes:g}"))
        return [k for k in keys if k not in self.done]


class OddsPoller:
    def __init__(
        self,
        cfg: Config,
        client: OddsClient,
        writer: Writer,
        markets: Callable[[], Sequence[MarketMeta]],
        listeners: Iterable[Callable[[OddsQuote], None]] = (),
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._writer = writer
        self._markets = markets
        self._listeners = list(listeners)
        self.schedule = OddsSchedule(cfg, set())
        self._last_interval_poll: dict[str, int] = {}
        self.stopped = False

    @property
    def cost(self) -> int:
        return len(self._cfg.odds.regions) * len(self._cfg.odds.markets.split(","))

    def due(self, now: int) -> dict[str, list[tuple[str, str]]]:
        remaining = self._client.remaining
        if remaining is not None and remaining < self.cost:
            if not self.stopped:
                log.error("odds api credits exhausted (%s left); odds polling stopped", remaining)
            self.stopped = True
            return {}
        markets = self._markets()
        out: dict[str, list[tuple[str, str]]] = {}
        interval = self._cfg.odds.interval_minutes
        for league in self._cfg.leagues:
            if interval is not None:
                live = any(
                    m.league == league and not m.closed and (m.game_start or 0) > now
                    for m in markets
                )
                last = self._last_interval_poll.get(league)
                if live and (last is None or now - last >= interval * 60_000):
                    out[league] = []
                continue
            keys = self.schedule.due_keys(markets, league, now, remaining)
            if keys:
                out[league] = keys
        return out

    async def poll(self, league: str, now: int) -> int:
        sport = self._cfg.odds.sport_keys[league]
        events = await self._client.odds(sport)
        ts = now_ms()
        n = 0
        for ev in events:
            quotes, skipped = event_quotes(ev, ts)
            if skipped:
                log.info("%s: skipped non two-way h2h from %s", ev.id, ",".join(skipped))
            for q in quotes:
                self._writer.add_odds(q)
                for fn in self._listeners:
                    fn(q)
                n += 1
        self._last_interval_poll[league] = now
        return n

    async def run_once(self, now: int) -> int:
        total = 0
        for league, keys in self.due(now).items():
            total += await self.poll(league, now)
            self.schedule.done.update(keys)
        return total

    async def run(self) -> None:
        while True:
            try:
                await self.run_once(now_ms())
            except Exception:
                log.exception("odds poll failed")
            await asyncio.sleep(self._cfg.odds.check_seconds)

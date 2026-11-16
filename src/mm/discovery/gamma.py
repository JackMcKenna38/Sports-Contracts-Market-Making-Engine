from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mm.config import Config
from mm.discovery.models import GammaMarket, MarketRewardsPage, SportRow, TeamRow
from mm.net import Http, PayloadError, save_payload, validate, validate_list
from mm.storage.store import Store, meta_from_row
from mm.types import MarketMeta, parse_ts

log = logging.getLogger(__name__)

CENTS = 100.0


class DiscoveryError(RuntimeError):
    pass


class GammaClient:
    def __init__(self, cfg: Config, http: Http) -> None:
        self._base = cfg.endpoints.gamma.rstrip("/")
        self._clob = cfg.endpoints.clob.rstrip("/")
        self._http = http
        self._page = cfg.discovery.page_limit
        self._data_dir = cfg.paths.data_dir

    async def sports(self) -> list[SportRow]:
        payload = await self._http.get(f"{self._base}/sports")
        return validate_list(SportRow, payload, "gamma_sports", self._data_dir)

    async def teams(self, league: str) -> list[TeamRow]:
        rows: list[TeamRow] = []
        offset = 0
        while True:
            payload = await self._http.get(
                f"{self._base}/teams",
                {"league": league, "limit": self._page, "offset": offset},
            )
            page = validate_list(TeamRow, payload, f"gamma_teams_{league}", self._data_dir)
            rows.extend(page)
            if len(page) < self._page:
                return rows
            offset += self._page

    async def daily_reward_rate(self, condition_id: str, day: date) -> float:
        """Reward pool per day configured on a market for ``day``; 0 when none is configured."""
        payload = await self._http.get(f"{self._clob}/rewards/markets/{condition_id}")
        page = validate(MarketRewardsPage, payload, "clob_rewards_market", self._data_dir)
        return sum(
            cfg.rate_per_day
            for row in page.data
            if row.condition_id == condition_id
            for cfg in row.rewards_config
            if cfg.start_date <= day <= cfg.end_date
        )

    async def markets(self, params: dict[str, Any]) -> list[tuple[GammaMarket, Any]]:
        """All pages for ``params``; each model is paired with its raw payload."""
        out: list[tuple[GammaMarket, Any]] = []
        offset = 0
        while True:
            payload = await self._http.get(
                f"{self._base}/markets", {**params, "limit": self._page, "offset": offset}
            )
            if not isinstance(payload, list):
                path = save_payload(self._data_dir, "gamma_markets", payload)
                raise PayloadError(f"gamma /markets did not return a list; saved to {path}")
            page = validate_list(GammaMarket, payload, "gamma_markets", self._data_dir)
            out.extend(zip(page, payload, strict=True))
            if len(page) < self._page:
                return out
            offset += self._page


def league_tags(sports: Sequence[SportRow], leagues: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Tag ids per league: ``primaryTagId`` when present, else the tags unique to that row.

    Tags present on every sport row are generic (e.g. a shared "sports" tag) and are dropped.
    """
    by_sport = {row.sport.lower(): row for row in sports}
    common = set.intersection(*(set(r.tag_ids()) for r in sports)) if len(sports) > 1 else set()
    out: dict[str, tuple[str, ...]] = {}
    for league in leagues:
        row = by_sport.get(league.lower())
        if row is None:
            raise DiscoveryError(f"gamma /sports has no row for league {league!r}")
        if row.primary_tag_id is not None:
            out[league] = (str(row.primary_tag_id),)
            continue
        tags = tuple(t for t in row.tag_ids() if t not in common)
        if not tags:
            raise DiscoveryError(f"gamma /sports row for {league!r} has no league-specific tag")
        out[league] = tags
    return out


def final_winner(m: GammaMarket) -> str | None:
    """Winning token of a closed market, only when its final prices are exactly 0 and 1."""
    if not m.closed or m.outcome_prices is None or len(m.outcome_prices) != 2:
        return None
    if sorted(m.outcome_prices) != [0.0, 1.0]:
        return None
    return m.clob_token_ids[m.outcome_prices.index(1.0)]


def league_orderings(sports: Sequence[SportRow]) -> dict[str, str | None]:
    return {row.sport.lower(): row.ordering for row in sports}


def home_away(m: GammaMarket, ordering: str | None) -> tuple[str | None, str | None]:
    """Home and away team names.

    The event's own names win when present. Otherwise the league's ``ordering`` from
    ``/sports`` says which side is listed first in ``outcomes``.
    """
    event = m.events[0] if m.events else None
    if event is not None and event.home_team_name and event.away_team_name:
        return event.home_team_name, event.away_team_name
    if ordering == "away":
        return m.outcomes[1], m.outcomes[0]
    if ordering == "home":
        return m.outcomes[0], m.outcomes[1]
    return None, None


def market_row(m: GammaMarket, league: str, ordering: str | None = None) -> dict[str, Any]:
    if len(m.outcomes) != 2 or len(m.clob_token_ids) != 2:
        raise DiscoveryError(
            f"{m.condition_id}: expected 2 outcomes and 2 token ids, got "
            f"{len(m.outcomes)} and {len(m.clob_token_ids)}"
        )
    if m.fee_schedule is None:
        raise DiscoveryError(f"{m.condition_id}: no feeSchedule")
    event = m.events[0] if m.events else None
    home, away = home_away(m, ordering)
    game_id = m.game_id if m.game_id is not None else (event.game_id if event else None)
    start = m.game_start_time or (event.start_time if event else None)
    return {
        "condition_id": m.condition_id,
        "gamma_id": str(m.id),
        "slug": m.slug,
        "question": m.question,
        "league": league,
        "token_id_yes": m.clob_token_ids[0],
        "token_id_no": m.clob_token_ids[1],
        "outcome_yes": m.outcomes[0],
        "outcome_no": m.outcomes[1],
        "home": home,
        "away": away,
        "game_id": str(game_id) if game_id is not None else None,
        "game_start_utc": parse_ts(start) if start else None,
        "tick_size": m.order_price_min_tick_size,
        "min_size": m.order_min_size,
        "fee_rate": m.fee_schedule.rate,
        "fee_exponent": m.fee_schedule.exponent,
        "rebate_rate": m.fee_schedule.rebate_rate,
        "rewards_max_spread": (m.rewards_max_spread or 0.0) / CENTS,
        "rewards_min_size": m.rewards_min_size or 0.0,
        "rewards_daily_rate": sum(r.rewards_daily_rate for r in m.clob_rewards),
        "seconds_delay": m.seconds_delay,
        "neg_risk": m.neg_risk,
        "accepting_orders": m.accepting_orders,
        "closed": m.closed,
        "winning_token_id": final_winner(m),
    }


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    markets: tuple[MarketMeta, ...]
    changed: tuple[MarketMeta, ...]
    skipped: tuple[tuple[str, str], ...]


class Discovery:
    def __init__(self, cfg: Config, client: GammaClient, store: Store) -> None:
        self._cfg = cfg
        self._client = client
        self._store = store
        self._orderings: dict[str, str | None] = {}

    async def fetch(self, now: datetime) -> list[tuple[str, GammaMarket, Any]]:
        sports = await self._client.sports()
        tags = league_tags(sports, self._cfg.leagues)
        self._orderings = league_orderings(sports)
        since = (now - timedelta(days=self._cfg.discovery.closed_lookback_days)).astimezone(UTC)
        seen: dict[str, tuple[str, GammaMarket, Any]] = {}
        for league, tag_ids in tags.items():
            for tag in tag_ids:
                base = {"tag_id": tag, "sports_market_types": self._cfg.market_type}
                batches = (
                    await self._client.markets({**base, "closed": "false"}),
                    await self._client.markets(
                        {**base, "closed": "true", "end_date_min": since.isoformat()}
                    ),
                )
                for batch in batches:
                    for market, raw in batch:
                        if market.sports_market_type not in (None, self._cfg.market_type):
                            continue
                        seen.setdefault(market.condition_id, (league, market, raw))
        return list(seen.values())

    def _close_known(self, market: GammaMarket) -> dict[str, Any] | None:
        """Row for a closed market that Gamma no longer describes in full.

        Closed markets come back without ``feeSchedule``. One recorded while it was open keeps
        its stored terms and is only marked closed, with its winner when the final prices
        give one; one never seen open is not stored at all.
        """
        known = self._store.market(market.condition_id)
        if known is None:
            return None
        row = {k: v for k, v in known.items() if k not in ("first_seen", "last_seen")}
        row.update(closed=True, accepting_orders=False, winning_token_id=final_winner(market))
        return row

    async def _reward_rate(self, row: dict[str, Any], now: datetime) -> float:
        start = row["game_start_utc"]
        horizon = int(self._cfg.discovery.rewards_window_hours * 3_600_000)
        now_ms = int(now.timestamp() * 1000)
        if row["closed"] or start is None or not now_ms - horizon <= start <= now_ms + horizon:
            return float(row["rewards_daily_rate"])
        return await self._client.daily_reward_rate(row["condition_id"], now.astimezone(UTC).date())

    async def run_once(self, now: datetime, ts: int) -> DiscoveryResult:
        found = await self.fetch(now)
        markets: list[MarketMeta] = []
        changed: list[MarketMeta] = []
        skipped: list[tuple[str, str]] = []
        for league, market, raw in found:
            try:
                row: dict[str, Any] | None = market_row(
                    market, league, self._orderings.get(league.lower())
                )
            except DiscoveryError as exc:
                if not market.closed:
                    path = save_payload(self._cfg.paths.data_dir, "gamma_market", raw)
                    raise DiscoveryError(f"{exc}; raw payload saved to {path}") from exc
                row = self._close_known(market)
                if row is None:
                    skipped.append((market.condition_id, str(exc)))
                    continue
            assert row is not None
            row["rewards_daily_rate"] = await self._reward_rate(row, now)
            meta = meta_from_row(row)
            markets.append(meta)
            if self._store.upsert_market(row, ts):
                changed.append(meta)
        if skipped:
            log.info("%d closed markets never recorded while open were not stored", len(skipped))
        markets.sort(key=lambda m: (m.game_start or 0, m.condition_id))
        return DiscoveryResult(tuple(markets), tuple(changed), tuple(skipped))

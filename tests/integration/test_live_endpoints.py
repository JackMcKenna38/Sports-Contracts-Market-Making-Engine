"""One call per live public endpoint, validated against the wire models.

Run nightly (`pytest -m integration`). A failure here means an upstream API changed or is
unreachable; the rejected payload is saved under the test's data directory.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from mm.config import Config, Secrets
from mm.discovery.gamma import Discovery, GammaClient, home_away, league_tags
from mm.discovery.models import GammaMarket
from mm.net import Http, PayloadError
from mm.recorder.backfill import BackfillError, TradeBackfill
from mm.recorder.messages import MARKET_MSG, GameMsg, RestBook, market_subscribe
from mm.recorder.odds import OddsClient
from mm.recorder.ws import default_connector, recv_text
from mm.storage.store import Store

pytestmark = pytest.mark.integration


def _with_http[T](cfg: Config, body: Callable[[Http], Awaitable[T]]) -> T:
    async def go() -> T:
        http = Http(cfg)
        try:
            return await body(http)
        finally:
            await http.aclose()

    return asyncio.run(go())


def _open_markets(cfg: Config, store: Store) -> list[tuple[str, GammaMarket, Any]]:
    async def body(http: Http) -> list[tuple[str, GammaMarket, Any]]:
        found = await Discovery(cfg, GammaClient(cfg, http), store).fetch(datetime.now(UTC))
        return [row for row in found if not row[1].closed]

    return _with_http(cfg, body)


def _require_open_market(cfg: Config, store: Store) -> GammaMarket:
    markets = _open_markets(cfg, store)
    if not markets:
        pytest.skip("no open NBA or NHL moneyline market right now (off-season?)")
    return markets[0][1]


def test_gamma_sports_has_a_tag_for_every_league(cfg: Config) -> None:
    async def body(http: Http) -> dict[str, tuple[str, ...]]:
        return league_tags(await GammaClient(cfg, http).sports(), cfg.leagues)

    tags = _with_http(cfg, body)
    assert set(tags) == set(cfg.leagues)
    assert all(tags.values())


def test_gamma_sports_rows_carry_primary_tag_id(cfg: Config) -> None:
    async def body(http: Http) -> Any:
        return await http.get(cfg.endpoints.gamma.rstrip("/") + "/sports")

    rows = {row["sport"]: row for row in _with_http(cfg, body)}
    for league in cfg.leagues:
        assert "primaryTagId" in rows[league], f"/sports row for {league} has no primaryTagId"


@pytest.mark.parametrize("league", ["nba", "nhl"])
def test_gamma_teams(cfg: Config, league: str) -> None:
    async def body(http: Http) -> int:
        return len(await GammaClient(cfg, http).teams(league))

    assert _with_http(cfg, body) > 0


def test_gamma_markets_carry_every_field_discovery_stores(cfg: Config, store: Store) -> None:
    for _league, market, raw in _open_markets(cfg, store):
        for name in (
            "conditionId",
            "clobTokenIds",
            "outcomes",
            "gameStartTime",
            "outcomePrices",
            "orderPriceMinTickSize",
            "orderMinSize",
            "feeSchedule",
            "acceptingOrders",
            "closed",
            "negRisk",
            "sportsMarketType",
        ):
            assert name in raw, f"{market.condition_id}: /markets row has no {name}"
        fee = raw["feeSchedule"]
        fee = json.loads(fee) if isinstance(fee, str) else fee
        assert {"rate", "exponent", "takerOnly", "rebateRate"} <= set(fee)
        assert market.sports_market_type == cfg.market_type


def test_discovery_stores_markets(cfg: Config, store: Store) -> None:
    async def body(http: Http) -> int:
        result = await Discovery(cfg, GammaClient(cfg, http), store).run_once(
            datetime.now(UTC), int(datetime.now(UTC).timestamp() * 1000)
        )
        return len(result.markets)

    assert _with_http(cfg, body) == len(store.markets())


def test_clob_book_and_books(cfg: Config, store: Store) -> None:
    market = _require_open_market(cfg, store)
    base = cfg.endpoints.clob.rstrip("/")

    async def body(http: Http) -> tuple[Any, Any]:
        one = await http.get(f"{base}/book", {"token_id": market.clob_token_ids[0]})
        many = await http.post(f"{base}/books", [{"token_id": t} for t in market.clob_token_ids])
        return one, many

    one, many = _with_http(cfg, body)
    book = RestBook.model_validate(one)
    assert book.asset_id == market.clob_token_ids[0]
    assert book.market == market.condition_id
    assert book.hash, "REST book has no hash; replay verification depends on it"
    assert book.timestamp > 10**12, "REST book timestamp is not epoch milliseconds"
    assert {RestBook.model_validate(b).asset_id for b in many} == set(market.clob_token_ids)


def test_market_websocket_sends_a_book_after_subscribing(cfg: Config, store: Store) -> None:
    market = _require_open_market(cfg, store)

    async def go() -> list[Any]:
        async with default_connector(cfg.endpoints.clob_ws) as ws:
            await ws.send(json.dumps(market_subscribe(list(market.clob_token_ids))))
            await ws.send("PING")
            frames: list[Any] = []
            pong = False
            async with asyncio.timeout(30):
                while not (pong and frames):
                    text = await recv_text(ws, cfg.recorder.market_ws_idle_timeout_seconds)
                    if text == "PONG":
                        pong = True
                    else:
                        frames.append(json.loads(text))
            return frames

    items = [
        i for frame in asyncio.run(go()) for i in (frame if isinstance(frame, list) else [frame])
    ]
    parsed = [MARKET_MSG.validate_python(item) for item in items]
    books = [m for m in parsed if m.event_type == "book"]
    assert books, "no book message after subscribing with initial_dump"
    assert all(b.hash for b in books)
    assert all(b.timestamp > 10**12 for b in books)


def test_sports_websocket_frames_for_our_leagues_match_the_model(cfg: Config) -> None:
    async def go() -> list[str]:
        frames: list[str] = []
        async with default_connector(cfg.endpoints.sports_ws) as ws:
            async with asyncio.timeout(30):
                while len(frames) < 20:
                    text = await recv_text(ws, cfg.recorder.sports_ws_idle_timeout_seconds)
                    if text.strip().lower() == "ping":
                        await ws.send("pong")
                    else:
                        frames.append(text)
        return frames

    try:
        frames = asyncio.run(go())
    except TimeoutError:
        pytest.skip("fewer than 20 sports frames in 30 s; nothing live right now")
    for text in frames:
        payload = json.loads(text)
        for item in payload if isinstance(payload, list) else [payload]:
            assert "leagueAbbreviation" in item, f"frame without a league: {item}"
            if item["leagueAbbreviation"].lower() in cfg.leagues:
                GameMsg.model_validate(item)


def test_outcome_order_follows_the_league_ordering(cfg: Config, store: Store) -> None:
    """Discovery reads home and away from `/sports` ordering; check it against one event's teams."""
    league, market, _raw = _open_markets(cfg, store)[0]

    async def body(http: Http) -> tuple[Any, Any]:
        gamma = cfg.endpoints.gamma.rstrip("/")
        sports = await http.get(f"{gamma}/sports")
        events = await http.get(f"{gamma}/events", {"id": market.events[0].id})
        return sports, events

    sports, events = _with_http(cfg, body)
    ordering = next(row["ordering"] for row in sports if row["sport"] == league)
    teams = {team["ordering"]: team["name"] for team in events[0]["teams"]}
    first, second = market.outcomes
    expected = (second, first) if ordering == "away" else (first, second)
    assert (teams["home"], teams["away"]) == expected
    assert home_away(market, ordering) == expected


def test_data_api_trades_for_a_recently_closed_market(cfg: Config, store: Store) -> None:
    async def body(http: Http) -> str:
        found = await Discovery(cfg, GammaClient(cfg, http), store).fetch(datetime.now(UTC))
        closed = [m for _lg, m, _raw in found if m.closed]
        if not closed:
            pytest.skip("no recently closed market to backfill")
        try:
            rows = await TradeBackfill(cfg, http, store).fetch(closed[0].condition_id)
        except BackfillError as exc:
            pytest.fail(f"trade history is longer than the configured page budget: {exc}")
        except PayloadError as exc:
            pytest.fail(f"Data API v2 /trades shape differs from the model: {exc}")
        assert rows, "a closed game with no trades at all is implausible; check the query"
        assert all(r["ts"] > 10**12 for r in rows)
        return closed[0].condition_id

    _with_http(cfg, body)


def test_geoblock_reports_blocked_and_country(cfg: Config) -> None:
    async def body(http: Http) -> Any:
        return await http.get(cfg.endpoints.geoblock)

    result = _with_http(cfg, body)
    assert isinstance(result, dict)
    assert {"blocked", "country"} <= set(result)


@pytest.mark.parametrize("league", ["nba", "nhl"])
def test_odds_api_events_endpoint(cfg: Config, league: str) -> None:
    key = Secrets().odds_api_key
    if key is None or not key.get_secret_value():
        pytest.skip("ODDS_API_KEY is not set")

    async def body(http: Http) -> None:
        client = OddsClient(cfg, http, key)
        for event in await client.events(cfg.odds.sport_keys[league]):
            assert event.commence_ms > 10**12

    _with_http(cfg, body)

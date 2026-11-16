from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from mm.config import Config
from mm.discovery.gamma import (
    Discovery,
    DiscoveryError,
    GammaClient,
    final_winner,
    home_away,
    league_orderings,
    league_tags,
    market_row,
)
from mm.discovery.models import GammaMarket, SportRow
from mm.net import Http
from mm.storage import schema as s
from mm.storage.store import Store
from tests.conftest import fixture_json

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
TS = 1_763_294_400_000
REWARDED = {"0xcond_synthetic_b"}


def _transport(markets: list[Any], calls: list[httpx.Request]) -> httpx.MockTransport:
    sports = fixture_json("gamma_sports_synthetic.json")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/sports":
            return httpx.Response(200, json=sports)
        if request.url.path == "/markets":
            params = request.url.params
            if params["tag_id"] == "7450" and params["closed"] == "false":
                return httpx.Response(200, json=markets)
            return httpx.Response(200, json=[])
        if request.url.path.startswith("/rewards/markets/"):
            cid = request.url.path.rsplit("/", 1)[1]
            config = [
                {"start_date": "2026-11-01", "end_date": "2500-12-31", "rate_per_day": 6},
                {"start_date": "2026-12-01", "end_date": "2500-12-31", "rate_per_day": 40},
            ]
            rows = [{"condition_id": cid, "rewards_config": config}] if cid in REWARDED else []
            return httpx.Response(200, json={"data": rows, "next_cursor": "LTE=", "count": 0})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def _run(cfg: Config, store: Store, markets: list[Any]) -> tuple[Any, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    async def go() -> Any:
        http = Http(cfg, transport=_transport(markets, calls))
        try:
            return await Discovery(cfg, GammaClient(cfg, http), store).run_once(NOW, TS)
        finally:
            await http.aclose()

    return asyncio.run(go()), calls


def test_league_tags_drop_tags_shared_by_every_league() -> None:
    sports = [SportRow.model_validate(r) for r in fixture_json("gamma_sports_synthetic.json")]
    assert league_tags(sports, ["nba", "nhl"]) == {"nba": ("7450",), "nhl": ("8990",)}


def test_league_tags_prefer_primary_tag() -> None:
    rows = [{"id": 1, "sport": "nba", "tags": "1,7450", "primaryTagId": 42}]
    sports = [SportRow.model_validate(r) for r in rows]
    assert league_tags(sports, ["nba"]) == {"nba": ("42",)}


def test_missing_league_fails() -> None:
    sports = [SportRow.model_validate(r) for r in fixture_json("gamma_sports_synthetic.json")]
    with pytest.raises(DiscoveryError, match="nfl"):
        league_tags(sports, ["nfl"])


def test_market_row_maps_tokens_fees_and_rewards() -> None:
    raw = fixture_json("gamma_markets_synthetic.json")[0]
    row = market_row(GammaMarket.model_validate(raw), "nba")
    assert (row["token_id_yes"], row["token_id_no"]) == ("111", "222")
    assert (row["outcome_yes"], row["outcome_no"]) == ("Hawks", "Owls")
    assert (row["home"], row["away"]) == ("Owls", "Hawks")
    assert row["game_start_utc"] == int(
        datetime(2026, 11, 17, 0, 30, tzinfo=UTC).timestamp() * 1000
    )
    assert (row["tick_size"], row["min_size"]) == (0.001, 5)
    assert (row["fee_rate"], row["fee_exponent"], row["rebate_rate"]) == (0.05, 1, 0.15)
    assert row["rewards_max_spread"] == pytest.approx(0.045)
    assert row["rewards_daily_rate"] == pytest.approx(12.5)


def test_run_once_stores_moneylines_and_skips_other_types(cfg: Config, store: Store) -> None:
    result, calls = _run(cfg, store, fixture_json("gamma_markets_synthetic.json"))
    ids = [m.condition_id for m in result.markets]
    assert ids == ["0xcond_synthetic_b", "0xcond_synthetic_a"]
    assert len(result.changed) == 2
    assert store.count(s.markets) == 2
    assert all(r.headers["user-agent"] == cfg.http.user_agent for r in calls)
    market_calls = [r for r in calls if r.url.path == "/markets"]
    assert {r.url.params["sports_market_types"] for r in market_calls} == {"moneyline"}


def test_rerun_reports_only_changed_markets(cfg: Config, store: Store) -> None:
    markets = fixture_json("gamma_markets_synthetic.json")
    _run(cfg, store, markets)
    moved = copy.deepcopy(markets)
    moved[0]["gameStartTime"] = "2026-11-17 01:00:00+00"
    result, _ = _run(cfg, store, moved)
    assert [m.condition_id for m in result.changed] == ["0xcond_synthetic_a"]
    assert store.count(s.market_meta_history) == 3


def test_open_market_without_fee_schedule_fails_and_saves_payload(
    cfg: Config, store: Store
) -> None:
    raw = copy.deepcopy(fixture_json("gamma_markets_synthetic.json")[0])
    del raw["feeSchedule"]
    with pytest.raises(DiscoveryError, match="feeSchedule"):
        _run(cfg, store, [raw])
    saved = list((cfg.paths.data_dir / "failed_payloads").glob("*gamma_market.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text(encoding="utf-8"))["conditionId"] == raw["conditionId"]


def test_closed_market_never_seen_open_is_not_stored(cfg: Config, store: Store) -> None:
    raw = copy.deepcopy(fixture_json("gamma_markets_synthetic.json")[0])
    del raw["feeSchedule"]
    raw["closed"] = True
    result, _ = _run(cfg, store, [raw])
    assert result.markets == ()
    assert result.skipped[0][0] == raw["conditionId"]
    assert store.count(s.markets) == 0


def test_known_market_is_closed_with_its_winner_when_gamma_drops_the_fee_schedule(
    cfg: Config, store: Store
) -> None:
    """Live closed markets come back without feeSchedule and with final prices of 0 and 1."""
    raw = copy.deepcopy(fixture_json("gamma_markets_synthetic.json")[0])
    _run(cfg, store, [raw])
    del raw["feeSchedule"]
    raw.update(closed=True, acceptingOrders=False, outcomePrices='["0", "1"]')
    result, _ = _run(cfg, store, [raw])
    row = store.market(raw["conditionId"])
    assert row is not None
    assert (row["closed"], row["accepting_orders"], row["winning_token_id"]) == (True, False, "222")
    assert (row["fee_rate"], row["rebate_rate"], row["tick_size"]) == (0.05, 0.15, 0.001)
    assert [m.condition_id for m in result.changed] == [raw["conditionId"]]
    assert store.conditions_needing_backfill() == [raw["conditionId"]]


def test_home_and_away_fall_back_to_the_league_listing_order() -> None:
    raw = copy.deepcopy(fixture_json("gamma_markets_synthetic.json")[0])
    raw["events"] = [{"id": "e1", "title": "Hawks vs. Owls", "gameId": 77}]
    del raw["gameId"]
    market = GammaMarket.model_validate(raw)
    assert home_away(market, "away") == ("Owls", "Hawks")
    assert home_away(market, "home") == ("Hawks", "Owls")
    assert home_away(market, None) == (None, None)
    row = market_row(market, "nba", "away")
    assert (row["home"], row["away"], row["game_id"]) == ("Owls", "Hawks", "77")
    sports = [
        SportRow.model_validate({"id": 1, "sport": "NBA", "tags": "1,2", "ordering": "away"}),
        SportRow.model_validate({"id": 2, "sport": "nhl", "tags": "1,3"}),
    ]
    assert league_orderings(sports) == {"nba": "away", "nhl": None}


def test_reward_pool_is_read_from_the_clob_for_near_term_games_only(
    cfg: Config, store: Store
) -> None:
    markets = fixture_json("gamma_markets_synthetic.json")
    _, calls = _run(cfg, store, markets)
    rows = {r["condition_id"]: r for r in store.markets()}
    assert rows["0xcond_synthetic_b"]["rewards_daily_rate"] == 6, "only the config active today"
    assert rows["0xcond_synthetic_a"]["rewards_daily_rate"] == 0, "no config means no rewards"
    assert sum(r.url.path.startswith("/rewards/markets/") for r in calls) == 2

    far = copy.deepcopy(markets[:1])
    far[0]["conditionId"] = "0xcond_synthetic_far"
    far[0]["gameStartTime"] = "2026-12-20 00:30:00+00"
    _, calls = _run(cfg, store, far)
    assert not any(r.url.path.startswith("/rewards/markets/") for r in calls)
    row = store.market("0xcond_synthetic_far")
    assert row is not None and row["rewards_daily_rate"] == pytest.approx(12.5)


def test_renamed_field_fails_validation(cfg: Config, store: Store) -> None:
    raw = copy.deepcopy(fixture_json("gamma_markets_synthetic.json")[0])
    raw["clob_token_ids"] = raw.pop("clobTokenIds")
    with pytest.raises(Exception, match="GammaMarket"):
        _run(cfg, store, [raw])


@pytest.mark.parametrize(
    ("closed", "prices", "winner"),
    [
        (True, '["0", "1"]', "222"),
        (True, ["1", "0"], "111"),
        (True, '["0.5", "0.5"]', None),
        (True, '["0.999", "0.001"]', None),
        (False, '["1", "0"]', None),
        (True, None, None),
    ],
)
def test_winner_is_taken_only_from_a_closed_market_priced_exactly_zero_and_one(
    closed: bool, prices: Any, winner: str | None
) -> None:
    raw = copy.deepcopy(fixture_json("gamma_markets_synthetic.json")[0])
    raw["closed"] = closed
    raw["outcomePrices"] = prices
    market = GammaMarket.model_validate(raw)
    assert final_winner(market) == winner
    assert market_row(market, "nba")["winning_token_id"] == winner

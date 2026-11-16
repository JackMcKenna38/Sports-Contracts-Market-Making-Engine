from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from mm.config import Config
from mm.net import Http, PayloadError
from mm.recorder.backfill import BackfillError, TradeBackfill
from mm.recorder.odds import NoOddsKey, OddsClient, OddsEvent, OddsPoller, event_quotes
from mm.recorder.writer import Writer
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import MarketMeta
from tests.conftest import fixture_json

MIN = 60_000
START = 1_000 * MIN


def _meta(cid: str, start: int, league: str = "nba") -> MarketMeta:
    return MarketMeta(
        cid, league, f"{cid}y", f"{cid}n", "A", "B", None, start, 0.01, 5, 0.05, 1, 0.15,
        0.045, 50, 0, True, False,
    )  # fmt: skip


def test_event_quotes_keep_two_way_books_only() -> None:
    ev = OddsEvent.model_validate(fixture_json("odds_nba_synthetic.json")[0])
    quotes, skipped = event_quotes(ev, 123)
    assert [q.bookmaker for q in quotes] == ["pinnacle", "otherbook"]
    assert skipped == ["threeway"]
    q = quotes[0]
    assert (q.home_team, q.home_price, q.away_price, q.ts) == ("Synthetic Owls", 1.8, 2.1, 123)


def test_missing_key_refuses_to_build_client(cfg: Config) -> None:
    async def go() -> None:
        http = Http(cfg)
        try:
            with pytest.raises(NoOddsKey):
                OddsClient(cfg, http, None)
            with pytest.raises(NoOddsKey):
                OddsClient(cfg, http, SecretStr(""))
        finally:
            await http.aclose()

    asyncio.run(go())


class _Odds:
    def __init__(self, remaining: float | None = None) -> None:
        self.remaining = remaining
        self.calls: list[str] = []

    async def odds(self, sport: str) -> list[OddsEvent]:
        self.calls.append(sport)
        return [OddsEvent.model_validate(e) for e in fixture_json("odds_nba_synthetic.json")]


def _poller(cfg: Config, store: Store, client: _Odds, markets: list[MarketMeta]) -> OddsPoller:
    return OddsPoller(cfg, client, Writer(cfg, store), lambda: markets)  # type: ignore[arg-type]


def test_schedule_polls_on_new_game_then_at_offsets(cfg: Config, store: Store) -> None:
    client = _Odds()
    poller = _poller(cfg, store, client, [_meta("a", START)])
    assert asyncio.run(poller.run_once(START - 600 * MIN)) == 2
    assert asyncio.run(poller.run_once(START - 500 * MIN)) == 0
    assert asyncio.run(poller.run_once(START - 360 * MIN)) == 2
    assert asyncio.run(poller.run_once(START - 61 * MIN)) == 0
    assert asyncio.run(poller.run_once(START - 60 * MIN)) == 2
    assert asyncio.run(poller.run_once(START + MIN)) == 0
    assert client.calls == ["basketball_nba"] * 3


def test_low_credits_use_reduced_schedule_and_zero_stops(cfg: Config, store: Store) -> None:
    client = _Odds(remaining=cfg.odds.min_credits_reserve)
    poller = _poller(cfg, store, client, [_meta("a", START)])
    assert poller.due(START - 600 * MIN) == {}
    assert poller.due(START - 300 * MIN) == {}
    assert poller.due(START - 60 * MIN) == {"nba": [("a", "T-60")]}
    client.remaining = 1
    assert poller.due(START - 30 * MIN) == {}
    assert poller.stopped


def test_interval_mode(cfg: Config, store: Store) -> None:
    cfg = cfg.model_copy(update={"odds": cfg.odds.model_copy(update={"interval_minutes": 30})})
    client = _Odds()
    poller = _poller(cfg, store, client, [_meta("a", START)])
    asyncio.run(poller.run_once(START - 100 * MIN))
    asyncio.run(poller.run_once(START - 80 * MIN))
    asyncio.run(poller.run_once(START - 70 * MIN))
    assert client.calls == ["basketball_nba", "basketball_nba"]


def test_odds_client_sends_key_and_logs_credits(cfg: Config) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=fixture_json("odds_nba_synthetic.json"),
            headers={"x-requests-remaining": "480", "x-requests-used": "20"},
        )

    async def go() -> OddsClient:
        http = Http(cfg, transport=httpx.MockTransport(handler))
        client = OddsClient(cfg, http, SecretStr("test-key"))
        events = await client.odds("basketball_nba")
        await http.aclose()
        assert len(events) == 1
        return client

    client = asyncio.run(go())
    assert client.remaining == 480
    params = seen[0].url.params
    assert (params["apiKey"], params["regions"], params["markets"]) == ("test-key", "us,eu", "h2h")
    assert seen[0].url.path == "/v4/sports/basketball_nba/odds"


def _trade(ts: int, tx: str = "0xtx") -> dict[str, Any]:
    return {
        "proxy_wallet": "0xsynthetic",
        "name": "synthetic",
        "side": "BUY",
        "token_id": "111",
        "condition_id": "c1",
        "size": 10,
        "price": 0.5,
        "timestamp": ts,
        "transaction_hash": tx,
    }


def _page(rows: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    pagination = {"limit": 2, "offset": 0, "has_more": cursor is not None, "next_cursor": cursor}
    return {"data": rows, "pagination": pagination}


def _backfill(
    cfg: Config, store: Store, payload: Any, seen: list[httpx.Request] | None = None
) -> TradeBackfill:
    pages = payload if isinstance(payload, list) else [payload]

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        cursor = request.url.params.get("cursor")
        return httpx.Response(200, json=pages[int(cursor) if cursor else 0])

    return TradeBackfill(cfg, Http(cfg, transport=httpx.MockTransport(handler)), store)


def test_backfill_converts_seconds_and_drops_wallets(cfg: Config, store: Store) -> None:
    rows = asyncio.run(_backfill(cfg, store, _page([_trade(1_763_000_000)])).fetch("c1"))
    assert rows[0]["ts"] == 1_763_000_000_000
    assert set(rows[0]) == {
        "ts", "ts_recv", "condition_id", "token_id", "side", "price", "size", "tx_hash", "source"
    }  # fmt: skip
    assert (rows[0]["token_id"], rows[0]["tx_hash"], rows[0]["source"]) == (
        "111",
        "0xtx",
        "data_api",
    )


def test_backfill_follows_cursors_until_the_last_page(cfg: Config, store: Store) -> None:
    seen: list[httpx.Request] = []
    pages = [
        _page([_trade(1_763_000_300, "c"), _trade(1_763_000_200, "b")], cursor="1"),
        _page([_trade(1_763_000_100, "a")]),
    ]
    rows = asyncio.run(_backfill(cfg, store, pages, seen).fetch("c1"))
    assert [r["tx_hash"] for r in rows] == ["a", "b", "c"], "oldest first"
    first, second = (dict(r.url.params) for r in seen)
    assert first == {"condition": "c1", "limit": str(cfg.recorder.trade_backfill_limit)}
    assert second == {"condition": "c1", "cursor": "1"}


def test_backfill_refuses_unexpected_shapes(cfg: Config, store: Store) -> None:
    with pytest.raises(PayloadError, match="TradesPage"):
        asyncio.run(_backfill(cfg, store, [_trade(1_763_000_000)][0:0] or {"rows": []}).fetch("c1"))
    with pytest.raises(PayloadError, match="has_more without a next_cursor"):
        bad = _page([_trade(1_763_000_000)])
        bad["pagination"]["has_more"] = True
        asyncio.run(_backfill(cfg, store, bad).fetch("c1"))
    with pytest.raises(PayloadError, match="TradesPage"):
        asyncio.run(_backfill(cfg, store, _page([_trade(1_763_000_000_000)])).fetch("c1"))
    with pytest.raises(PayloadError, match="other markets"):
        asyncio.run(_backfill(cfg, store, _page([_trade(1_763_000_000)])).fetch("c2"))


def test_backfill_refuses_a_partial_history(cfg: Config, store: Store) -> None:
    cfg = cfg.model_copy(
        update={"recorder": cfg.recorder.model_copy(update={"trade_backfill_max_pages": 2})}
    )
    endless = [_page([_trade(1_763_000_000)], cursor="1"), _page([_trade(1_763_000_001)], "1")]
    with pytest.raises(BackfillError, match="more than 2 pages"):
        asyncio.run(_backfill(cfg, store, endless).fetch("c1"))


def test_backfill_run_once_targets_closed_markets(cfg: Config, store: Store) -> None:
    from mm.discovery.gamma import market_row
    from mm.discovery.models import GammaMarket

    raw = fixture_json("gamma_markets_synthetic.json")[0] | {"conditionId": "c1", "closed": True}
    store.upsert_market(market_row(GammaMarket.model_validate(raw), "nba"), 1)
    bf = _backfill(cfg, store, _page([_trade(1_763_000_000)]))
    assert asyncio.run(bf.run_once()) == 1
    assert store.count(s.trades) == 1
    assert store.conditions_needing_backfill() == []

"""The whole recorder, and paper trading on top of it, against scripted fakes. No network."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from mm.config import Config
from mm.net import Http, PayloadError
from mm.paper.trader import PaperTrader
from mm.recorder.service import Recorder, RecorderAlreadyRunning
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import Event, Gap, MetaChange, OddsQuote, now_ms
from tests.conftest import fixture_json
from tests.unit.fake_ws import FakeWs

CID = "0xcond_synthetic_a"
HOUR = 3_600_000


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fakes:
    """Scripted Gamma, CLOB, Data API, Odds API and geoblock responses plus both WebSockets."""

    def __init__(self, cfg: Config, *, blocked: bool = False) -> None:
        self.cfg = cfg
        self.now = now_ms()
        self.start = self.now + 2 * HOUR
        self.blocked = blocked
        self.requests: list[httpx.Request] = []
        self.sockets: dict[str, list[FakeWs]] = {"market": [], "sports": []}
        market = copy.deepcopy(fixture_json("gamma_markets_synthetic.json")[0])
        market["gameStartTime"] = _iso(self.start)
        self.markets = [market]
        odds = copy.deepcopy(fixture_json("odds_nba_synthetic.json"))
        odds[0]["commence_time"] = _iso(self.start)
        self.odds = odds

    def http(self) -> Http:
        return Http(self.cfg, transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path, q = request.url.host, request.url.path, request.url.params
        if host == "gamma-api.polymarket.com":
            if path == "/sports":
                return httpx.Response(200, json=fixture_json("gamma_sports_synthetic.json"))
            hit = path == "/markets" and q["tag_id"] == "7450" and q["closed"] == "false"
            return httpx.Response(200, json=self.markets if hit else [])
        if host == "clob.polymarket.com" and path.startswith("/rewards/markets/"):
            return httpx.Response(200, json={"data": []})
        if host == "clob.polymarket.com" and path == "/books":
            return httpx.Response(
                200, json=[self._rest_book(b["token_id"]) for b in json.loads(request.content)]
            )
        if host == "api.the-odds-api.com":
            assert q["apiKey"] == "synthetic-key"
            body = self.odds if "basketball_nba" in path else []
            return httpx.Response(200, json=body, headers={"x-requests-remaining": "480"})
        if host == "polymarket.com":
            return httpx.Response(200, json={"blocked": self.blocked, "country": "ZZ"})
        if host == "data-api.polymarket.com":
            page = {"data": [], "pagination": {"limit": 1000, "offset": 0, "has_more": False}}
            return httpx.Response(200, json=page)
        return httpx.Response(404, json={"error": f"unexpected {request.url}"})

    def _rest_book(self, token: str) -> dict[str, Any]:
        return {
            "market": CID, "asset_id": token, "timestamp": str(now_ms()), "hash": f"rest-{token}",
            "bids": [{"price": "0.43", "size": "100"}], "asks": [{"price": "0.50", "size": "100"}],
            "min_order_size": "5", "tick_size": "0.001", "neg_risk": False,
        }  # fmt: skip

    def _market_script(self) -> list[str | Exception]:
        ts = str(now_ms())
        book = {
            "event_type": "book", "asset_id": "111", "market": CID, "timestamp": ts,
            "bids": [{"price": "0.43", "size": "100"}], "asks": [{"price": "0.50", "size": "100"}],
            "hash": "h0",
        }  # fmt: skip
        change = {
            "event_type": "price_change", "market": CID, "timestamp": ts,
            "price_changes": [
                {"asset_id": "111", "price": "0.42", "size": "70", "side": "BUY", "hash": "h1"}
            ],
        }  # fmt: skip
        trade = {
            "event_type": "last_trade_price", "asset_id": "111", "market": CID, "price": "0.50",
            "side": "BUY", "size": "10", "timestamp": ts,
        }  # fmt: skip
        return [json.dumps([book]), json.dumps(change), json.dumps(trade), "{broken"]

    def connect(self, url: str) -> AbstractAsyncContextManager[FakeWs]:
        if url == self.cfg.endpoints.clob_ws:
            kind, script = "market", self._market_script()
        else:
            game = {"gameId": "g-1", "leagueAbbreviation": "nba", "status": "Scheduled",
                    "live": False, "ended": False}  # fmt: skip
            kind, script = "sports", ["ping", json.dumps(game)]
        ws = FakeWs(script)
        self.sockets[kind].append(ws)

        @asynccontextmanager
        async def cm() -> AsyncIterator[FakeWs]:
            yield ws

        return cm()


@pytest.fixture
def live_cfg(cfg: Config, tmp_path: Path) -> Config:
    aliases = tmp_path / "aliases.yaml"
    aliases.write_text("nba:\n  Synthetic Owls: [Owls]\n  Synthetic Hawks: [Hawks]\nnhl: {}\n")
    return cfg.model_copy(
        update={
            "matching": cfg.matching.model_copy(
                update={"aliases_file": aliases, "review_file": tmp_path / "review.csv"}
            ),
            "recorder": cfg.recorder.model_copy(
                update={
                    "writer_flush_seconds": 0.02,
                    "rest_snapshot_seconds": 0.05,
                    "heartbeat_seconds": 0.02,
                    "market_ws_ping_seconds": 0.02,
                }
            ),
            "odds": cfg.odds.model_copy(update={"check_seconds": 0.02}),
            "sim": cfg.sim.model_copy(update={"latency_ms": 0, "tick_seconds": 0.02}),
            "paper": cfg.paper.model_copy(update={"flush_seconds": 0.02}),
        }
    )


def _record(cfg: Config, store: Store, fakes: Fakes, seconds: float, **kwargs: Any) -> Recorder:
    async def go() -> Recorder:
        http = fakes.http()
        recorder = Recorder(cfg, store, http, connector=fakes.connect, **kwargs)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(recorder.run(), seconds)
        finally:
            await http.aclose()
        return recorder

    return asyncio.run(go())


def test_recorder_fills_every_table_and_publishes_every_event_kind(
    live_cfg: Config, store: Store
) -> None:
    fakes = Fakes(live_cfg)
    events: list[Event] = []
    recorder = _record(
        live_cfg, store, fakes, 1.5, odds_key=SecretStr("synthetic-key"), listeners=[events.append]
    )

    counts = {t.name: store.count(t) for t in s.metadata.sorted_tables}
    for table in ("markets", "market_meta_history", "book_snapshots", "book_deltas", "trades",
                  "odds_snapshots", "game_status", "market_odds_map", "recorder_heartbeat",
                  "quarantine"):  # fmt: skip
        assert counts[table] > 0, f"{table}: {counts}"
    assert {r["source"] for r in store.iter_book_snapshots(0, 2**62, "rest")} == {"rest"}
    assert counts["book_snapshots"] > 3, "REST checkpoints keep arriving on a schedule"
    assert recorder.feed.tokens == ["111", "222"]
    assert recorder.feed.quarantined == 1

    mapping = store.mappings(confirmed_only=True)
    assert [(m["condition_id"], m["odds_event_id"], m["yes_is_home"]) for m in mapping] == [
        (CID, "ev_synthetic_1", False)
    ]
    assert {q["bookmaker"] for q in store.iter_odds(0, 2**62)} == {"pinnacle", "otherbook"}

    kinds = {type(e).__name__ for e in events}
    assert {"MetaChange", "BookSnapshot", "BookDelta", "Trade", "GameStatus", "OddsQuote"} <= kinds
    assert any(isinstance(e, MetaChange) and e.meta.condition_id == CID for e in events)
    assert sum(isinstance(e, OddsQuote) for e in events) == 2, "one poll for the new game"

    subscribe = json.loads(fakes.sockets["market"][0].sent[0])
    assert subscribe["assets_ids"] == ["111", "222"]
    assert "PING" in fakes.sockets["market"][0].sent
    assert fakes.sockets["sports"][0].sent == ["pong"]
    assert all(r.headers["user-agent"] == live_cfg.http.user_agent for r in fakes.requests)
    assert not any(r.method not in ("GET", "POST") for r in fakes.requests)
    assert {r.url.path for r in fakes.requests if r.method == "POST"} == {"/books"}


def test_recorder_without_an_odds_key_records_everything_else(
    live_cfg: Config, store: Store
) -> None:
    fakes = Fakes(live_cfg, blocked=True)
    recorder = _record(live_cfg, store, fakes, 0.8)
    assert recorder.odds is None
    assert store.count(s.odds_snapshots) == 0
    assert store.count(s.book_snapshots) > 0
    assert store.mappings() == []
    assert not any(r.url.host == "api.the-odds-api.com" for r in fakes.requests)


def test_restart_closes_gaps_left_open_by_the_previous_run(live_cfg: Config, store: Store) -> None:
    gap = store.open_gap("market_ws", None, 1, "error")
    _record(live_cfg, store, Fakes(live_cfg), 0.5)
    (row,) = [g for g in store.gaps(0, 2**62) if g["id"] == gap]
    assert row["ended_ts"] is not None


def test_paper_trading_quotes_on_live_events_end_to_end(live_cfg: Config, store: Store) -> None:
    """Discovery, matching, odds, books and ticks flow through to simulated resting quotes."""
    fakes = Fakes(live_cfg)
    trader = PaperTrader(live_cfg, store, "baseline", latency_ms=0, sha="abc1234")
    gaps: list[Gap] = []

    def listener(event: Event) -> None:
        if isinstance(event, Gap):
            gaps.append(event)
        trader.on_event(event)

    async def go() -> dict[str, Any]:
        http = fakes.http()
        recorder = Recorder(
            live_cfg, store, http, odds_key=SecretStr("synthetic-key"),
            connector=fakes.connect, listeners=[listener],
        )  # fmt: skip
        try:
            return await trader.run(recorder, dashboard=False, duration=2.0)
        finally:
            await http.aclose()

    summary = asyncio.run(go())
    state = trader.engine.markets[CID]
    assert (state.event_id, state.yes_is_home) == ("ev_synthetic_1", False)
    assert state.fair == pytest.approx(0.4625, abs=0.01)
    assert summary["markets"] == 1

    placed = [o for o in store.run_rows(s.sim_orders, trader.run_id) if o["action"] == "place"]
    assert {o["side"] for o in placed} == {"BUY", "SELL"}
    for order in placed:
        assert 0.43 <= order["price"] <= 0.50 or order["side"] == "SELL"
        assert order["token_id"] == "111"
    bids = [o["price"] for o in placed if o["side"] == "BUY"]
    asks = [o["price"] for o in placed if o["side"] == "SELL"]
    assert max(bids) < state.fair < min(asks)
    assert store.run_rows(s.pnl_ledger, trader.run_id)
    run = store.run(trader.run_id)
    assert run is not None and run["mode"] == "paper" and run["finished"] is not None
    assert run["data_end"] > run["data_start"]
    assert not any(r.method not in ("GET", "POST") for r in fakes.requests)


def test_second_recorder_on_the_same_database_refuses_to_start(
    live_cfg: Config, store: Store
) -> None:
    """`mm paper` embeds a recorder; beside `mm record` it would double every trade."""
    fakes = Fakes(live_cfg)
    store.init()
    store.set_heartbeat(now_ms() - 5_000)

    async def go() -> None:
        http = fakes.http()
        try:
            await Recorder(live_cfg, store, http, connector=fakes.connect).run()
        finally:
            await http.aclose()

    with pytest.raises(RecorderAlreadyRunning, match="twice"):
        asyncio.run(go())
    assert fakes.requests == [], "it must refuse before touching the network or the database"
    assert store.count(s.markets) == 0

    stale = now_ms() - int(live_cfg.recorder.heartbeat_stale_seconds * 1000) - 1
    store.set_heartbeat(stale)
    _record(live_cfg, store, Fakes(live_cfg), 0.5)
    assert store.count(s.markets) == 1


def test_discovery_outage_keeps_recording_but_a_changed_schema_stops_the_run(
    live_cfg: Config, store: Store
) -> None:
    cfg = live_cfg.model_copy(
        update={
            "discovery": live_cfg.discovery.model_copy(update={"refresh_minutes": 0.002}),
            "http": live_cfg.http.model_copy(update={"max_retries": 0}),
        }
    )
    fakes = Fakes(cfg)
    real = fakes._handle
    mode = {"gamma": "ok"}

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gamma-api.polymarket.com" and request.url.path == "/sports":
            if mode["gamma"] == "down":
                return httpx.Response(503, text="synthetic outage")
            if mode["gamma"] == "changed":
                return httpx.Response(200, json=[{"sport": "nba"}])
        return real(request)

    fakes._handle = flaky  # type: ignore[method-assign]

    async def go() -> int:
        http = fakes.http()
        recorder = Recorder(cfg, store, http, connector=fakes.connect)
        task = asyncio.create_task(recorder.run())
        try:
            await asyncio.sleep(0.4)
            mode["gamma"] = "down"
            await asyncio.sleep(0.6)
            assert not task.done(), "an outage must not stop the recorder"
            before = store.count(s.book_snapshots)
            await asyncio.sleep(0.3)
            assert store.count(s.book_snapshots) > before, "books are still being recorded"
            mode["gamma"] = "changed"
            with pytest.raises(PayloadError, match="SportRow"):
                await asyncio.wait_for(task, 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await http.aclose()
        return store.count(s.markets)

    assert asyncio.run(go()) == 1

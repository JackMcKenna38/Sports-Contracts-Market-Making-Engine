from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from mm.config import Config
from mm.net import Http
from mm.recorder.market_ws import MarketFeed
from mm.recorder.service import wanted_tokens
from mm.recorder.snapshots import RestBooks
from mm.recorder.sports_ws import SportsFeed
from mm.recorder.writer import Writer
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import BookSnapshot, Event, GameStatus, MarketMeta
from tests.conftest import fixture_json
from tests.unit.fake_ws import Drop, FakeServer


def _fast(cfg: Config, **recorder: Any) -> Config:
    rec = cfg.recorder.model_copy(
        update={
            "market_ws_ping_seconds": 0.01,
            "reconnect_base_seconds": 0.001,
            "reconnect_max_seconds": 0.002,
            **recorder,
        }
    )
    return cfg.model_copy(update={"recorder": rec})


def _ws_messages() -> list[str]:
    return [json.dumps(m) for m in fixture_json("market_ws_synthetic.json")]


def test_market_feed_records_reconnects_and_logs_gap(cfg: Config, store: Store) -> None:
    cfg = _fast(cfg)
    events: list[Event] = []
    snapshot_calls: list[list[str]] = []
    feed_resolved: list[tuple[str, str]] = []

    async def go() -> None:
        server = FakeServer([[*_ws_messages(), "PONG", Drop("reset by peer")]])
        writer = Writer(cfg, store)
        feed = MarketFeed(cfg, store, writer, connector=server.connect, listeners=[events.append])

        async def snap(tokens: Sequence[str]) -> int:
            snapshot_calls.append(list(tokens))
            return 0

        feed.snapshot = snap
        feed.set_tokens(["111", "222"])
        feed.start()
        await asyncio.wait_for(server.connected.wait(), 5)
        await feed.stop()
        writer.flush()
        feed_resolved.extend(feed.resolved)
        sub = json.loads(server.conns[0].sent[0])
        assert sub["assets_ids"] == ["111", "222"]
        assert sub["custom_feature_enabled"] is True

    asyncio.run(go())
    assert snapshot_calls == [["111", "222"]]
    assert feed_resolved == [("0xcond_synthetic_a", "111")]
    assert store.count(s.book_snapshots) == 1
    assert store.count(s.book_deltas) == 2
    assert store.count(s.trades) == 1
    assert store.count(s.market_events) == 2
    gaps = store.gaps(0, 2**62)
    assert len(gaps) == 1
    assert gaps[0]["reason"] == "error"
    assert "reset by peer" in gaps[0]["detail"]
    assert gaps[0]["ended_ts"] is None
    kinds = [type(e).__name__ for e in events]
    assert kinds == [
        "BookSnapshot",
        "BookDelta",
        "BookDelta",
        "Trade",
        "TickSizeChange",
        "MarketResolved",
        "Gap",
    ]


def test_market_feed_sends_ping(cfg: Config, store: Store) -> None:
    cfg = _fast(cfg)

    async def go() -> list[str]:
        server = FakeServer([[]])
        feed = MarketFeed(cfg, store, Writer(cfg, store), connector=server.connect)
        feed.set_tokens(["111"])
        feed.start()
        await asyncio.sleep(0.1)
        await feed.stop()
        return server.conns[0].sent

    sent = asyncio.run(go())
    assert "PING" in sent[1:]


def test_quarantine_opens_token_gap_until_next_book(cfg: Config, store: Store) -> None:
    writer = Writer(cfg, store)
    feed = MarketFeed(cfg, store, writer)
    bad = {"event_type": "book", "asset_id": "111", "market": "m", "timestamp": "x"}
    feed.handle_text(json.dumps(bad), 500)
    feed.handle_text("not json", 501)
    assert feed.quarantined == 2
    feed.handle_text(_ws_messages()[0], 1000)
    writer.flush()
    assert store.count(s.quarantine) == 2
    gaps = store.gaps(0, 2**62)
    assert [(g["token_id"], g["reason"], g["started_ts"], g["ended_ts"]) for g in gaps] == [
        ("111", "quarantine", 500, 1000)
    ]


def _rest(ts: int, bids: tuple[tuple[float, float], ...], h: str) -> BookSnapshot:
    return BookSnapshot(ts, ts + 5, "111", bids, ((0.52, 80.0),), h, "rest")


def test_reconcile_detects_mismatch_on_equal_hash(cfg: Config, store: Store) -> None:
    writer = Writer(cfg, store)
    feed = MarketFeed(cfg, store, writer)
    feed.handle_text(_ws_messages()[0], 1000)
    feed.reconcile(_rest(1500, ((0.48, 100.0), (0.47, 50.0)), "h0"))
    assert feed.mismatches == 1
    assert feed.books["111"].levels("SELL") == ((0.52, 80.0),)
    writer.flush()
    assert [g["reason"] for g in store.gaps(0, 2**62)] == ["lost_update"]


def test_reconcile_adopts_newer_rest_book(cfg: Config, store: Store) -> None:
    feed = MarketFeed(cfg, store, Writer(cfg, store))
    feed.handle_text(_ws_messages()[0], 1000)
    feed.reconcile(_rest(2000, ((0.30, 1.0),), "other"))
    assert feed.mismatches == 0
    assert feed.books["111"].best_bid() == 0.30
    feed.reconcile(_rest(900, ((0.10, 1.0),), "older"))
    assert feed.books["111"].best_bid() == 0.30


def _change(ts: int, price: str, size: str) -> str:
    entry = {"asset_id": "111", "price": price, "size": size, "side": "BUY", "hash": f"h{ts}"}
    message = {"event_type": "price_change", "market": "m", "timestamp": str(ts)}
    return json.dumps({**message, "price_changes": [entry]})


def test_rest_book_ahead_of_an_in_flight_delta_is_not_a_lost_update(
    cfg: Config, store: Store
) -> None:
    writer = Writer(cfg, store)
    feed = MarketFeed(cfg, store, writer)
    feed.handle_text(_ws_messages()[0], 1000)
    ahead = ((0.48, 100.0), (0.47, 50.0), (0.46, 9.0))
    feed.reconcile(_rest(2000, ahead, "r1"))
    feed.handle_text(_change(2000, "0.46", "9"), 2100)
    feed.reconcile(_rest(2000, ahead, "r1"))
    writer.flush()
    assert feed.mismatches == 0 and store.gaps(0, 2**62) == []


def test_change_the_stream_never_delivers_is_logged_at_the_next_checkpoint(
    cfg: Config, store: Store
) -> None:
    """Seen live: a level changes in the REST book and no delta for it ever arrives."""
    writer = Writer(cfg, store)
    events: list[Event] = []
    feed = MarketFeed(cfg, store, writer, listeners=[events.append])
    feed.handle_text(_ws_messages()[0], 1000)
    changed = ((0.48, 60.0), (0.47, 50.0))
    first = _rest(2000, changed, "r1")
    feed.reconcile(first)
    assert feed.mismatches == 0, "it may still be in flight"
    assert feed.books["111"].size_at("BUY", 0.48) == 60, "the REST book is adopted at once"
    feed.handle_text(_change(60_000, "0.45", "5"), 60_050)
    feed.reconcile(_rest(60_000, (*changed, (0.45, 5.0)), "h60000"))
    writer.flush()
    assert feed.mismatches == 1
    gaps = store.gaps(0, 2**62)
    assert [(g["token_id"], g["reason"], g["started_ts"]) for g in gaps] == [
        ("111", "lost_update", first.recv)
    ]
    assert sum(isinstance(e, BookSnapshot) and e.source == "rest" for e in events) == 1


def test_tokens_are_spread_over_connections(cfg: Config, store: Store) -> None:
    cfg = _fast(cfg, max_assets_per_connection=2)
    feed = MarketFeed(cfg, store, Writer(cfg, store))
    added, removed = feed.set_tokens(["a", "b", "c", "d", "e"])
    assert (added, removed) == (["a", "b", "c", "d", "e"], [])
    assert len(feed._conns) == 3
    added, removed = feed.set_tokens(["a", "c", "f"])
    assert (added, removed) == (["f"], ["b", "d", "e"])
    assert feed.tokens == ["a", "c", "f"]


def test_rest_books_posts_batches_and_reconciles(cfg: Config, store: Store) -> None:
    cfg = _fast(cfg, books_batch_size=2)
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json=[
                {"market": "m", "asset_id": b["token_id"], "timestamp": "10", "hash": "h"}
                | {"bids": [{"price": "0.4", "size": "1"}], "asks": []}
                for b in body
            ],
        )

    async def go() -> int:
        http = Http(cfg, transport=httpx.MockTransport(handler))
        writer = Writer(cfg, store)
        feed = MarketFeed(cfg, store, writer)
        n = await RestBooks(cfg, http, writer, feed).snapshot(["1", "2", "3"])
        writer.flush()
        await http.aclose()
        assert feed.books["3"].best_bid() == 0.4
        return n

    assert asyncio.run(go()) == 3
    assert bodies == [[{"token_id": "1"}, {"token_id": "2"}], [{"token_id": "3"}]]
    assert store.count(s.book_snapshots) == 3


def test_sports_feed_answers_ping_and_records_games(cfg: Config, store: Store) -> None:
    cfg = _fast(cfg)
    game = {
        "gameId": "g1",
        "leagueAbbreviation": "NBA",
        "status": "Scheduled",
        "live": False,
        "ended": False,
    }
    other = game | {"gameId": "g2", "leagueAbbreviation": "EPL"}

    async def go() -> list[str]:
        server = FakeServer([["ping", json.dumps([game, other]), "{}", Drop("gone")]])
        writer = Writer(cfg, store)
        feed = SportsFeed(cfg, store, writer, connector=server.connect)
        task = asyncio.get_running_loop().create_task(feed.run())
        await asyncio.wait_for(server.connected.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        writer.flush()
        return server.conns[0].sent

    assert asyncio.run(go()) == ["pong"]
    assert store.count(s.game_status) == 1
    assert store.count(s.quarantine) == 1
    assert [g["stream"] for g in store.gaps(0, 2**62)] == ["sports_ws"]


def _meta(cid: str, start: int | None, closed: bool = False) -> MarketMeta:
    return MarketMeta(
        cid, "nba", f"{cid}y", f"{cid}n", "A", "B", None, start, 0.01, 5, 0.05, 1, 0.15,
        0.045, 50, 0, True, closed,
    )  # fmt: skip


def test_wanted_tokens_drop_closed_and_long_finished_games() -> None:
    hour = 3_600_000
    markets = [_meta("a", 10 * hour), _meta("b", None), _meta("c", 0), _meta("d", 9 * hour, True)]
    assert wanted_tokens(markets, 10 * hour, 6) == {"ay", "an", "by", "bn"}


def test_wanted_tokens_can_wait_until_a_game_is_near() -> None:
    hour = 3_600_000
    markets = [_meta("soon", 40 * hour), _meta("later", 60 * hour), _meta("undated", None)]
    assert wanted_tokens(markets, 0, 6, None) == {"soony", "soonn", "latery", "latern",
                                                  "undatedy", "undatedn"}  # fmt: skip
    assert wanted_tokens(markets, 0, 6, 48) == {"soony", "soonn", "undatedy", "undatedn"}
    assert wanted_tokens(markets, 13 * hour, 6, 48) >= {"latery", "latern"}


def _feed(cfg: Config, store: Store, events: list[Event]) -> tuple[MarketFeed, Writer]:
    writer = Writer(cfg, store)
    feed = MarketFeed(cfg, store, writer, listeners=[events.append])
    feed.set_tokens(["111", "222"])
    return feed, writer


def test_exchange_wide_broadcasts_are_ignored_not_quarantined(cfg: Config, store: Store) -> None:
    """Seen live: `new_market` and other markets' resolutions arrive on every connection."""
    events: list[Event] = []
    feed, writer = _feed(cfg, store, events)
    new_market = {
        "event_type": "new_market", "id": "7096148", "question": "Synthetic?",
        "market": "0xother", "slug": "synthetic", "assets_ids": ["901", "902"],
        "outcomes": ["Up", "Down"], "timestamp": "1794845000000",
    }  # fmt: skip
    foreign = {
        "event_type": "market_resolved", "id": "x", "market": "0xother",
        "assets_ids": ["901", "902"], "winning_asset_id": "901", "winning_outcome": "Up",
        "timestamp": "1794845000000",
    }  # fmt: skip
    ours = {**foreign, "market": "0xcond_synthetic_a", "assets_ids": ["111", "222"],
            "winning_asset_id": "222"}  # fmt: skip
    for item in (new_market, foreign, ours):
        feed.handle_item(item, 1_794_845_000_100)
    writer.flush()
    assert feed.quarantined == 0 and store.count(s.quarantine) == 0
    assert feed.resolved == [("0xcond_synthetic_a", "222")]
    assert store.count(s.market_events) == 1
    assert [type(e).__name__ for e in events] == ["MarketResolved"]


def test_trade_hash_is_kept_and_old_book_dumps_do_not_raise_a_clock_alarm(
    cfg: Config, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    events: list[Event] = []
    feed, writer = _feed(cfg, store, events)
    recv = 1_794_845_000_000
    book = {
        "event_type": "book", "asset_id": "111", "market": "0xcond_synthetic_a",
        "timestamp": str(recv - 170_000), "bids": [{"price": "0.48", "size": "10"}],
        "asks": [], "hash": "h0", "tick_size": "0.01", "last_trade_price": "",
    }  # fmt: skip
    trade = {
        "event_type": "last_trade_price", "asset_id": "222", "market": "0xcond_synthetic_a",
        "price": "0.77", "size": "9.090908", "fee_rate_bps": "0", "side": "BUY",
        "timestamp": str(recv - 60), "transaction_hash": "0xsynthetic",
    }  # fmt: skip
    with caplog.at_level("WARNING"):
        feed.handle_item(book, recv)
        feed.handle_item(trade, recv)
    writer.flush()
    assert "NTP" not in caplog.text, "a quiet book's dump is old by design"
    (row,) = store.iter_trades(0, 2**62)
    assert (row["token_id"], row["side"], row["price"], row["tx_hash"]) == (
        "222",
        "BUY",
        0.77,
        "0xsynthetic",
    )


def test_sports_frames_of_other_leagues_are_skipped_before_validation(
    cfg: Config, store: Store
) -> None:
    """Seen live: cricket frames carry `metadataGameId` and no `gameId`."""
    events: list[Event] = []
    writer = Writer(cfg, store)
    feed = SportsFeed(cfg, store, writer, listeners=[events.append])
    cricket = {"metadataGameId": "id27", "leagueAbbreviation": "cricket", "score": "0-0",
               "period": "Scheduled", "live": False, "ended": False}  # fmt: skip
    esports = {"gameId": 1813237, "leagueAbbreviation": "cs2", "homeTeam": "A", "awayTeam": "B",
               "status": "running", "score": "000-000|0-0|Bo3", "period": "1/3", "live": True,
               "ended": False}  # fmt: skip
    nba = {"gameId": 20024122, "leagueAbbreviation": "NBA", "homeTeam": "Celtics",
           "awayTeam": "Magic", "score": "55-50", "period": "Q2", "live": True}  # fmt: skip
    broken = {"leagueAbbreviation": "nhl", "score": "1-0"}
    for frame in (cricket, esports, nba, broken):
        feed.handle_text(json.dumps(frame), 1_794_845_000_000)
    writer.flush()
    assert feed.quarantined == 1, "only the malformed frame from one of our leagues"
    (game,) = events
    assert isinstance(game, GameStatus)
    assert (game.game_id, game.league, game.live, game.ended, game.status) == (
        "20024122",
        "nba",
        True,
        False,
        "",
    )

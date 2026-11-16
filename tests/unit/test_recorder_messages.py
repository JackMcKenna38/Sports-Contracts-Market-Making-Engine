from __future__ import annotations

import pytest
from pydantic import ValidationError

from mm.recorder.book import LocalBook
from mm.recorder.messages import (
    MARKET_MSG,
    BookMsg,
    GameMsg,
    LastTradeMsg,
    PriceChangeMsg,
    ResolvedMsg,
    RestBook,
    TickSizeMsg,
    market_subscribe,
)
from tests.conftest import fixture_json


def test_market_messages_parse_and_rebuild_book() -> None:
    msgs = [MARKET_MSG.validate_python(m) for m in fixture_json("market_ws_synthetic.json")]
    book_msg, change, trade, tick, _bba, resolved = msgs
    assert isinstance(book_msg, BookMsg)
    assert isinstance(change, PriceChangeMsg)
    assert isinstance(trade, LastTradeMsg)
    assert isinstance(tick, TickSizeMsg)
    assert isinstance(resolved, ResolvedMsg)
    book = LocalBook("111")
    book.apply_snapshot(book_msg.to_event(recv=1005))
    for delta in change.to_events(recv=1110):
        book.apply_delta(delta)
    assert book.levels("BUY") == ((0.49, 30), (0.48, 100), (0.47, 50))
    assert book.levels("SELL") == ((0.52, 80),)
    assert book.hash == "h2"
    t = trade.to_event(recv=1201)
    assert (t.ts, t.recv, t.side, t.price, t.size) == (1200, 1201, "BUY", 0.52, 20)
    assert tick.to_event().new_tick_size == 0.001
    assert resolved.to_event().winning_token_id == "111"


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MARKET_MSG.validate_python({"event_type": "brand_new", "timestamp": "1"})


def test_rest_book_parses() -> None:
    raw = {
        "market": "m",
        "asset_id": "111",
        "timestamp": "2000",
        "hash": "abc",
        "bids": [{"price": "0.4", "size": "10"}],
        "asks": [],
        "min_order_size": "5",
        "tick_size": "0.01",
        "neg_risk": False,
        "last_trade_price": "0.41",
    }
    snap = RestBook.model_validate(raw).to_event(recv=2050)
    assert (snap.ts, snap.source, snap.bids) == (2000, "rest", ((0.4, 10.0),))


def test_game_message_parses() -> None:
    raw = {
        "gameId": 77,
        "leagueAbbreviation": "NBA",
        "status": "InProgress",
        "live": True,
        "ended": False,
        "score": "51-48",
        "period": 2,
    }
    ev = GameMsg.model_validate(raw).to_event(recv=5)
    assert (ev.game_id, ev.league, ev.period) == ("77", "nba", "2")


def test_subscribe_payload() -> None:
    msg = market_subscribe(["1", "2"])
    assert msg["custom_feature_enabled"] is True
    assert msg["operation"] == "subscribe"
    assert market_subscribe(["1"], unsubscribe=True)["operation"] == "unsubscribe"

from __future__ import annotations

from typing import Any

import pytest

from mm.sim.exchange import Exchange, Fill
from mm.types import BookDelta, BookSide, BookSnapshot, Trade
from tests.conftest import fixture_json

TOKEN = "yes"


def _snap(ts: int, bids: Any, asks: Any) -> BookSnapshot:
    return BookSnapshot(ts, ts, TOKEN, tuple(map(tuple, bids)), tuple(map(tuple, asks)), None, "ws")


def _trade(ts: int, side: BookSide, price: float, size: float) -> Trade:
    return Trade(ts, ts, "c", TOKEN, side, price, size)


def _status(ex: Exchange, order_id: int) -> str:
    return ex.orders[order_id].status


def _run_fixture(ex: Exchange, data: dict[str, Any]) -> list[Fill]:
    book = data["book"]
    ex.on_snapshot(_snap(book["ts"], book["bids"], book["asks"]))
    for o in data["orders"]:
        ex.place("c", TOKEN, o["side"], o["price"], o["size"], o["ts"])
    fills: list[Fill] = []
    for ev in data["events"]:
        if ev["kind"] == "trade":
            fills += ex.on_trade(_trade(ev["ts"], ev["side"], ev["price"], ev["size"]))
        elif ev["kind"] == "cancel":
            ex.cancel(ev["order_id"], ev["ts"])
        else:
            delta = BookDelta(ev["ts"], ev["ts"], TOKEN, ev["side"], ev["price"], ev["size"], None)
            fills += ex.on_delta(delta)
    return fills


def test_ten_order_fixture_matches_hand_worked_answer() -> None:
    data = fixture_json("fill_model_10_orders_synthetic.json")
    ex = Exchange(latency_ms=0, fill_on_book_cross=True)
    fills = _run_fixture(ex, data)
    got = [[f.order_id, f.size, f.trigger, f.ts] for f in fills]
    assert got == data["expected_fills"]
    assert [f.seq for f in fills] == list(range(1, 11))
    status = {str(oid): o.status for oid, o in ex.orders.items()}
    assert status == data["expected_status"]
    assert ex.orders[7].reason == ex.orders[8].reason == "post_only_cross"


def test_without_book_cross_fills_the_crossed_orders_stay_open() -> None:
    data = fixture_json("fill_model_10_orders_synthetic.json")
    ex = Exchange(latency_ms=0, fill_on_book_cross=False)
    fills = _run_fixture(ex, data)
    assert all(f.trigger != "book_cross" for f in fills)
    assert ex.orders[4].status == ex.orders[10].status == "live"


def test_latency_delays_activation_and_cancel() -> None:
    ex = Exchange(latency_ms=100, fill_on_book_cross=False)
    ex.on_snapshot(_snap(0, [[0.40, 10]], [[0.60, 10]]))
    order = ex.place("c", TOKEN, "BUY", 0.45, 5, ts=0)
    assert ex.on_trade(_trade(50, "SELL", 0.45, 5)) == []
    assert _status(ex, order.order_id) == "pending"
    assert [f.size for f in ex.on_trade(_trade(150, "SELL", 0.45, 2))] == [2]
    ex.cancel(order.order_id, ts=200)
    assert [f.size for f in ex.on_trade(_trade(250, "SELL", 0.45, 1))] == [1]
    assert ex.on_trade(_trade(301, "SELL", 0.45, 1)) == []
    assert _status(ex, order.order_id) == "cancelled"
    assert order.remaining == 2


def test_queue_is_not_shortened_by_cancels_ahead() -> None:
    ex = Exchange(latency_ms=0, fill_on_book_cross=True)
    ex.on_snapshot(_snap(0, [[0.45, 50]], [[0.60, 10]]))
    order = ex.place("c", TOKEN, "BUY", 0.45, 5, ts=1)
    ex.on_delta(BookDelta(2, 2, TOKEN, "BUY", 0.45, 1, None))
    assert ex.on_trade(_trade(3, "SELL", 0.45, 40)) == []
    assert order.queue_ahead == pytest.approx(10)


def test_trades_on_the_same_side_do_not_fill() -> None:
    ex = Exchange(latency_ms=0, fill_on_book_cross=True)
    ex.on_snapshot(_snap(0, [[0.40, 1]], [[0.60, 1]]))
    ex.place("c", TOKEN, "BUY", 0.45, 5, ts=0)
    ex.place("c", TOKEN, "SELL", 0.55, 5, ts=0)
    assert ex.on_trade(_trade(1, "BUY", 0.45, 100)) == []
    assert ex.on_trade(_trade(2, "SELL", 0.55, 100)) == []
    assert ex.on_trade(Trade(3, 3, "c", "other", "SELL", 0.30, 100)) == []


def test_invalid_orders_rejected() -> None:
    ex = Exchange(latency_ms=0, fill_on_book_cross=True)
    with pytest.raises(ValueError):
        ex.place("c", TOKEN, "BUY", 1.0, 5, 0)
    with pytest.raises(ValueError):
        ex.place("c", TOKEN, "BUY", 0.5, 0, 0)
    with pytest.raises(ValueError):
        Exchange(latency_ms=-1, fill_on_book_cross=True)

"""Conservative simulated matching for post-only maker orders in YES-token price space.

An order becomes live ``latency_ms`` after it is placed and joins the back of its price level,
behind the size displayed there at that moment. Trades at its price eat that queue before it;
trades through its price fill it up to the trade's size. Cancels ahead of it never move it
forward. With ``fill_on_book_cross`` a book that moves through a live order fills it in full.
A cancel also takes ``latency_ms``; the order can fill until then.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Literal

from mm.recorder.book import LocalBook
from mm.types import BookDelta, BookSide, BookSnapshot, Trade, px

Status = Literal["pending", "live", "cancelled", "filled", "rejected"]
Trigger = Literal["trade", "trade_through", "book_cross"]

_EPS = 1e-9


@dataclass(slots=True)
class SimOrder:
    order_id: int
    condition_id: str
    token_id: str
    side: BookSide
    price: float
    size: float
    placed_ts: int
    active_ts: int
    remaining: float = 0.0
    queue_ahead: float = 0.0
    cancel_ts: int | None = None
    status: Status = "pending"
    reason: str = ""

    @property
    def open(self) -> bool:
        return self.status in ("pending", "live")


@dataclass(frozen=True, slots=True)
class Fill:
    seq: int
    ts: int
    order_id: int
    condition_id: str
    token_id: str
    side: BookSide
    price: float
    size: float
    trigger: Trigger


@dataclass(order=True, slots=True)
class _Action:
    due: int
    seq: int
    kind: Literal["activate", "cancel"] = field(compare=False)
    order_id: int = field(compare=False)


class Exchange:
    def __init__(self, latency_ms: int, fill_on_book_cross: bool) -> None:
        if latency_ms < 0:
            raise ValueError("latency must not be negative")
        self.latency_ms = latency_ms
        self.fill_on_book_cross = fill_on_book_cross
        self.books: dict[str, LocalBook] = {}
        self.orders: dict[int, SimOrder] = {}
        self._actions: list[_Action] = []
        self._ids = itertools.count(1)
        self._action_seq = itertools.count()
        self._fill_seq = itertools.count(1)
        self.events: list[tuple[int, SimOrder, str]] = []

    def book(self, token: str) -> LocalBook:
        b = self.books.get(token)
        if b is None:
            b = self.books[token] = LocalBook(token)
        return b

    def open_orders(self, token: str | None = None) -> list[SimOrder]:
        return [
            o for o in self.orders.values() if o.open and (token is None or o.token_id == token)
        ]

    # order actions --------------------------------------------------------------

    def place(
        self, condition_id: str, token: str, side: BookSide, price: float, size: float, ts: int
    ) -> SimOrder:
        if size <= 0 or not 0 < price < 1:
            raise ValueError(f"invalid order {side} {size}@{price}")
        oid = next(self._ids)
        order = SimOrder(
            oid, condition_id, token, side, px(price), size, ts, ts + self.latency_ms, size
        )
        self.orders[oid] = order
        heapq.heappush(
            self._actions, _Action(order.active_ts, next(self._action_seq), "activate", oid)
        )
        self.events.append((ts, order, "place"))
        return order

    def cancel(self, order_id: int, ts: int, reason: str = "") -> None:
        order = self.orders[order_id]
        if not order.open or order.cancel_ts is not None:
            return
        order.cancel_ts = ts + self.latency_ms
        order.reason = reason
        heapq.heappush(
            self._actions, _Action(order.cancel_ts, next(self._action_seq), "cancel", order_id)
        )

    def advance(self, ts: int) -> None:
        """Apply every activation and cancel due at or before ``ts``."""
        while self._actions and self._actions[0].due <= ts:
            act = heapq.heappop(self._actions)
            order = self.orders[act.order_id]
            if not order.open:
                continue
            if act.kind == "cancel":
                order.status = "cancelled"
                self.events.append((act.due, order, "cancel"))
                continue
            if order.status != "pending":
                continue
            book = self.book(order.token_id)
            if self._crosses(order, book):
                order.status = "rejected"
                order.reason = "post_only_cross"
                self.events.append((act.due, order, "reject"))
                continue
            order.queue_ahead = book.size_at(order.side, order.price)
            order.status = "live"
            self.events.append((act.due, order, "live"))

    @staticmethod
    def _crosses(order: SimOrder, book: LocalBook) -> bool:
        if order.side == "BUY":
            ask = book.best_ask()
            return ask is not None and order.price >= ask - _EPS
        bid = book.best_bid()
        return bid is not None and order.price <= bid + _EPS

    # market data ----------------------------------------------------------------

    def on_snapshot(self, snap: BookSnapshot) -> list[Fill]:
        self.advance(snap.ts)
        self.book(snap.token_id).apply_snapshot(snap)
        return self._book_crosses(snap.token_id, snap.ts)

    def on_delta(self, delta: BookDelta) -> list[Fill]:
        self.advance(delta.ts)
        self.book(delta.token_id).apply_delta(delta)
        return self._book_crosses(delta.token_id, delta.ts)

    def on_trade(self, trade: Trade) -> list[Fill]:
        self.advance(trade.ts)
        hit: BookSide = "BUY" if trade.side == "SELL" else "SELL"
        candidates = [
            o
            for o in self.open_orders(trade.token_id)
            if o.status == "live"
            and o.side == hit
            and (trade.price <= o.price + _EPS if hit == "BUY" else trade.price >= o.price - _EPS)
        ]
        candidates.sort(key=lambda o: (-o.price if hit == "BUY" else o.price, o.order_id))
        left = trade.size
        fills: list[Fill] = []
        for order in candidates:
            if left <= _EPS:
                break
            at_price = abs(trade.price - order.price) <= _EPS
            if at_price:
                eaten = min(order.queue_ahead, left)
                order.queue_ahead -= eaten
                left -= eaten
                if left <= _EPS:
                    break
            qty = min(left, order.remaining)
            left -= qty
            fills.append(self._fill(order, qty, trade.ts, "trade" if at_price else "trade_through"))
        return fills

    def _book_crosses(self, token: str, ts: int) -> list[Fill]:
        if not self.fill_on_book_cross:
            return []
        book = self.book(token)
        return [
            self._fill(o, o.remaining, ts, "book_cross")
            for o in sorted(self.open_orders(token), key=lambda o: o.order_id)
            if o.status == "live" and self._crosses(o, book)
        ]

    def _fill(self, order: SimOrder, qty: float, ts: int, trigger: Trigger) -> Fill:
        order.remaining -= qty
        if order.remaining <= _EPS:
            order.remaining = 0.0
            order.status = "filled"
        return Fill(
            next(self._fill_seq),
            ts,
            order.order_id,
            order.condition_id,
            order.token_id,
            order.side,
            order.price,
            qty,
            trigger,
        )

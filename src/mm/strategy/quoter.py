"""Pre-game two-sided quoting around fair value, in YES-token space.

A bid buys YES at its price; an ask is the mirror order that buys NO at ``1 - price``.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

from mm.config import Quoting
from mm.types import BookSide, px

_EPS = 1e-9


def round_down(price: float, tick: float) -> float:
    return px(math.floor(price / tick + _EPS) * tick)


def round_up(price: float, tick: float) -> float:
    return px(math.ceil(price / tick - _EPS) * tick)


def on_tick(price: float, tick: float) -> bool:
    return abs(price / tick - round(price / tick)) < 1e-6


@dataclass(frozen=True, slots=True)
class Quote:
    side: BookSide
    price: float
    size: float


@dataclass(frozen=True, slots=True)
class Quotes:
    bid: Quote | None
    ask: Quote | None
    fair: float

    def __iter__(self) -> Iterator[Quote]:
        return iter(q for q in (self.bid, self.ask) if q is not None)


class Quoter:
    def __init__(self, cfg: Quoting) -> None:
        self._cfg = cfg

    def reservation(self, fair: float, net_position: float) -> float:
        """Fair value shifted against inventory: long leans the quotes down, short up."""
        lean = max(-1.0, min(1.0, net_position / self._cfg.max_position_shares))
        return fair - self._cfg.skew_strength * lean

    def quotes(
        self,
        *,
        fair: float,
        net_position: float,
        tick: float,
        min_size: float,
        best_bid: float | None = None,
        best_ask: float | None = None,
        size_multiplier: float = 1.0,
        allow_bid: bool = True,
        allow_ask: bool = True,
    ) -> Quotes:
        """Bid and ask to rest for a market, or ``None`` for a side that must stay out.

        Prices are on-tick, inside the configured bounds, never crossed with each other
        and never marketable against the given best bid and ask. Sizes are capped so a
        full fill cannot take the position past ``max_position_shares``.
        """
        cfg = self._cfg
        lo, hi = round_up(cfg.min_price, tick), round_down(cfg.max_price, tick)
        centre = self.reservation(fair, net_position)

        bid: float | None = round_down(centre - cfg.half_spread, tick)
        ask: float | None = round_up(centre + cfg.half_spread, tick)
        assert bid is not None and ask is not None
        if best_ask is not None:
            bid = min(bid, round_down(best_ask - tick, tick))
        if best_bid is not None:
            ask = max(ask, round_up(best_bid + tick, tick))
        bid, ask = min(bid, hi), max(ask, lo)
        if bid >= ask:
            ask = px(bid + tick)
        if bid < lo:
            bid = None
        if ask > hi:
            ask = None

        base = cfg.quote_size * size_multiplier
        bid_size = min(base, cfg.max_position_shares - net_position)
        ask_size = min(base, cfg.max_position_shares + net_position)
        return Quotes(
            bid=Quote("BUY", bid, bid_size)
            if allow_bid and bid is not None and bid_size >= min_size and bid_size > 0
            else None,
            ask=Quote("SELL", ask, ask_size)
            if allow_ask and ask is not None and ask_size >= min_size and ask_size > 0
            else None,
            fair=fair,
        )

    def should_requote(self, fair: float, quoted_fair: float) -> bool:
        return abs(fair - quoted_fair) >= self._cfg.requote_threshold - _EPS

    def should_cancel(self, fair: float, quoted_fair: float) -> bool:
        """True when new odds moved fair value far enough to pull quotes immediately."""
        return abs(fair - quoted_fair) >= self._cfg.cancel_threshold - _EPS

    def stop_time(self, game_start: int) -> int:
        return game_start - int(self._cfg.stop_minutes_before_start * 60_000)

    def quoting_open(self, now: int, game_start: int | None) -> bool:
        return game_start is not None and now < self.stop_time(game_start)

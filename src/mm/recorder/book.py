from __future__ import annotations

from dataclasses import dataclass, field

from mm.types import BookDelta, BookSide, BookSnapshot, Levels, px


@dataclass(slots=True)
class LocalBook:
    """Price-level book for one token, rebuilt from snapshots and deltas.

    A delta older than the current snapshot is stale and ignored; a delta older than the last
    applied delta is applied anyway (levels are absolute sizes) but counted as out of order.
    """

    token_id: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    ts: int = -1
    base_ts: int = -1
    hash: str | None = None
    out_of_order: int = 0
    stale: int = 0

    @property
    def ready(self) -> bool:
        return self.base_ts >= 0

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.ts = self.base_ts = -1
        self.hash = None

    def apply_snapshot(self, snap: BookSnapshot) -> bool:
        if snap.ts < self.ts:
            self.stale += 1
            return False
        self.bids = {px(p): s for p, s in snap.bids if s > 0}
        self.asks = {px(p): s for p, s in snap.asks if s > 0}
        self.ts = self.base_ts = snap.ts
        self.hash = snap.hash
        return True

    def apply_delta(self, delta: BookDelta) -> bool:
        if not self.ready or delta.ts < self.base_ts:
            self.stale += 1
            return False
        if delta.ts < self.ts:
            self.out_of_order += 1
        side = self.bids if delta.side == "BUY" else self.asks
        price = px(delta.price)
        if delta.size > 0:
            side[price] = delta.size
        else:
            side.pop(price, None)
        self.ts = max(self.ts, delta.ts)
        if delta.hash is not None:
            self.hash = delta.hash
        return True

    def levels(self, side: BookSide) -> Levels:
        if side == "BUY":
            return tuple(sorted(self.bids.items(), key=lambda kv: -kv[0]))
        return tuple(sorted(self.asks.items()))

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def mid(self) -> float | None:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return px((bid + ask) / 2)

    def size_at(self, side: BookSide, price: float) -> float:
        return (self.bids if side == "BUY" else self.asks).get(px(price), 0.0)

    def same_levels(self, snap: BookSnapshot) -> bool:
        return self.levels("BUY") == _norm(snap.bids, True) and self.levels("SELL") == _norm(
            snap.asks, False
        )


def _norm(levels: Levels, descending: bool) -> Levels:
    clean = ((px(p), s) for p, s in levels if s > 0)
    return tuple(sorted(clean, key=lambda kv: -kv[0] if descending else kv[0]))

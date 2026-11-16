"""Liquidity-reward estimate from the published scoring formula. Always reported as an estimate."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from mm.config import Rewards
from mm.types import utc_day

Quote = tuple[float, float]


def order_score(v: float, s: float, b: float = 1.0) -> float:
    """S(v, s) = ((v - s) / v)^2 * b for an order ``s`` from the midpoint; zero outside ``v``."""
    if v <= 0 or s < 0 or s > v:
        return 0.0
    return ((v - s) / v) ** 2 * b


def side_score(quotes: Iterable[Quote], mid: float, v: float, min_size: float, b: float) -> float:
    return sum(
        order_score(v, abs(mid - price), b) * size for price, size in quotes if size >= min_size
    )


def combine(q_bid: float, q_ask: float, mid: float, cfg: Rewards) -> float:
    lo, hi = cfg.two_sided_band
    if lo <= mid <= hi:
        c = cfg.single_sided_scale
        return max(min(q_bid, q_ask), max(q_bid / c, q_ask / c))
    return min(q_bid, q_ask)


def market_score(
    bids: Iterable[Quote],
    asks: Iterable[Quote],
    mid: float,
    v: float,
    min_size: float,
    cfg: Rewards,
) -> float:
    b = cfg.multiplier
    return combine(
        side_score(bids, mid, v, min_size, b), side_score(asks, mid, v, min_size, b), mid, cfg
    )


@dataclass
class RewardEstimator:
    """Accumulates our share of each market's score once per sample and pays it out per UTC day.

    The rest of the recorded book is treated as one competing maker.
    """

    cfg: Rewards
    shares: dict[tuple[str, int], float] = field(default_factory=lambda: defaultdict(float))
    rates: dict[str, float] = field(default_factory=dict)

    @property
    def samples_per_day(self) -> float:
        return 86_400 / self.cfg.sample_seconds

    def sample(
        self,
        condition_id: str,
        ts: int,
        ours_bids: Iterable[Quote],
        ours_asks: Iterable[Quote],
        book_bids: Iterable[Quote],
        book_asks: Iterable[Quote],
        mid: float | None,
        v: float,
        min_size: float,
        daily_rate: float,
    ) -> float:
        if mid is None or v <= 0 or daily_rate <= 0:
            return 0.0
        ours = market_score(ours_bids, ours_asks, mid, v, min_size, self.cfg)
        if ours <= 0:
            return 0.0
        others = market_score(book_bids, book_asks, mid, v, min_size, self.cfg)
        share = ours / (ours + others)
        self.shares[(condition_id, utc_day(ts))] += share
        self.rates[condition_id] = daily_rate
        return share

    def payouts(self) -> dict[tuple[str, int], float]:
        out: dict[tuple[str, int], float] = {}
        for (cid, day), total in sorted(self.shares.items()):
            amount = self.rates[cid] * total / self.samples_per_day
            out[(cid, day)] = amount
        return out

    def daily_totals(self) -> dict[int, float]:
        per_day: dict[int, float] = defaultdict(float)
        for (_cid, day), amount in self.payouts().items():
            per_day[day] += amount
        return {
            day: amount
            for day, amount in sorted(per_day.items())
            if amount >= self.cfg.min_daily_payout
        }

    def total(self) -> float:
        return sum(self.daily_totals().values())

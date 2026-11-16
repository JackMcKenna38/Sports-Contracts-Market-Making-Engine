from __future__ import annotations

from dataclasses import dataclass

from mm.config import Config
from mm.fairvalue.devig import devig
from mm.types import OddsQuote


class LookaheadError(RuntimeError):
    """A fair value was requested for a time before odds that have already been observed."""


@dataclass(frozen=True, slots=True)
class Consensus:
    home_prob: float
    bookmakers: int
    anchored: bool
    newest_ts: int


class FairValue:
    """Consensus home-win probability per odds event, fed strictly in time order.

    ``observe`` rejects quotes older than the newest one seen, and ``consensus`` rejects query
    times earlier than the newest quote, so no value can depend on data from its future.
    """

    def __init__(self, cfg: Config) -> None:
        self._fv = cfg.fairvalue
        self._max_age = int(cfg.fairvalue.max_odds_age_minutes * 60_000)
        self._latest: dict[str, dict[str, OddsQuote]] = {}
        self.clock = -1

    def observe(self, quote: OddsQuote) -> None:
        if quote.ts < self.clock:
            raise LookaheadError(f"odds at {quote.ts} arrived after odds at {self.clock}")
        self.clock = quote.ts
        self._latest.setdefault(quote.event_id, {})[quote.bookmaker] = quote

    def consensus(self, event_id: str, t: int) -> Consensus | None:
        if t < self.clock:
            raise LookaheadError(f"fair value asked at {t} after observing odds at {self.clock}")
        books = self._latest.get(event_id, {})
        fresh = [q for q in books.values() if t - q.ts <= self._max_age]
        if len(fresh) < self._fv.min_bookmakers:
            return None
        num = den = 0.0
        for q in fresh:
            w = self._fv.bookmaker_weights.get(q.bookmaker, self._fv.default_weight)
            if w <= 0:
                continue
            home, _away = devig((q.home_price, q.away_price), self._fv.devig_method)
            num += w * home
            den += w
        if den == 0:
            return None
        return Consensus(
            home_prob=num / den,
            bookmakers=len(fresh),
            anchored=any(q.bookmaker == self._fv.anchor_bookmaker for q in fresh),
            newest_ts=max(q.ts for q in fresh),
        )

    def yes_prob(self, event_id: str, yes_is_home: bool, t: int) -> float | None:
        c = self.consensus(event_id, t)
        if c is None:
            return None
        return c.home_prob if yes_is_home else 1.0 - c.home_prob

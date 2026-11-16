"""Risk rules: daily loss cap, per-event exposure, loss throttle and stale-data kill switch.

Each rule logs one `RiskEvent` when it starts to bind and stays silent until it has
cleared and binds again, so the risk log records triggers, not every tick they stay active.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from mm.config import Risk
from mm.types import utc_day

PORTFOLIO = "*"

DAILY_LOSS = "daily_loss"
EVENT_EXPOSURE = "event_exposure"
LOSS_THROTTLE = "loss_throttle"
STALE_BOOK = "stale_book"
STALE_ODDS = "stale_odds"
STALE_GAME_FEED = "stale_game_feed"


@dataclass(frozen=True, slots=True)
class RiskEvent:
    ts: int
    condition_id: str
    rule: str
    value: float
    limit: float
    action: str


@dataclass(frozen=True, slots=True)
class Decision:
    """What a market may quote right now."""

    allow_bid: bool
    allow_ask: bool
    size_multiplier: float
    reasons: tuple[str, ...]

    @property
    def quote(self) -> bool:
        return self.allow_bid or self.allow_ask


@dataclass(slots=True)
class _Market:
    losses: int = 0
    multiplier: float = 1.0
    stopped: bool = False
    trip_start: float | None = None
    active: set[str] = field(default_factory=set)


class RiskEngine:
    def __init__(self, cfg: Risk, max_odds_age_ms: int, sink: Callable[[RiskEvent], None]) -> None:
        self._cfg = cfg
        self._max_odds_age_ms = max_odds_age_ms
        self._sink = sink
        self._markets: dict[str, _Market] = {}
        self._day: int | None = None
        self._day_start_equity = 0.0
        self._halted_day: int | None = None
        self._feed_active = False
        self._last_flat_cash: dict[str, float] = {}

    def _market(self, condition_id: str) -> _Market:
        return self._markets.setdefault(condition_id, _Market())

    def _log(
        self, ts: int, condition_id: str, rule: str, value: float, limit: float, action: str
    ) -> None:
        self._sink(RiskEvent(ts, condition_id, rule, value, limit, action))

    def _edge(
        self,
        active: set[str],
        breached: bool,
        ts: int,
        condition_id: str,
        rule: str,
        value: float,
        limit: float,
        action: str,
    ) -> bool:
        if breached and rule not in active:
            active.add(rule)
            self._log(ts, condition_id, rule, value, limit, action)
        elif not breached:
            active.discard(rule)
        return breached

    # daily loss cap ------------------------------------------------------------

    def update_equity(self, ts: int, equity: float) -> bool:
        """Feed realised-plus-marked equity. Returns True while quoting is halted for the day."""
        day = utc_day(ts)
        if day != self._day:
            self._day = day
            self._day_start_equity = equity
        loss = self._day_start_equity - equity
        if self._halted_day != day and loss >= self._cfg.daily_loss_limit:
            self._halted_day = day
            self._log(ts, PORTFOLIO, DAILY_LOSS, -loss, -self._cfg.daily_loss_limit, "halt_day")
        return self._halted_day == day

    def halted(self, ts: int) -> bool:
        return self._halted_day == utc_day(ts)

    # loss throttle ---------------------------------------------------------------

    def observe_position(self, ts: int, condition_id: str, flat: bool, cash_flow: float) -> None:
        """Track inventory round trips; a flat-to-flat trip that lost cash is one loss."""
        market = self._market(condition_id)
        if not flat:
            if market.trip_start is None:
                market.trip_start = self._last_flat_cash.get(condition_id, 0.0)
            return
        if market.trip_start is not None:
            result = cash_flow - market.trip_start
            market.trip_start = None
            if result < 0:
                self._record_loss(ts, condition_id, market)
        self._last_flat_cash[condition_id] = cash_flow

    def _record_loss(self, ts: int, condition_id: str, market: _Market) -> None:
        market.losses += 1
        limit = float(self._cfg.max_losses_per_event)
        if market.losses >= self._cfg.max_losses_per_event:
            market.stopped = True
            market.multiplier = 0.0
            self._log(ts, condition_id, LOSS_THROTTLE, market.losses, limit, "stop_event")
        else:
            market.multiplier *= 1.0 - self._cfg.loss_size_cut_fraction
            self._log(ts, condition_id, LOSS_THROTTLE, market.losses, limit, "cut_size")

    def size_multiplier(self, condition_id: str) -> float:
        return self._market(condition_id).multiplier

    def losses(self, condition_id: str) -> int:
        return self._market(condition_id).losses

    # per-market decision -----------------------------------------------------------

    def check(
        self,
        ts: int,
        condition_id: str,
        *,
        net_position: float,
        exposure: float,
        book_ts: int | None,
        odds_ts: int | None,
        feed_down_since: int | None,
    ) -> Decision:
        """Evaluate every rule for one market.

        ``exposure`` is the marked value of the net inventory; ``book_ts`` and ``odds_ts``
        are when the book and the odds behind fair value last updated (``None`` = never);
        ``feed_down_since`` is when the game feed was lost (``None`` = connected).
        """
        cfg = self._cfg
        market = self._market(condition_id)
        reasons: list[str] = []

        if self.halted(ts):
            reasons.append(DAILY_LOSS)
        if market.stopped:
            reasons.append(LOSS_THROTTLE)

        book_age = None if book_ts is None else (ts - book_ts) / 1000
        if self._edge(
            market.active,
            book_age is None or book_age > cfg.stale_book_seconds,
            ts,
            condition_id,
            STALE_BOOK,
            float("inf") if book_age is None else book_age,
            cfg.stale_book_seconds,
            "pull_quotes",
        ):
            reasons.append(STALE_BOOK)

        odds_age_ms = None if odds_ts is None else ts - odds_ts
        if self._edge(
            market.active,
            odds_age_ms is None or odds_age_ms > self._max_odds_age_ms,
            ts,
            condition_id,
            STALE_ODDS,
            float("inf") if odds_age_ms is None else odds_age_ms / 1000,
            self._max_odds_age_ms / 1000,
            "pull_quotes",
        ):
            reasons.append(STALE_ODDS)

        if self.feed_stale(ts, feed_down_since):
            reasons.append(STALE_GAME_FEED)

        allow_bid = allow_ask = not reasons
        if self._edge(
            market.active,
            exposure >= cfg.max_event_exposure,
            ts,
            condition_id,
            EVENT_EXPOSURE,
            exposure,
            cfg.max_event_exposure,
            "block_increase",
        ):
            reasons.append(EVENT_EXPOSURE)
            if net_position > 0:
                allow_bid = False
            elif net_position < 0:
                allow_ask = False

        return Decision(allow_bid, allow_ask, market.multiplier, tuple(reasons))

    def feed_stale(self, ts: int, feed_down_since: int | None) -> bool:
        """Portfolio-wide: the game feed has been down longer than the configured limit."""
        down = None if feed_down_since is None else (ts - feed_down_since) / 1000
        breached = down is not None and down > self._cfg.stale_game_feed_seconds
        if breached and not self._feed_active:
            assert down is not None
            self._log(
                ts, PORTFOLIO, STALE_GAME_FEED, down, self._cfg.stale_game_feed_seconds, "pull_all"
            )
        self._feed_active = breached
        return breached

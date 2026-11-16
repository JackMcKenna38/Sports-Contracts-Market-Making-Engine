"""The trading loop shared by backtest and paper mode.

It consumes normalised events in time order and owns fair value, quoting, risk, the simulated
exchange and the portfolio. It never talks to the outside world: rows for the run tables are
buffered and handed out through ``drain``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import Table

from mm.config import Config
from mm.fairvalue.consensus import FairValue
from mm.inventory.portfolio import Portfolio
from mm.risk.engine import PORTFOLIO, Decision, RiskEngine, RiskEvent
from mm.sim.exchange import Exchange, Fill, SimOrder
from mm.sim.fees import maker_rebate
from mm.sim.rewards import RewardEstimator
from mm.storage import schema as s
from mm.storage.store import Row
from mm.strategy.quoter import Quote, Quoter
from mm.types import (
    BookDelta,
    BookSide,
    BookSnapshot,
    Event,
    GameStatus,
    Gap,
    MappingChange,
    MarketMeta,
    MarketResolved,
    MetaChange,
    OddsQuote,
    Tick,
    TickSizeChange,
    Trade,
    px,
)

_EPS = 1e-9


def event_time(e: Event) -> int:
    if isinstance(e, BookSnapshot) and e.source == "rest":
        return e.recv
    return e.ts


@dataclass(slots=True)
class MarketState:
    meta: MarketMeta
    event_id: str | None = None
    yes_is_home: bool | None = None
    fair: float | None = None
    fair_ts: int | None = None
    anchor: float | None = None
    bid: int | None = None
    ask: int | None = None
    gaps: int = 0
    gap_since: int | None = None
    excluded_ms: int = 0
    book_ts: int | None = None
    game_live: bool = False
    resolved: bool = False
    stop_reason: str = ""
    quoted: bool = False

    @property
    def token_yes(self) -> str:
        return self.meta.token_yes


@dataclass(slots=True)
class _Markout:
    seq: int
    condition_id: str
    due: int
    horizon_s: int
    fair: float | None
    mid: float | None


@dataclass
class Engine:
    cfg: Config
    run_id: str
    latency_ms: int
    markets: dict[str, MarketState] = field(default_factory=dict)
    now: int = 0

    def __post_init__(self) -> None:
        cfg = self.cfg
        self.fv = FairValue(cfg)
        self.quoter = Quoter(cfg.quoting)
        self.exchange = Exchange(self.latency_ms, cfg.sim.fill_on_book_cross)
        self.portfolio = Portfolio(cfg.risk.starting_capital)
        self.risk = RiskEngine(
            cfg.risk, int(cfg.fairvalue.max_odds_age_minutes * 60_000), self._risk_event
        )
        self.rewards = RewardEstimator(cfg.rewards)
        self._by_token: dict[str, str] = {}
        self._by_event: dict[str, set[str]] = defaultdict(set)
        self._by_game: dict[str, set[str]] = defaultdict(set)
        self._rows: dict[Table, list[Row]] = defaultdict(list)
        self._markouts: list[_Markout] = []
        self._next_markout: int | None = None
        self._ws_gaps = 0
        self._feed_down_since: int | None = None
        self._next_ledger: int | None = None
        self._next_reward: int | None = None
        self._ledger_ms = int(cfg.sim.ledger_interval_seconds * 1000)
        self._reward_ms = int(cfg.rewards.sample_seconds * 1000)
        self.fills = 0
        self.settled: set[str] = set()

    # outputs ----------------------------------------------------------------------

    def drain(self) -> dict[Table, list[Row]]:
        out, self._rows = dict(self._rows), defaultdict(list)
        return out

    def _risk_event(self, ev: RiskEvent) -> None:
        self._rows[s.risk_events].append(
            {
                "run_id": self.run_id,
                "ts": ev.ts,
                "condition_id": ev.condition_id,
                "rule": ev.rule,
                "value": ev.value,
                "limit": ev.limit,
                "action": ev.action,
            }
        )

    def _order_rows(self) -> None:
        for ts, order, kind in self.exchange.events:
            if kind == "live":
                continue
            m = self.markets.get(order.condition_id)
            self._rows[s.sim_orders].append(
                {
                    "run_id": self.run_id,
                    "ts": ts,
                    "order_id": order.order_id,
                    "condition_id": order.condition_id,
                    "token_id": order.token_id,
                    "side": order.side,
                    "price": order.price,
                    "size": order.size,
                    "action": kind,
                    "reason": order.reason or None,
                    "fair": m.fair if m else None,
                    "queue_ahead": order.queue_ahead if kind != "place" else None,
                }
            )
        self.exchange.events.clear()

    # helpers ------------------------------------------------------------------------

    def _mid(self, m: MarketState) -> float | None:
        book = self.exchange.books.get(m.token_yes)
        return book.mid() if book else None

    def _register(self, meta: MarketMeta) -> MarketState:
        m = self.markets.get(meta.condition_id)
        if m is None:
            m = self.markets[meta.condition_id] = MarketState(meta)
            self._by_token[meta.token_yes] = meta.condition_id
            self._by_token[meta.token_no] = meta.condition_id
        else:
            m.meta = meta
        if meta.game_id:
            self._by_game[meta.game_id].add(meta.condition_id)
        return m

    def _orders(self, m: MarketState) -> list[SimOrder]:
        return [
            self.exchange.orders[oid]
            for oid in (m.bid, m.ask)
            if oid is not None and self.exchange.orders[oid].open
        ]

    def _cancel_all(self, m: MarketState, reason: str) -> None:
        for order in self._orders(m):
            self.exchange.cancel(order.order_id, self.now, reason)
        m.bid = m.ask = None

    def _blocked(self, m: MarketState) -> str:
        meta = m.meta
        if m.resolved or meta.closed:
            return "closed"
        if not meta.accepting_orders:
            return "not_accepting"
        if m.event_id is None or m.yes_is_home is None:
            return "no_odds_match"
        if m.fair is None:
            return "no_fair_value"
        if not self.quoter.quoting_open(self.now, meta.game_start):
            return "stop_time"
        if m.game_live:
            return "game_live"
        if m.gaps > 0 or self._ws_gaps > 0:
            return "data_gap"
        return ""

    def _exposure(self, m: MarketState) -> tuple[float, float]:
        pos = self.portfolio.position(m.meta.condition_id)
        net = pos.net
        mark = pos.mark if pos.mark is not None else (m.fair if m.fair is not None else 0.5)
        value = net * mark if net > 0 else -net * (1.0 - mark)
        return net, value

    def _decision(self, m: MarketState) -> Decision:
        net, exposure = self._exposure(m)
        return self.risk.check(
            self.now,
            m.meta.condition_id,
            net_position=net,
            exposure=exposure,
            book_ts=m.book_ts,
            odds_ts=m.fair_ts,
            feed_down_since=self._feed_down_since,
        )

    def _reserved_cash(self) -> float:
        total = 0.0
        for o in self.exchange.open_orders():
            total += o.remaining * (o.price if o.side == "BUY" else 1.0 - o.price)
        return total

    def _place(self, m: MarketState, q: Quote) -> int | None:
        cost = q.size * (q.price if q.side == "BUY" else 1.0 - q.price)
        if cost > self.portfolio.cash - self._reserved_cash() + _EPS:
            return None
        order = self.exchange.place(
            m.meta.condition_id, m.token_yes, q.side, q.price, q.size, self.now
        )
        m.quoted = True
        return order.order_id

    def _quote(self, m: MarketState) -> None:
        blocked = self._blocked(m)
        if blocked:
            if self._orders(m):
                self._cancel_all(m, blocked)
            m.stop_reason = blocked
            return
        decision = self._decision(m)
        if not decision.quote:
            if self._orders(m):
                self._cancel_all(m, decision.reasons[0] if decision.reasons else "risk")
            m.stop_reason = ",".join(decision.reasons) or "risk"
            return
        assert m.fair is not None
        if m.anchor is None or self.quoter.should_requote(m.fair, m.anchor):
            m.anchor = m.fair
        book = self.exchange.books.get(m.token_yes)
        pos = self.portfolio.position(m.meta.condition_id)
        quotes = self.quoter.quotes(
            fair=m.anchor,
            net_position=pos.net,
            tick=m.meta.tick_size,
            min_size=m.meta.min_size,
            best_bid=book.best_bid() if book else None,
            best_ask=book.best_ask() if book else None,
            size_multiplier=decision.size_multiplier,
            allow_bid=decision.allow_bid,
            allow_ask=decision.allow_ask,
        )
        m.stop_reason = ""
        m.bid = self._replace(m, m.bid, quotes.bid)
        m.ask = self._replace(m, m.ask, quotes.ask)

    def _replace(self, m: MarketState, current: int | None, want: Quote | None) -> int | None:
        order = self.exchange.orders.get(current) if current is not None else None
        live = order is not None and order.open and order.cancel_ts is None
        if want is None:
            if live and order is not None:
                self.exchange.cancel(order.order_id, self.now, "requote")
            return None
        if live and order is not None and abs(order.price - want.price) < _EPS:
            return order.order_id
        if live and order is not None:
            self.exchange.cancel(order.order_id, self.now, "requote")
        return self._place(m, want)

    # fills --------------------------------------------------------------------------

    def _apply_fills(self, fills: Iterable[Fill]) -> None:
        touched: list[MarketState] = []
        for f in fills:
            m = self.markets[f.condition_id]
            meta = m.meta
            rebate = maker_rebate(
                f.size, f.price, meta.fee_rate, meta.fee_exponent, meta.rebate_rate
            )
            self.portfolio.fill(f.condition_id, f.side, f.price, f.size, fair=m.fair, rebate=rebate)
            pos = self.portfolio.position(f.condition_id)
            self.risk.observe_position(self.now, f.condition_id, pos.flat, pos.cash_flow)
            yes_buy = f.side == "BUY"
            mid = self._mid(m)
            self.fills += 1
            self._rows[s.sim_fills].append(
                {
                    "run_id": self.run_id,
                    "seq": f.seq,
                    "ts": f.ts,
                    "order_id": f.order_id,
                    "condition_id": f.condition_id,
                    "token_id": meta.token_yes if yes_buy else meta.token_no,
                    "side": "BUY",
                    "price": f.price if yes_buy else px(1.0 - f.price),
                    "size": f.size,
                    "liquidity": "maker",
                    "fee": 0.0,
                    "rebate": rebate,
                    "yes_price": f.price,
                    "yes_side": f.side,
                    "fair": m.fair,
                    "mid": mid,
                    "trigger": f.trigger,
                }
            )
            for h in self.cfg.sim.markout_horizons_seconds:
                due = f.ts + h * 1000
                self._markouts.append(_Markout(f.seq, f.condition_id, due, h, m.fair, mid))
                if self._next_markout is None or due < self._next_markout:
                    self._next_markout = due
            if m not in touched:
                touched.append(m)
        for m in touched:
            self._quote(m)

    def _resolve_markouts(self, before: int, final: bool = False) -> None:
        """Resolve markouts due strictly before ``before`` from the state as it stands now."""
        keep: list[_Markout] = []
        for mk in self._markouts:
            if mk.due >= before and not final:
                keep.append(mk)
                continue
            fair_move = mid_move = None
            if mk.due <= self.now:
                m = self.markets[mk.condition_id]
                mid = self._mid(m)
                if mk.fair is not None and m.fair is not None:
                    fair_move = m.fair - mk.fair
                if mk.mid is not None and mid is not None:
                    mid_move = mid - mk.mid
            self._rows[s.sim_markouts].append(
                {
                    "run_id": self.run_id,
                    "fill_seq": mk.seq,
                    "horizon_s": mk.horizon_s,
                    "fair_move": fair_move,
                    "mid_move": mid_move,
                }
            )
        self._markouts = keep
        self._next_markout = min((mk.due for mk in keep), default=None)

    # event handling -----------------------------------------------------------------

    def on_event(self, e: Event) -> None:
        ts = event_time(e)
        if self._next_markout is not None and self._next_markout < ts:
            self._resolve_markouts(ts)
        self.now = max(self.now, ts)
        self.exchange.advance(self.now)
        if self._next_ledger is None:
            self._next_ledger = self.now
            self._next_reward = self.now
        if isinstance(e, BookSnapshot):
            self._on_book(e.token_id, self.exchange.on_snapshot(e))
        elif isinstance(e, BookDelta):
            self._on_book(e.token_id, self.exchange.on_delta(e))
        elif isinstance(e, Trade):
            self._on_trade(e)
        elif isinstance(e, OddsQuote):
            self._on_odds(e)
        elif isinstance(e, MetaChange):
            self._quote(self._register(e.meta))
        elif isinstance(e, MappingChange):
            self._on_mapping(e)
        elif isinstance(e, TickSizeChange):
            self._on_tick_size(e)
        elif isinstance(e, MarketResolved):
            self._on_resolved(e.condition_id, e.winning_token_id)
        elif isinstance(e, GameStatus):
            self._on_game(e)
        elif isinstance(e, Gap):
            self._on_gap(e)
        elif isinstance(e, Tick):
            self._on_tick()
        self._order_rows()

    def _on_book(self, token: str, fills: list[Fill]) -> None:
        cid = self._by_token.get(token)
        if cid is None:
            return
        m = self.markets[cid]
        m.book_ts = self.now
        self._apply_fills(fills)

    def _on_trade(self, t: Trade) -> None:
        cid = self._by_token.get(t.token_id)
        if cid is None:
            return
        m = self.markets[cid]
        if t.token_id == m.meta.token_yes:
            trade = t
        elif self.cfg.sim.trade_assets == "both":
            side: BookSide = "SELL" if t.side == "BUY" else "BUY"
            trade = replace(t, token_id=m.token_yes, side=side, price=px(1.0 - t.price))
        else:
            return
        self._apply_fills(self.exchange.on_trade(trade))

    def _set_fair(self, m: MarketState) -> None:
        if m.event_id is None or m.yes_is_home is None:
            return
        c = self.fv.consensus(m.event_id, self.now)
        if c is None:
            m.fair = None
            return
        fair = c.home_prob if m.yes_is_home else 1.0 - c.home_prob
        if m.anchor is not None and self.quoter.should_cancel(fair, m.anchor):
            self._cancel_all(m, "odds_move")
            m.anchor = None
        m.fair, m.fair_ts = fair, c.newest_ts
        self.portfolio.mark(m.meta.condition_id, fair)

    def _on_odds(self, q: OddsQuote) -> None:
        self.fv.observe(q)
        for cid in sorted(self._by_event.get(q.event_id, ())):
            m = self.markets[cid]
            self._set_fair(m)
            self._quote(m)

    def _on_mapping(self, e: MappingChange) -> None:
        m = self.markets.get(e.condition_id)
        if m is None:
            return
        if m.event_id is not None:
            self._by_event[m.event_id].discard(e.condition_id)
        m.event_id, m.yes_is_home = e.odds_event_id, e.yes_is_home
        self._by_event[e.odds_event_id].add(e.condition_id)
        self._set_fair(m)
        self._quote(m)

    def _on_tick_size(self, e: TickSizeChange) -> None:
        m = self.markets.get(e.condition_id)
        if m is None:
            return
        m.meta = replace(m.meta, tick_size=e.new_tick_size)
        self._cancel_all(m, "tick_size_change")
        self._quote(m)

    def _on_resolved(self, condition_id: str, winner: str) -> None:
        m = self.markets.get(condition_id)
        if m is None or m.resolved:
            return
        self._cancel_all(m, "resolved")
        m.resolved = True
        if winner not in (m.meta.token_yes, m.meta.token_no):
            raise ValueError(f"{condition_id}: winning token {winner} is not one of its tokens")
        self.portfolio.settle(condition_id, winner == m.meta.token_yes)
        self.settled.add(condition_id)

    def settle_from_final_state(self, winners: dict[str, str]) -> None:
        for cid in sorted(winners):
            self._on_resolved(cid, winners[cid])

    def _on_game(self, g: GameStatus) -> None:
        for cid in sorted(self._by_game.get(g.game_id, ())):
            m = self.markets[cid]
            live = g.live or g.ended
            if live != m.game_live:
                m.game_live = live
                self._quote(m)

    def _gap_markets(self, g: Gap) -> list[MarketState]:
        if g.token_id is not None:
            cid = self._by_token.get(g.token_id)
            return [self.markets[cid]] if cid else []
        return [self.markets[c] for c in sorted(self.markets)]

    def _on_gap(self, g: Gap) -> None:
        if g.stream == "sports_ws":
            self._feed_down_since = self.now if g.open else None
            return
        if g.stream != "market_ws":
            return
        targets = self._gap_markets(g)
        if g.token_id is None:
            self._ws_gaps += 1 if g.open else -1
            self._ws_gaps = max(self._ws_gaps, 0)
        for m in targets:
            if g.token_id is not None:
                m.gaps = max(0, m.gaps + (1 if g.open else -1))
            in_gap = m.gaps > 0 or self._ws_gaps > 0
            if in_gap and m.gap_since is None:
                m.gap_since = self.now
            elif not in_gap and m.gap_since is not None:
                m.excluded_ms += self.now - m.gap_since
                m.gap_since = None
            self._quote(m)

    def _on_tick(self) -> None:
        for cid in sorted(self.markets):
            self._quote(self.markets[cid])
        self.risk.update_equity(self.now, self.portfolio.equity())
        if self._next_reward is not None and self.now >= self._next_reward:
            self._sample_rewards()
            self._next_reward = self.now + self._reward_ms
        if self._next_ledger is not None and self.now >= self._next_ledger:
            self.write_ledger()
            self._next_ledger = self.now + self._ledger_ms

    def _sample_rewards(self) -> None:
        for cid in sorted(self.markets):
            m = self.markets[cid]
            book = self.exchange.books.get(m.token_yes)
            if book is None:
                continue
            ours = [o for o in self._orders(m) if o.status == "live"]
            self.rewards.sample(
                cid,
                self.now,
                [(o.price, o.remaining) for o in ours if o.side == "BUY"],
                [(o.price, o.remaining) for o in ours if o.side == "SELL"],
                book.levels("BUY"),
                book.levels("SELL"),
                book.mid(),
                m.meta.rewards_max_spread,
                m.meta.rewards_min_size,
                m.meta.rewards_daily_rate,
            )

    def write_ledger(self) -> None:
        total_yes = total_no = 0.0
        for cid in sorted(self.portfolio.positions):
            pos = self.portfolio.positions[cid]
            total_yes += pos.yes
            total_no += pos.no
            self._rows[s.pnl_ledger].append(
                {
                    "run_id": self.run_id,
                    "ts": self.now,
                    "condition_id": cid,
                    "cash": pos.cash_flow,
                    "inventory_yes": pos.yes,
                    "inventory_no": pos.no,
                    "mark": pos.mark,
                    "components": json.dumps(pos.components(), sort_keys=True),
                }
            )
        comps: dict[str, Any] = dict(self.portfolio.components())
        comps["equity"] = self.portfolio.equity()
        comps["rewards_estimate"] = self.rewards.total()
        self._rows[s.pnl_ledger].append(
            {
                "run_id": self.run_id,
                "ts": self.now,
                "condition_id": PORTFOLIO,
                "cash": self.portfolio.cash,
                "inventory_yes": total_yes,
                "inventory_no": total_no,
                "mark": None,
                "components": json.dumps(comps, sort_keys=True),
            }
        )

    def finish(self) -> dict[str, Any]:
        for cid in sorted(self.markets):
            m = self.markets[cid]
            self._cancel_all(m, "end_of_run")
            if m.gap_since is not None:
                m.excluded_ms += self.now - m.gap_since
                m.gap_since = None
        self.exchange.advance(self.now + self.latency_ms)
        self._order_rows()
        self._resolve_markouts(self.now + 1, final=True)
        self.write_ledger()
        never_quoted: dict[str, int] = defaultdict(int)
        for m in self.markets.values():
            if m.quoted:
                continue
            if m.event_id is None:
                never_quoted["no_odds_match"] += 1
            elif m.fair_ts is None:
                never_quoted["no_fair_value"] += 1
            else:
                never_quoted[m.stop_reason or "no_order_placed"] += 1
        unsettled = sorted(
            cid
            for cid, pos in self.portfolio.positions.items()
            if not pos.flat and cid not in self.settled
        )
        return {
            "equity": self.portfolio.equity(),
            "pnl": self.portfolio.pnl(),
            "components": self.portfolio.components(),
            "rewards_estimate": self.rewards.total(),
            "latency_ms": self.latency_ms,
            "markets": len(self.markets),
            "fills": self.fills,
            "settled": len(self.settled),
            "unsettled": len(unsettled),
            "excluded_ms": sum(m.excluded_ms for m in self.markets.values()),
            "never_quoted": dict(sorted(never_quoted.items())),
        }

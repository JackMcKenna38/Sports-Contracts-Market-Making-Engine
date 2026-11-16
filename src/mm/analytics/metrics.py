"""Aggregations over one run's rows. Every number is derived from the run tables alone."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import RowMapping

from mm.storage import schema as s
from mm.storage.store import Store

PORTFOLIO = "*"
COMPONENTS = ("spread", "inventory", "rebates", "fees")

Row = RowMapping


class UnknownRun(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class RunData:
    run: Row
    summary: Mapping[str, Any]
    markets: Mapping[str, Row]
    winners: Mapping[str, str]
    orders: Sequence[Row]
    fills: Sequence[Row]
    markouts: Sequence[Row]
    ledger: Sequence[Row]
    risk_events: Sequence[Row]
    gaps: Sequence[Row]

    @property
    def run_id(self) -> str:
        return str(self.run["run_id"])


def load_run(store: Store, run_id: str) -> RunData:
    run = store.run(run_id)
    if run is None:
        raise UnknownRun(f"run {run_id!r} is not in {store.url}")
    markets = {m["condition_id"]: m for m in store.markets()}
    return RunData(
        run=run,
        summary=json.loads(run["summary_json"]) if run["summary_json"] else {},
        markets=markets,
        winners=_winners(store, markets, run["data_end"]),
        orders=store.run_rows(s.sim_orders, run_id),
        fills=store.run_rows(s.sim_fills, run_id),
        markouts=store.run_rows(s.sim_markouts, run_id),
        ledger=store.run_rows(s.pnl_ledger, run_id),
        risk_events=store.run_rows(s.risk_events, run_id),
        gaps=store.gaps(run["data_start"], run["data_end"]),
    )


def _winners(store: Store, markets: Mapping[str, Row], end: int) -> dict[str, str]:
    """Winning token per market: the stored final state, else a recorded resolution event."""
    winners = {
        cid: str(m["winning_token_id"])
        for cid, m in markets.items()
        if m["winning_token_id"] is not None
    }
    for row in store.iter_market_events(0, end):
        if row["event_type"] == "market_resolved":
            winners.setdefault(
                row["condition_id"], str(json.loads(row["payload_json"])["winning_asset_id"])
            )
    return winners


def pnl_of(components: Mapping[str, float]) -> float:
    return (
        components.get("spread", 0.0)
        + components.get("inventory", 0.0)
        + components.get("rebates", 0.0)
        - components.get("fees", 0.0)
    )


@dataclass(frozen=True, slots=True)
class Attribution:
    """PnL split by source. `rewards_estimate` is reported beside PnL, never inside it."""

    spread: float = 0.0
    inventory: float = 0.0
    rebates: float = 0.0
    fees: float = 0.0
    rewards_estimate: float | None = None
    fills: int = 0
    volume: float = 0.0

    @property
    def pnl(self) -> float:
        return self.spread + self.inventory + self.rebates - self.fees


def final_components(data: RunData) -> dict[str, dict[str, float]]:
    """Last ledger row's components per market, portfolio rows excluded."""
    last: dict[str, dict[str, float]] = {}
    for row in data.ledger:
        if row["condition_id"] != PORTFOLIO:
            last[row["condition_id"]] = json.loads(row["components"])
    return last


def attribution(data: RunData) -> Attribution:
    per_market = final_components(data)
    total = {k: sum(c.get(k, 0.0) for c in per_market.values()) for k in COMPONENTS}
    rewards = data.summary.get("rewards_estimate")
    return Attribution(
        **total,
        rewards_estimate=None if rewards is None else float(rewards),
        fills=len(data.fills),
        volume=sum(f["size"] for f in data.fills),
    )


def pnl_by_league(data: RunData) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for cid, components in final_components(data).items():
        market = data.markets.get(cid)
        out[market["league"] if market else "unknown"] += pnl_of(components)
    return dict(sorted(out.items()))


def cumulative_pnl(data: RunData) -> list[tuple[int, float]]:
    return [
        (row["ts"], pnl_of(json.loads(row["components"])))
        for row in data.ledger
        if row["condition_id"] == PORTFOLIO
    ]


def inventory_series(data: RunData) -> dict[str, list[tuple[int, float]]]:
    """Net YES-space position (YES minus NO shares) over time, per market that ever held any."""
    series: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for row in data.ledger:
        if row["condition_id"] != PORTFOLIO:
            series[row["condition_id"]].append(
                (row["ts"], row["inventory_yes"] - row["inventory_no"])
            )
    return {cid: pts for cid, pts in series.items() if any(net for _, net in pts)}


@dataclass(frozen=True, slots=True)
class Markout:
    horizon_s: int
    fair_mean: float | None
    fair_n: int
    mid_mean: float | None
    mid_n: int


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def markouts(data: RunData) -> list[Markout]:
    """Mean move per horizon over fills, signed so that negative is adverse selection."""
    sign = {f["seq"]: 1.0 if f["yes_side"] == "BUY" else -1.0 for f in data.fills}
    fair: dict[int, list[float]] = defaultdict(list)
    mid: dict[int, list[float]] = defaultdict(list)
    for row in data.markouts:
        direction = sign[row["fill_seq"]]
        horizon = row["horizon_s"]
        fair.setdefault(horizon, [])
        mid.setdefault(horizon, [])
        if row["fair_move"] is not None:
            fair[horizon].append(direction * row["fair_move"])
        if row["mid_move"] is not None:
            mid[horizon].append(direction * row["mid_move"])
    return [
        Markout(h, _mean(fair[h]), len(fair[h]), _mean(mid[h]), len(mid[h])) for h in sorted(fair)
    ]


@dataclass(frozen=True, slots=True)
class FillBucket:
    lower: float
    upper: float
    placed: int
    filled: int

    @property
    def rate(self) -> float:
        return self.filled / self.placed


def _yes_price(data: RunData, row: Row) -> float | None:
    market = data.markets.get(row["condition_id"])
    if market is None:
        return None
    price = float(row["price"])
    return price if row["token_id"] == market["token_id_yes"] else 1.0 - price


def fill_rate_by_distance(data: RunData, bucket: float) -> list[FillBucket]:
    """Share of placed orders that received any fill, by |quote price - fair value| at placement."""
    filled = {f["order_id"] for f in data.fills}
    placed: dict[int, int] = defaultdict(int)
    hit: dict[int, int] = defaultdict(int)
    for order in data.orders:
        if order["action"] != "place" or order["fair"] is None:
            continue
        price = _yes_price(data, order)
        if price is None:
            continue
        index = math.floor(abs(price - order["fair"]) / bucket + 1e-9)
        placed[index] += 1
        hit[index] += order["order_id"] in filled
    return [FillBucket(i * bucket, (i + 1) * bucket, placed[i], hit[i]) for i in sorted(placed)]


def risk_summary(data: RunData) -> list[tuple[str, str, int]]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for event in data.risk_events:
        counts[(event["rule"], event["action"])] += 1
    return [(rule, action, n) for (rule, action), n in sorted(counts.items())]


@dataclass(slots=True)
class GapSummary:
    count: int = 0
    seconds: float = 0.0
    open: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


def gap_summary(data: RunData) -> dict[str, GapSummary]:
    """Recorder gaps overlapping the run's data window, clipped to it, per stream."""
    start, end = data.run["data_start"], data.run["data_end"]
    out: dict[str, GapSummary] = {}
    for gap in data.gaps:
        entry = out.setdefault(gap["stream"], GapSummary())
        entry.count += 1
        entry.open += gap["ended_ts"] is None
        stop = end if gap["ended_ts"] is None else min(end, gap["ended_ts"])
        entry.seconds += max(0, stop - max(start, gap["started_ts"])) / 1000
        entry.reasons[gap["reason"]] = entry.reasons.get(gap["reason"], 0) + 1
    return dict(sorted(out.items()))


def calibration_rows(data: RunData) -> list[tuple[str, float, int]]:
    """(condition_id, fair value at fill, 1 if YES won) for fills in markets with a known winner."""
    rows: list[tuple[str, float, int]] = []
    for fill in data.fills:
        market = data.markets.get(fill["condition_id"])
        winner = data.winners.get(fill["condition_id"])
        if market is None or winner is None or fill["fair"] is None:
            continue
        won = int(winner == market["token_id_yes"])
        rows.append((fill["condition_id"], fill["fair"], won))
    return rows

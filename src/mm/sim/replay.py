"""Merge every recorded table into one time-ordered stream of normalised events.

Order key is (time, kind priority, source order). WS book messages, deltas, trades and market
events use exchange time; REST snapshots, odds and game updates use receive time, because that is
when the system could first have acted on them.
"""

from __future__ import annotations

import heapq
import json
import statistics
from collections.abc import Iterable, Iterator
from dataclasses import replace

from mm.recorder.writer import parse_levels
from mm.storage.store import Store, meta_from_row
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
)

PRIORITY: dict[type, int] = {
    MetaChange: 0,
    MappingChange: 1,
    Gap: 2,
    BookSnapshot: 3,
    BookDelta: 4,
    Trade: 5,
    TickSizeChange: 6,
    MarketResolved: 7,
    OddsQuote: 8,
    GameStatus: 9,
    Tick: 10,
}


class NoLatencyData(RuntimeError):
    pass


def measured_latency(store: Store, start: int, end: int) -> int:
    samples = store.latency_samples(start, end)
    if not samples:
        raise NoLatencyData(
            "sim.latency_ms is null and no recorded deltas exist in the window to measure it"
        )
    return max(0, int(statistics.median(samples)))


def _meta_events(store: Store, start: int, end: int) -> Iterator[MetaChange]:
    base = {r["condition_id"]: meta_from_row(r) for r in store.markets()}
    current: dict[str, MarketMeta] = {}
    later: list[MetaChange] = []
    for row in store.meta_history(end):
        meta = base.get(row["condition_id"])
        if meta is None:
            continue
        updated = replace(
            meta,
            game_start=row["game_start_utc"],
            tick_size=row["tick_size"],
            min_size=row["min_size"],
            fee_rate=row["fee_rate"],
            fee_exponent=row["fee_exponent"],
            rebate_rate=row["rebate_rate"],
            rewards_max_spread=row["rewards_max_spread"],
            rewards_min_size=row["rewards_min_size"],
            rewards_daily_rate=row["rewards_daily_rate"],
            accepting_orders=bool(row["accepting_orders"]),
            closed=bool(row["closed"]),
        )
        if row["effective_ts"] <= start:
            current[meta.condition_id] = updated
        else:
            later.append(MetaChange(row["effective_ts"], updated))
    for cid in sorted(current):
        yield MetaChange(start, current[cid])
    yield from later


def _mapping_events(store: Store, start: int) -> Iterator[MappingChange]:
    for r in store.mappings(confirmed_only=True):
        yield MappingChange(start, r["condition_id"], r["odds_event_id"], bool(r["yes_is_home"]))


def _gap_events(store: Store, start: int, end: int) -> Iterator[Gap]:
    rows = store.gaps(start, end)
    events: list[Gap] = []
    for r in rows:
        detail = r["reason"]
        events.append(Gap(max(r["started_ts"], start), r["stream"], r["token_id"], True, detail))
        closed = r["ended_ts"] if r["ended_ts"] is not None else end
        events.append(Gap(min(closed, end), r["stream"], r["token_id"], False, detail))
    events.sort(key=lambda g: (g.ts, not g.open))
    return iter(events)


def _snapshots(store: Store, start: int, end: int, source: str) -> Iterator[BookSnapshot]:
    for r in store.iter_book_snapshots(start, end, source):
        yield BookSnapshot(
            r["ts_exchange"],
            r["ts_recv"],
            r["token_id"],
            parse_levels(r["bids_json"]),
            parse_levels(r["asks_json"]),
            r["hash"],
            "rest" if source == "rest" else "ws",
        )


def _deltas(store: Store, start: int, end: int) -> Iterator[BookDelta]:
    for r in store.iter_deltas(start, end):
        side: BookSide = "BUY" if r["side"] == "BUY" else "SELL"
        yield BookDelta(
            r["ts_exchange"], r["ts_recv"], r["token_id"], side, r["price"], r["size"], r["hash"]
        )


def _trades(store: Store, start: int, end: int) -> Iterator[Trade]:
    for r in store.iter_trades(start, end, source="ws"):
        side: BookSide = "BUY" if r["side"] == "BUY" else "SELL"
        yield Trade(
            r["ts"],
            r["ts_recv"] if r["ts_recv"] is not None else r["ts"],
            r["condition_id"],
            r["token_id"],
            side,
            r["price"],
            r["size"],
            r["tx_hash"],
        )


def _odds(store: Store, start: int, end: int) -> Iterator[OddsQuote]:
    for r in store.iter_odds(start, end):
        yield OddsQuote(
            r["ts_recv"],
            r["sport"],
            r["event_id"],
            r["commence_time"],
            r["home_team"],
            r["away_team"],
            r["bookmaker"],
            r["home_price"],
            r["away_price"],
        )


def _games(store: Store, start: int, end: int) -> Iterator[GameStatus]:
    for r in store.iter_game_status(start, end):
        yield GameStatus(
            r["ts_recv"],
            r["game_id"],
            r["league"],
            r["status"],
            bool(r["live"]),
            bool(r["ended"]),
            r["score"],
            r["period"],
        )


def _market_events(store: Store, start: int, end: int) -> Iterator[TickSizeChange | MarketResolved]:
    for r in store.iter_market_events(start, end):
        payload = json.loads(r["payload_json"])
        if r["event_type"] == "tick_size_change":
            yield TickSizeChange(
                r["ts_exchange"], r["condition_id"], r["token_id"], float(payload["new_tick_size"])
            )
        elif r["event_type"] == "market_resolved":
            yield MarketResolved(r["ts_exchange"], r["condition_id"], payload["winning_asset_id"])


def _ticks(start: int, end: int, step_ms: int) -> Iterator[Tick]:
    ts = start
    while ts <= end:
        yield Tick(ts)
        ts += step_ms


def _keyed(events: Iterable[Event]) -> Iterator[tuple[int, int, Event]]:
    for e in events:
        yield (e.ts if not isinstance(e, BookSnapshot) or e.source == "ws" else e.recv,
               PRIORITY[type(e)], e)  # fmt: skip


def replay(
    store: Store, start: int, end: int, tick_ms: int, odds_warmup_ms: int = 0
) -> Iterator[Event]:
    """Events in ``[start, end]``, plus odds received up to ``odds_warmup_ms`` before ``start``."""
    if end < start:
        raise ValueError("replay window ends before it starts")
    sources: list[Iterable[Event]] = [
        _meta_events(store, start, end),
        _mapping_events(store, start),
        _gap_events(store, start, end),
        _snapshots(store, start, end, "ws"),
        _snapshots(store, start, end, "rest"),
        _deltas(store, start, end),
        _trades(store, start, end),
        _market_events(store, start, end),
        _odds(store, start - odds_warmup_ms, end),
        _games(store, start, end),
        _ticks(start, end, tick_ms),
    ]
    merged = heapq.merge(*(_keyed(s) for s in sources), key=lambda k: (k[0], k[1]))
    for _ts, _prio, event in merged:
        yield event

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Engine,
    Table,
    and_,
    create_engine,
    delete,
    event,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import RowMapping

from mm.storage import schema as s
from mm.types import MarketMeta

Row = Mapping[str, Any]

_TRACKED_META = (
    "game_start_utc",
    "tick_size",
    "min_size",
    "fee_rate",
    "fee_exponent",
    "rebate_rate",
    "rewards_max_spread",
    "rewards_min_size",
    "rewards_daily_rate",
    "accepting_orders",
    "closed",
)


class SchemaVersionError(RuntimeError):
    pass


def _engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


def meta_from_row(row: Mapping[Any, Any]) -> MarketMeta:
    return MarketMeta(
        condition_id=row["condition_id"],
        league=row["league"],
        token_yes=row["token_id_yes"],
        token_no=row["token_id_no"],
        outcome_yes=row["outcome_yes"],
        outcome_no=row["outcome_no"],
        game_id=row["game_id"],
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


class Store:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = _engine(url)

    def init(self) -> None:
        s.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            found = conn.execute(select(s.schema_version.c.version)).scalar()
            if found is None:
                conn.execute(insert(s.schema_version).values(version=s.SCHEMA_VERSION))
            elif found != s.SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database is schema v{found}, code expects v{s.SCHEMA_VERSION}"
                )

    def close(self) -> None:
        self.engine.dispose()

    def insert(self, table: Table, rows: Sequence[Row]) -> None:
        if rows:
            with self.engine.begin() as conn:
                conn.execute(insert(table), list(rows))

    def insert_many(self, batches: Mapping[Table, Sequence[Row]]) -> None:
        with self.engine.begin() as conn:
            for table, rows in batches.items():
                if rows:
                    conn.execute(insert(table), list(rows))

    def count(self, table: Table) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(table)).scalar_one())

    def _rows(self, stmt: Any) -> list[RowMapping]:
        with self.engine.connect() as conn:
            return list(conn.execute(stmt).mappings())

    def _stream(self, stmt: Any) -> Iterator[RowMapping]:
        with self.engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(stmt)
            for chunk in result.mappings().partitions(5000):
                yield from chunk

    # markets -----------------------------------------------------------------

    def upsert_market(self, row: Row, ts: int) -> bool:
        """Insert or update a market. Returns True when a tracked field changed."""
        cid = row["condition_id"]
        with self.engine.begin() as conn:
            old = (
                conn.execute(select(s.markets).where(s.markets.c.condition_id == cid))
                .mappings()
                .first()
            )
            if old is None:
                conn.execute(insert(s.markets).values(**row, first_seen=ts, last_seen=ts))
                changed = True
            else:
                changed = any(old[k] != row[k] for k in _TRACKED_META)
                values = dict(row)
                if values.get("winning_token_id") is None:
                    values.pop("winning_token_id", None)
                conn.execute(
                    update(s.markets)
                    .where(s.markets.c.condition_id == cid)
                    .values(**values, last_seen=ts)
                )
            if changed:
                conn.execute(
                    insert(s.market_meta_history).values(
                        condition_id=cid, effective_ts=ts, **{k: row[k] for k in _TRACKED_META}
                    )
                )
        return changed

    def set_winner(self, condition_id: str, token_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(s.markets)
                .where(s.markets.c.condition_id == condition_id)
                .values(winning_token_id=token_id)
            )

    def markets(
        self, *, started_after: int | None = None, started_before: int | None = None
    ) -> list[RowMapping]:
        stmt = select(s.markets).order_by(s.markets.c.game_start_utc, s.markets.c.condition_id)
        if started_after is not None:
            stmt = stmt.where(s.markets.c.game_start_utc >= started_after)
        if started_before is not None:
            stmt = stmt.where(s.markets.c.game_start_utc <= started_before)
        return self._rows(stmt)

    def market(self, condition_id: str) -> RowMapping | None:
        rows = self._rows(select(s.markets).where(s.markets.c.condition_id == condition_id))
        return rows[0] if rows else None

    def meta_history(self, end: int) -> list[RowMapping]:
        return self._rows(
            select(s.market_meta_history)
            .where(s.market_meta_history.c.effective_ts <= end)
            .order_by(s.market_meta_history.c.effective_ts, s.market_meta_history.c.id)
        )

    # odds mapping --------------------------------------------------------------

    def upsert_mapping(self, row: Row) -> None:
        cid = row["condition_id"]
        with self.engine.begin() as conn:
            old = (
                conn.execute(
                    select(s.market_odds_map).where(s.market_odds_map.c.condition_id == cid)
                )
                .mappings()
                .first()
            )
            if old is None:
                conn.execute(insert(s.market_odds_map).values(**row))
            elif not old["confirmed"] or row["confirmed"]:
                conn.execute(
                    update(s.market_odds_map)
                    .where(s.market_odds_map.c.condition_id == cid)
                    .values(**row)
                )

    def mappings(self, *, confirmed_only: bool = False) -> list[RowMapping]:
        stmt = select(s.market_odds_map).order_by(s.market_odds_map.c.condition_id)
        if confirmed_only:
            stmt = stmt.where(s.market_odds_map.c.confirmed.is_(True))
        return self._rows(stmt)

    def odds_events(self) -> list[RowMapping]:
        o = s.odds_snapshots.c
        return self._rows(
            select(
                o.sport,
                o.event_id,
                o.home_team,
                o.away_team,
                func.max(o.commence_time).label("commence_time"),
            )
            .group_by(o.sport, o.event_id, o.home_team, o.away_team)
            .order_by(o.event_id)
        )

    # recorder bookkeeping ------------------------------------------------------

    def open_gap(
        self, stream: str, token_id: str | None, ts: int, reason: str, detail: str = ""
    ) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(
                insert(s.recorder_gaps).values(
                    stream=stream, token_id=token_id, started_ts=ts, reason=reason, detail=detail
                )
            )
            return int(result.inserted_primary_key[0])  # type: ignore[index]

    def close_gap(self, gap_id: int, ts: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(s.recorder_gaps).where(s.recorder_gaps.c.id == gap_id).values(ended_ts=ts)
            )

    def log_gap(
        self, stream: str, token_id: str | None, start: int, end: int, reason: str, detail: str = ""
    ) -> None:
        self.insert(
            s.recorder_gaps,
            [
                {
                    "stream": stream,
                    "token_id": token_id,
                    "started_ts": start,
                    "ended_ts": end,
                    "reason": reason,
                    "detail": detail,
                }
            ],
        )

    def close_dangling_gaps(self, ts: int) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(
                update(s.recorder_gaps)
                .where(s.recorder_gaps.c.ended_ts.is_(None))
                .values(ended_ts=ts)
            )
            return int(result.rowcount)

    def gaps(self, start: int, end: int) -> list[RowMapping]:
        g = s.recorder_gaps.c
        return self._rows(
            select(s.recorder_gaps)
            .where(and_(g.started_ts <= end, func.coalesce(g.ended_ts, end) >= start))
            .order_by(g.started_ts, g.id)
        )

    def heartbeat(self) -> int | None:
        with self.engine.connect() as conn:
            value = conn.execute(select(s.recorder_heartbeat.c.ts)).scalar()
            return int(value) if value is not None else None

    def set_heartbeat(self, ts: int) -> None:
        with self.engine.begin() as conn:
            hit = conn.execute(
                update(s.recorder_heartbeat).where(s.recorder_heartbeat.c.id == 1).values(ts=ts)
            )
            if hit.rowcount == 0:
                conn.execute(insert(s.recorder_heartbeat).values(id=1, ts=ts))

    def replace_backfilled_trades(self, condition_id: str, rows: Sequence[Row]) -> None:
        t = s.trades.c
        with self.engine.begin() as conn:
            conn.execute(
                delete(s.trades).where(and_(t.condition_id == condition_id, t.source == "data_api"))
            )
            if rows:
                conn.execute(insert(s.trades), list(rows))

    def conditions_needing_backfill(self) -> list[str]:
        t, m = s.trades.c, s.markets.c
        done = select(t.condition_id).where(t.source == "data_api").distinct()
        stmt = (
            select(m.condition_id)
            .where(or_(m.closed.is_(True), m.winning_token_id.is_not(None)))
            .where(m.condition_id.not_in(done))
            .order_by(m.game_start_utc, m.condition_id)
        )
        with self.engine.connect() as conn:
            return [str(v) for (v,) in conn.execute(stmt)]

    # replay readers --------------------------------------------------------------

    def data_bounds(self) -> tuple[int, int] | None:
        with self.engine.connect() as conn:
            lo, hi = conn.execute(
                select(func.min(s.book_snapshots.c.ts_recv), func.max(s.book_snapshots.c.ts_recv))
            ).one()
        return (int(lo), int(hi)) if lo is not None else None

    def latency_samples(self, start: int, end: int, limit: int = 200_000) -> list[int]:
        d = s.book_deltas.c
        stmt = (
            select(d.ts_recv - d.ts_exchange)
            .where(and_(d.ts_exchange >= start, d.ts_exchange <= end))
            .order_by(d.id)
            .limit(limit)
        )
        with self.engine.connect() as conn:
            return [int(v) for (v,) in conn.execute(stmt)]

    def iter_snapshots(
        self, start: int, end: int, tokens: Sequence[str] | None = None
    ) -> Iterator[RowMapping]:
        b = s.book_snapshots.c
        stmt = select(s.book_snapshots).where(and_(b.ts_recv >= start, b.ts_recv <= end))
        if tokens is not None:
            stmt = stmt.where(b.token_id.in_(tokens))
        return self._stream(stmt.order_by(b.ts_recv, b.id))

    def iter_book_snapshots(self, start: int, end: int, source: str) -> Iterator[RowMapping]:
        """WS snapshots in exchange-time order, REST snapshots in receive-time order."""
        b = s.book_snapshots.c
        col = b.ts_exchange if source == "ws" else b.ts_recv
        return self._stream(
            select(s.book_snapshots)
            .where(and_(b.source == source, col >= start, col <= end))
            .order_by(col, b.id)
        )

    def iter_deltas(
        self, start: int, end: int, tokens: Sequence[str] | None = None
    ) -> Iterator[RowMapping]:
        d = s.book_deltas.c
        stmt = select(s.book_deltas).where(and_(d.ts_exchange >= start, d.ts_exchange <= end))
        if tokens is not None:
            stmt = stmt.where(d.token_id.in_(tokens))
        return self._stream(stmt.order_by(d.ts_exchange, d.id))

    def iter_trades(self, start: int, end: int, source: str = "ws") -> Iterator[RowMapping]:
        t = s.trades.c
        return self._stream(
            select(s.trades)
            .where(and_(t.ts >= start, t.ts <= end, t.source == source))
            .order_by(t.ts, t.id)
        )

    def iter_odds(self, start: int, end: int) -> Iterator[RowMapping]:
        o = s.odds_snapshots.c
        return self._stream(
            select(s.odds_snapshots)
            .where(and_(o.ts_recv >= start, o.ts_recv <= end))
            .order_by(o.ts_recv, o.id)
        )

    def iter_game_status(self, start: int, end: int) -> Iterator[RowMapping]:
        g = s.game_status.c
        return self._stream(
            select(s.game_status)
            .where(and_(g.ts_recv >= start, g.ts_recv <= end))
            .order_by(g.ts_recv, g.id)
        )

    def iter_market_events(self, start: int, end: int) -> Iterator[RowMapping]:
        m = s.market_events.c
        return self._stream(
            select(s.market_events)
            .where(and_(m.ts_exchange >= start, m.ts_exchange <= end))
            .order_by(m.ts_exchange, m.id)
        )

    # runs ------------------------------------------------------------------------

    def start_run(self, row: Row) -> None:
        run_id = row["run_id"]
        with self.engine.begin() as conn:
            for table in s.RUN_TABLES:
                conn.execute(delete(table).where(table.c.run_id == run_id))
            conn.execute(delete(s.runs).where(s.runs.c.run_id == run_id))
            conn.execute(insert(s.runs).values(**row))

    def finish_run(
        self, run_id: str, ts: int, summary: Mapping[str, Any], data_end: int | None = None
    ) -> None:
        values: dict[str, Any] = {
            "finished": ts,
            "summary_json": json.dumps(summary, sort_keys=True),
        }
        if data_end is not None:
            values["data_end"] = data_end
        with self.engine.begin() as conn:
            conn.execute(update(s.runs).where(s.runs.c.run_id == run_id).values(**values))

    def run(self, run_id: str) -> RowMapping | None:
        rows = self._rows(select(s.runs).where(s.runs.c.run_id == run_id))
        return rows[0] if rows else None

    def list_runs(self) -> list[RowMapping]:
        return self._rows(select(s.runs).order_by(s.runs.c.started, s.runs.c.run_id))

    def run_rows(self, table: Table, run_id: str) -> list[RowMapping]:
        return self._rows(select(table).where(table.c.run_id == run_id).order_by(table.c.id))

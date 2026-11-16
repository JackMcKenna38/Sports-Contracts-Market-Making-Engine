from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

SCHEMA_VERSION = 1

metadata = MetaData()

_ID = BigInteger().with_variant(Integer, "sqlite")


def _pk() -> Column[int]:
    return Column("id", _ID, primary_key=True, autoincrement=True)


schema_version = Table("schema_version", metadata, Column("version", Integer, primary_key=True))

markets = Table(
    "markets",
    metadata,
    Column("condition_id", String(80), primary_key=True),
    Column("gamma_id", String(40)),
    Column("slug", String(255)),
    Column("question", Text),
    Column("league", String(16), nullable=False, index=True),
    Column("token_id_yes", String(100), nullable=False, index=True),
    Column("token_id_no", String(100), nullable=False, index=True),
    Column("outcome_yes", String(120), nullable=False),
    Column("outcome_no", String(120), nullable=False),
    Column("home", String(120)),
    Column("away", String(120)),
    Column("game_id", String(40), index=True),
    Column("game_start_utc", BigInteger, index=True),
    Column("tick_size", Float, nullable=False),
    Column("min_size", Float, nullable=False),
    Column("fee_rate", Float, nullable=False),
    Column("fee_exponent", Float, nullable=False),
    Column("rebate_rate", Float, nullable=False),
    Column("rewards_max_spread", Float, nullable=False),
    Column("rewards_min_size", Float, nullable=False),
    Column("rewards_daily_rate", Float, nullable=False),
    Column("seconds_delay", Integer),
    Column("neg_risk", Boolean, nullable=False),
    Column("accepting_orders", Boolean, nullable=False),
    Column("closed", Boolean, nullable=False),
    Column("winning_token_id", String(100)),
    Column("first_seen", BigInteger, nullable=False),
    Column("last_seen", BigInteger, nullable=False),
)

market_meta_history = Table(
    "market_meta_history",
    metadata,
    _pk(),
    Column("condition_id", String(80), nullable=False),
    Column("effective_ts", BigInteger, nullable=False),
    Column("game_start_utc", BigInteger),
    Column("tick_size", Float, nullable=False),
    Column("min_size", Float, nullable=False),
    Column("fee_rate", Float, nullable=False),
    Column("fee_exponent", Float, nullable=False),
    Column("rebate_rate", Float, nullable=False),
    Column("rewards_max_spread", Float, nullable=False),
    Column("rewards_min_size", Float, nullable=False),
    Column("rewards_daily_rate", Float, nullable=False),
    Column("accepting_orders", Boolean, nullable=False),
    Column("closed", Boolean, nullable=False),
    Index("ix_meta_history_cond_ts", "condition_id", "effective_ts"),
)

book_snapshots = Table(
    "book_snapshots",
    metadata,
    _pk(),
    Column("ts_recv", BigInteger, nullable=False),
    Column("ts_exchange", BigInteger, nullable=False),
    Column("token_id", String(100), nullable=False),
    Column("hash", String(64)),
    Column("bids_json", Text, nullable=False),
    Column("asks_json", Text, nullable=False),
    Column("source", String(8), nullable=False),
    Index("ix_book_snapshots_token_recv", "token_id", "ts_recv"),
    Index("ix_book_snapshots_recv", "ts_recv"),
)

book_deltas = Table(
    "book_deltas",
    metadata,
    _pk(),
    Column("ts_recv", BigInteger, nullable=False),
    Column("ts_exchange", BigInteger, nullable=False),
    Column("token_id", String(100), nullable=False),
    Column("side", String(4), nullable=False),
    Column("price", Float, nullable=False),
    Column("size", Float, nullable=False),
    Column("hash", String(64)),
    Index("ix_book_deltas_token_ts", "token_id", "ts_exchange"),
    Index("ix_book_deltas_ts", "ts_exchange"),
)

trades = Table(
    "trades",
    metadata,
    _pk(),
    Column("ts", BigInteger, nullable=False),
    Column("ts_recv", BigInteger),
    Column("condition_id", String(80), nullable=False),
    Column("token_id", String(100), nullable=False),
    Column("side", String(4), nullable=False),
    Column("price", Float, nullable=False),
    Column("size", Float, nullable=False),
    Column("tx_hash", String(80)),
    Column("source", String(12), nullable=False),
    Index("ix_trades_cond_ts", "condition_id", "ts"),
    Index("ix_trades_source_ts", "source", "ts"),
)

market_events = Table(
    "market_events",
    metadata,
    _pk(),
    Column("ts_recv", BigInteger, nullable=False),
    Column("ts_exchange", BigInteger, nullable=False),
    Column("condition_id", String(80), nullable=False),
    Column("token_id", String(100)),
    Column("event_type", String(32), nullable=False),
    Column("payload_json", Text, nullable=False),
    Index("ix_market_events_ts", "ts_exchange"),
)

odds_snapshots = Table(
    "odds_snapshots",
    metadata,
    _pk(),
    Column("ts_recv", BigInteger, nullable=False),
    Column("sport", String(40), nullable=False),
    Column("event_id", String(64), nullable=False),
    Column("commence_time", BigInteger, nullable=False),
    Column("home_team", String(120), nullable=False),
    Column("away_team", String(120), nullable=False),
    Column("bookmaker", String(40), nullable=False),
    Column("home_price", Float, nullable=False),
    Column("away_price", Float, nullable=False),
    Column("source", String(12), nullable=False),
    Index("ix_odds_event_ts", "event_id", "ts_recv"),
    Index("ix_odds_ts", "ts_recv"),
)

game_status = Table(
    "game_status",
    metadata,
    _pk(),
    Column("ts_recv", BigInteger, nullable=False, index=True),
    Column("game_id", String(40), nullable=False),
    Column("league", String(16), nullable=False),
    Column("status", String(40), nullable=False),
    Column("live", Boolean, nullable=False),
    Column("ended", Boolean, nullable=False),
    Column("score", String(40)),
    Column("period", String(40)),
)

market_odds_map = Table(
    "market_odds_map",
    metadata,
    Column("condition_id", String(80), primary_key=True),
    Column("odds_event_id", String(64), nullable=False),
    Column("match_method", String(32), nullable=False),
    Column("confidence", Float, nullable=False),
    Column("confirmed", Boolean, nullable=False),
    Column("yes_is_home", Boolean, nullable=False),
    Column("updated_ts", BigInteger, nullable=False),
)

recorder_gaps = Table(
    "recorder_gaps",
    metadata,
    _pk(),
    Column("stream", String(24), nullable=False),
    Column("token_id", String(100)),
    Column("started_ts", BigInteger, nullable=False),
    Column("ended_ts", BigInteger),
    Column("reason", String(40), nullable=False),
    Column("detail", Text),
    Index("ix_gaps_started", "started_ts"),
)

recorder_heartbeat = Table(
    "recorder_heartbeat",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ts", BigInteger, nullable=False),
)

quarantine = Table(
    "quarantine",
    metadata,
    _pk(),
    Column("ts_recv", BigInteger, nullable=False),
    Column("stream", String(24), nullable=False),
    Column("error", Text, nullable=False),
    Column("payload", Text, nullable=False),
)

runs = Table(
    "runs",
    metadata,
    Column("run_id", String(40), primary_key=True),
    Column("mode", String(12), nullable=False),
    Column("strategy", String(40), nullable=False),
    Column("git_sha", String(48), nullable=False),
    Column("config_hash", String(32), nullable=False),
    Column("data_start", BigInteger, nullable=False),
    Column("data_end", BigInteger, nullable=False),
    Column("started", BigInteger, nullable=False),
    Column("finished", BigInteger),
    Column("summary_json", Text),
)

sim_orders = Table(
    "sim_orders",
    metadata,
    _pk(),
    Column("run_id", String(40), nullable=False, index=True),
    Column("ts", BigInteger, nullable=False),
    Column("order_id", Integer, nullable=False),
    Column("condition_id", String(80), nullable=False),
    Column("token_id", String(100), nullable=False),
    Column("side", String(4), nullable=False),
    Column("price", Float, nullable=False),
    Column("size", Float, nullable=False),
    Column("action", String(12), nullable=False),
    Column("reason", String(40)),
    Column("fair", Float),
    Column("queue_ahead", Float),
)

sim_fills = Table(
    "sim_fills",
    metadata,
    _pk(),
    Column("run_id", String(40), nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("ts", BigInteger, nullable=False),
    Column("order_id", Integer, nullable=False),
    Column("condition_id", String(80), nullable=False),
    Column("token_id", String(100), nullable=False),
    Column("side", String(4), nullable=False),
    Column("price", Float, nullable=False),
    Column("size", Float, nullable=False),
    Column("liquidity", String(8), nullable=False),
    Column("fee", Float, nullable=False),
    Column("rebate", Float, nullable=False),
    Column("yes_price", Float, nullable=False),
    Column("yes_side", String(4), nullable=False),
    Column("fair", Float),
    Column("mid", Float),
    Column("trigger", String(16), nullable=False),
)

sim_markouts = Table(
    "sim_markouts",
    metadata,
    _pk(),
    Column("run_id", String(40), nullable=False, index=True),
    Column("fill_seq", Integer, nullable=False),
    Column("horizon_s", Integer, nullable=False),
    Column("fair_move", Float),
    Column("mid_move", Float),
)

pnl_ledger = Table(
    "pnl_ledger",
    metadata,
    _pk(),
    Column("run_id", String(40), nullable=False, index=True),
    Column("ts", BigInteger, nullable=False),
    Column("condition_id", String(80), nullable=False),
    Column("cash", Float, nullable=False),
    Column("inventory_yes", Float, nullable=False),
    Column("inventory_no", Float, nullable=False),
    Column("mark", Float),
    Column("components", Text, nullable=False),
)

risk_events = Table(
    "risk_events",
    metadata,
    _pk(),
    Column("run_id", String(40), nullable=False, index=True),
    Column("ts", BigInteger, nullable=False),
    Column("condition_id", String(80), nullable=False),
    Column("rule", String(32), nullable=False),
    Column("value", Float),
    Column("limit", Float),
    Column("action", String(24), nullable=False),
)

RUN_TABLES = (sim_orders, sim_fills, sim_markouts, pnl_ledger, risk_events)

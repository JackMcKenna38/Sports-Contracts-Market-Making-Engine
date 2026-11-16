"""Normalised events and market metadata shared by the recorder, backtester and paper trader.

All timestamps are integer milliseconds since the Unix epoch, UTC.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

Levels = tuple[tuple[float, float], ...]
BookSide = Literal["BUY", "SELL"]

PRICE_DP = 6


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; all times must carry a timezone")
    return int(dt.timestamp() * 1000)


def parse_ts(value: str) -> int:
    """Parse an ISO-8601 timestamp, including Gamma's `2026-11-16 00:00:00+00` form."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return to_ms(datetime.fromisoformat(text))


def iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_day(ts_ms: int) -> int:
    return ts_ms // 86_400_000


def px(value: float) -> float:
    return round(value, PRICE_DP)


@dataclass(frozen=True, slots=True)
class MarketMeta:
    condition_id: str
    league: str
    token_yes: str
    token_no: str
    outcome_yes: str
    outcome_no: str
    game_id: str | None
    game_start: int | None
    tick_size: float
    min_size: float
    fee_rate: float
    fee_exponent: float
    rebate_rate: float
    rewards_max_spread: float
    rewards_min_size: float
    rewards_daily_rate: float
    accepting_orders: bool
    closed: bool


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    ts: int
    recv: int
    token_id: str
    bids: Levels
    asks: Levels
    hash: str | None
    source: Literal["ws", "rest"]


@dataclass(frozen=True, slots=True)
class BookDelta:
    ts: int
    recv: int
    token_id: str
    side: BookSide
    price: float
    size: float
    hash: str | None


@dataclass(frozen=True, slots=True)
class Trade:
    ts: int
    recv: int
    condition_id: str
    token_id: str
    side: BookSide
    price: float
    size: float
    tx_hash: str | None = None


@dataclass(frozen=True, slots=True)
class OddsQuote:
    ts: int
    sport: str
    event_id: str
    commence_time: int
    home_team: str
    away_team: str
    bookmaker: str
    home_price: float
    away_price: float


@dataclass(frozen=True, slots=True)
class GameStatus:
    ts: int
    game_id: str
    league: str
    status: str
    live: bool
    ended: bool
    score: str | None
    period: str | None


@dataclass(frozen=True, slots=True)
class MarketResolved:
    ts: int
    condition_id: str
    winning_token_id: str


@dataclass(frozen=True, slots=True)
class TickSizeChange:
    ts: int
    condition_id: str
    token_id: str
    new_tick_size: float


@dataclass(frozen=True, slots=True)
class MetaChange:
    ts: int
    meta: MarketMeta


@dataclass(frozen=True, slots=True)
class MappingChange:
    ts: int
    condition_id: str
    odds_event_id: str
    yes_is_home: bool


@dataclass(frozen=True, slots=True)
class Gap:
    ts: int
    stream: str
    token_id: str | None
    open: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Tick:
    ts: int


Event = (
    BookSnapshot
    | BookDelta
    | Trade
    | OddsQuote
    | GameStatus
    | MarketResolved
    | TickSizeChange
    | MetaChange
    | MappingChange
    | Gap
    | Tick
)

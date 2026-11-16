"""Wire models for the CLOB market WebSocket, CLOB REST books and the sports WebSocket.

Market-channel and REST shapes follow the official open-source clients; the sports-channel game
object follows the build spec's field list only.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter

from mm.types import (
    BookDelta,
    BookSnapshot,
    GameStatus,
    Levels,
    MarketResolved,
    TickSizeChange,
    Trade,
)


def _ms(value: Any) -> Any:
    if isinstance(value, str):
        return int(value)
    return value


def _opt_str(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    return str(value)


EpochMs = Annotated[int, BeforeValidator(_ms)]
AnyStr = Annotated[str | None, BeforeValidator(_opt_str)]
Side = Literal["BUY", "SELL"]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Level(_Wire):
    price: float
    size: float


def _levels(levels: list[Level]) -> Levels:
    return tuple((lv.price, lv.size) for lv in levels)


class BookMsg(_Wire):
    event_type: Literal["book"]
    asset_id: str
    market: str
    timestamp: EpochMs
    bids: list[Level]
    asks: list[Level]
    hash: str | None = None

    def to_event(self, recv: int) -> BookSnapshot:
        return BookSnapshot(
            self.timestamp,
            recv,
            self.asset_id,
            _levels(self.bids),
            _levels(self.asks),
            self.hash,
            "ws",
        )


class PriceChange(_Wire):
    asset_id: str
    price: float
    size: float
    side: Side
    hash: str | None = None
    best_bid: float | None = None
    best_ask: float | None = None


class PriceChangeMsg(_Wire):
    event_type: Literal["price_change"]
    market: str
    timestamp: EpochMs
    price_changes: list[PriceChange]

    def to_events(self, recv: int) -> list[BookDelta]:
        return [
            BookDelta(self.timestamp, recv, c.asset_id, c.side, c.price, c.size, c.hash)
            for c in self.price_changes
        ]


class LastTradeMsg(_Wire):
    event_type: Literal["last_trade_price"]
    asset_id: str
    market: str
    price: float
    side: Side
    size: float
    fee_rate_bps: float | None = None
    transaction_hash: str | None = None
    timestamp: EpochMs

    def to_event(self, recv: int) -> Trade:
        return Trade(
            self.timestamp,
            recv,
            self.market,
            self.asset_id,
            self.side,
            self.price,
            self.size,
            self.transaction_hash,
        )


class TickSizeMsg(_Wire):
    event_type: Literal["tick_size_change"]
    asset_id: str
    market: str
    old_tick_size: float
    new_tick_size: float
    timestamp: EpochMs

    def to_event(self) -> TickSizeChange:
        return TickSizeChange(self.timestamp, self.market, self.asset_id, self.new_tick_size)


class BestBidAskMsg(_Wire):
    event_type: Literal["best_bid_ask"]
    market: str
    asset_id: str
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    timestamp: EpochMs


class ResolvedMsg(_Wire):
    event_type: Literal["market_resolved"]
    market: str
    assets_ids: list[str] = Field(default_factory=list)
    winning_asset_id: str
    winning_outcome: str | None = None
    timestamp: EpochMs

    def to_event(self) -> MarketResolved:
        return MarketResolved(self.timestamp, self.market, self.winning_asset_id)


class NewMarketMsg(_Wire):
    """Broadcast for every market created anywhere on the exchange; carries nothing we record."""

    event_type: Literal["new_market"]
    market: str


MarketMsg = Annotated[
    BookMsg
    | PriceChangeMsg
    | LastTradeMsg
    | TickSizeMsg
    | BestBidAskMsg
    | ResolvedMsg
    | NewMarketMsg,
    Field(discriminator="event_type"),
]
MARKET_MSG: TypeAdapter[MarketMsg] = TypeAdapter(MarketMsg)
KNOWN_EVENT_TYPES = frozenset(
    {"book", "price_change", "last_trade_price", "tick_size_change", "best_bid_ask"}
    | {"market_resolved", "new_market"}
)


class RestBook(_Wire):
    market: str
    asset_id: str
    timestamp: EpochMs
    hash: str | None = None
    bids: list[Level]
    asks: list[Level]
    min_order_size: float | None = None
    tick_size: float | None = None
    neg_risk: bool | None = None

    def to_event(self, recv: int) -> BookSnapshot:
        return BookSnapshot(
            self.timestamp,
            recv,
            self.asset_id,
            _levels(self.bids),
            _levels(self.asks),
            self.hash,
            "rest",
        )


class GameMsg(_Wire):
    game_id: Annotated[str, BeforeValidator(_opt_str)] = Field(alias="gameId")
    league_abbreviation: str = Field(alias="leagueAbbreviation")
    status: str = ""
    live: bool = False
    ended: bool = False
    score: AnyStr = None
    period: AnyStr = None

    def to_event(self, recv: int) -> GameStatus:
        return GameStatus(
            recv,
            self.game_id,
            self.league_abbreviation.lower(),
            self.status,
            self.live,
            self.ended,
            self.score,
            self.period,
        )


def market_subscribe(asset_ids: list[str], *, unsubscribe: bool = False) -> dict[str, Any]:
    return {
        "type": "market",
        "operation": "unsubscribe" if unsubscribe else "subscribe",
        "markets": [],
        "assets_ids": asset_ids,
        "initial_dump": True,
        "custom_feature_enabled": True,
    }

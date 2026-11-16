"""Gamma API response models.

Field names follow the official open-source clients; ``primaryTagId`` and ``feeSchedule`` follow
the build spec. Unknown fields are ignored, missing required fields fail validation.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Gamma(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


def _json_list(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value) if value.strip() else []
    return value


class SportRow(_Gamma):
    id: int | str
    sport: str
    tags: str = ""
    series: str | int | None = None
    primary_tag_id: int | str | None = Field(default=None, alias="primaryTagId")
    ordering: Literal["home", "away"] | None = None

    def tag_ids(self) -> tuple[str, ...]:
        return tuple(t.strip() for t in self.tags.split(",") if t.strip())


class FeeSchedule(_Gamma):
    rate: float = Field(ge=0)
    exponent: float = 1.0
    taker_only: bool = Field(default=True, alias="takerOnly")
    rebate_rate: float = Field(default=0.0, ge=0, alias="rebateRate")


class ClobReward(_Gamma):
    rewards_daily_rate: float = Field(default=0.0, alias="rewardsDailyRate")


class GammaEvent(_Gamma):
    id: str | int
    title: str | None = None
    game_id: str | int | None = Field(default=None, alias="gameId")
    home_team_name: str | None = Field(default=None, alias="homeTeamName")
    away_team_name: str | None = Field(default=None, alias="awayTeamName")
    start_time: str | None = Field(default=None, alias="startTime")
    live: bool | None = None
    ended: bool | None = None


class GammaMarket(_Gamma):
    id: str | int
    condition_id: str = Field(alias="conditionId", min_length=1)
    question: str | None = None
    slug: str | None = None
    outcomes: list[str]
    clob_token_ids: list[str] = Field(alias="clobTokenIds")
    outcome_prices: list[float] | None = Field(default=None, alias="outcomePrices")
    game_start_time: str | None = Field(default=None, alias="gameStartTime")
    order_price_min_tick_size: float = Field(alias="orderPriceMinTickSize", gt=0)
    order_min_size: float = Field(alias="orderMinSize", ge=0)
    seconds_delay: int | None = Field(default=None, alias="secondsDelay")
    rewards_min_size: float | None = Field(default=None, alias="rewardsMinSize")
    rewards_max_spread: float | None = Field(default=None, alias="rewardsMaxSpread")
    neg_risk: bool = Field(default=False, alias="negRisk")
    accepting_orders: bool = Field(default=False, alias="acceptingOrders")
    closed: bool = False
    sports_market_type: str | None = Field(default=None, alias="sportsMarketType")
    game_id: str | int | None = Field(default=None, alias="gameId")
    fee_schedule: FeeSchedule | None = Field(default=None, alias="feeSchedule")
    clob_rewards: list[ClobReward] = Field(default_factory=list, alias="clobRewards")
    events: list[GammaEvent] = Field(default_factory=list)

    @field_validator("outcomes", "clob_token_ids", "outcome_prices", mode="before")
    @classmethod
    def _decode(cls, value: Any) -> Any:
        return _json_list(value)

    @field_validator("fee_schedule", mode="before")
    @classmethod
    def _decode_fee(cls, value: Any) -> Any:
        if isinstance(value, str):
            return json.loads(value) if value.strip() else None
        return value

    @field_validator("clob_rewards", mode="before")
    @classmethod
    def _null_rewards(cls, value: Any) -> Any:
        return [] if value is None else value


class TeamRow(_Gamma):
    id: int | str
    name: str
    league: str | None = None
    abbreviation: str | None = None
    alias: str | None = None


class RewardConfig(_Gamma):
    start_date: date
    end_date: date
    rate_per_day: float = Field(ge=0)


class MarketRewards(_Gamma):
    condition_id: str
    rewards_config: list[RewardConfig]


class MarketRewardsPage(_Gamma):
    data: list[MarketRewards]

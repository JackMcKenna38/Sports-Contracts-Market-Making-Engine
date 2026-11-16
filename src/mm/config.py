from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Endpoints(_Section):
    gamma: str
    clob: str
    clob_ws: str
    data_api: str
    sports_ws: str
    odds_api: str
    geoblock: str


class Http(_Section):
    user_agent: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    backoff_base_seconds: float = Field(gt=0)
    backoff_max_seconds: float = Field(gt=0)
    min_interval_seconds: float = Field(ge=0)


class Discovery(_Section):
    refresh_minutes: float = Field(gt=0)
    page_limit: int = Field(gt=0)
    closed_lookback_days: float = Field(ge=0)
    rewards_window_hours: float = Field(ge=0)


class Recorder(_Section):
    market_ws_ping_seconds: float = Field(gt=0)
    market_ws_idle_timeout_seconds: float = Field(gt=0)
    sports_ws_pong_deadline_seconds: float = Field(gt=0)
    sports_ws_idle_timeout_seconds: float = Field(gt=0)
    rest_snapshot_seconds: float = Field(gt=0)
    books_batch_size: int = Field(gt=0, le=500)
    max_assets_per_connection: int = Field(gt=0)
    reconnect_base_seconds: float = Field(gt=0)
    reconnect_max_seconds: float = Field(gt=0)
    writer_flush_seconds: float = Field(gt=0)
    writer_max_buffer_rows: int = Field(gt=0)
    writer_max_fail_seconds: float = Field(gt=0)
    heartbeat_seconds: float = Field(gt=0)
    heartbeat_stale_seconds: float = Field(gt=0)
    drift_alert_ms: float = Field(gt=0)
    trade_backfill_limit: int = Field(gt=0, le=1000)
    trade_backfill_max_pages: int = Field(gt=0)
    record_hours_after_start: float = Field(ge=0)
    record_hours_before_start: float | None = Field(default=None, gt=0)


class Odds(_Section):
    regions: tuple[str, ...] = Field(min_length=1)
    markets: str
    sport_keys: dict[str, str]
    poll_minutes_before_start: tuple[float, ...]
    poll_on_new_game: bool
    interval_minutes: float | None = Field(default=None, gt=0)
    min_credits_reserve: int = Field(ge=0)
    check_seconds: float = Field(gt=0)
    reduced_poll_minutes_before_start: tuple[float, ...]


class Matching(_Section):
    start_window_minutes: float = Field(gt=0)
    loose_window_hours: float = Field(gt=0)
    aliases_file: Path
    review_file: Path


class FairValue(_Section):
    devig_method: Literal["multiplicative", "power", "shin"]
    anchor_bookmaker: str
    bookmaker_weights: dict[str, float]
    default_weight: float = Field(ge=0)
    max_odds_age_minutes: float = Field(gt=0)
    min_bookmakers: int = Field(ge=1)


class Quoting(_Section):
    min_price: float = Field(gt=0, lt=1)
    max_price: float = Field(gt=0, lt=1)
    half_spread: float = Field(gt=0, lt=0.5)
    quote_size: float = Field(gt=0)
    max_position_shares: float = Field(gt=0)
    skew_strength: float = Field(ge=0, lt=0.5)
    requote_threshold: float = Field(gt=0)
    cancel_threshold: float = Field(gt=0)
    stop_minutes_before_start: float = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min_price >= self.max_price:
            raise ValueError("quoting.min_price must be below quoting.max_price")
        if self.cancel_threshold < self.requote_threshold:
            raise ValueError("quoting.cancel_threshold must not be below requote_threshold")
        return self


class Risk(_Section):
    starting_capital: float = Field(gt=0)
    daily_loss_limit: float = Field(gt=0)
    max_event_exposure: float = Field(gt=0)
    loss_size_cut_fraction: float = Field(ge=0, lt=1)
    max_losses_per_event: int = Field(ge=1)
    stale_book_seconds: float = Field(gt=0)
    stale_game_feed_seconds: float = Field(gt=0)


class Sim(_Section):
    latency_ms: float | None = Field(default=None, ge=0)
    tick_seconds: float = Field(gt=0)
    ledger_interval_seconds: float = Field(gt=0)
    trade_assets: Literal["yes_only", "both"]
    fill_on_book_cross: bool
    markout_horizons_seconds: tuple[int, ...] = Field(min_length=1)


class Rewards(_Section):
    single_sided_scale: float = Field(gt=0)
    two_sided_band: tuple[float, float]
    min_daily_payout: float = Field(ge=0)
    sample_seconds: float = Field(gt=0)
    multiplier: float = Field(gt=0)


class Analytics(_Section):
    fill_distance_bucket: float = Field(gt=0, lt=1)
    calibration_bins: int = Field(ge=1)
    replay_skew_ms: int = Field(ge=0)


class Paper(_Section):
    dashboard_refresh_seconds: float = Field(gt=0)
    flush_seconds: float = Field(gt=0)


class Paths(_Section):
    data_dir: Path
    reports_dir: Path
    database_url: str


class Config(_Section):
    leagues: tuple[str, ...] = Field(min_length=1)
    market_type: str
    endpoints: Endpoints
    http: Http
    discovery: Discovery
    recorder: Recorder
    odds: Odds
    matching: Matching
    fairvalue: FairValue
    quoting: Quoting
    strategies: dict[str, dict[str, float]]
    risk: Risk
    sim: Sim
    rewards: Rewards
    analytics: Analytics
    paper: Paper
    paths: Paths

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        missing = [lg for lg in self.leagues if lg not in self.odds.sport_keys]
        if missing:
            raise ValueError(f"odds.sport_keys has no entry for leagues: {missing}")
        allowed = set(Quoting.model_fields)
        for name, overrides in self.strategies.items():
            unknown = set(overrides) - allowed
            if unknown:
                raise ValueError(f"strategies.{name} has unknown quoting keys: {sorted(unknown)}")
        return self

    def with_strategy(self, name: str) -> Config:
        if name not in self.strategies:
            raise KeyError(f"unknown strategy {name!r}; known: {sorted(self.strategies)}")
        quoting = Quoting.model_validate({**self.quoting.model_dump(), **self.strategies[name]})
        return self.model_copy(update={"quoting": quoting})

    def with_database(self, url: str) -> Config:
        return self.model_copy(
            update={"paths": self.paths.model_copy(update={"database_url": url})}
        )

    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    odds_api_key: SecretStr | None = None


def load_config(path: Path | None = None) -> Config:
    source = path or DEFAULT_CONFIG_PATH
    with source.open(encoding="utf-8") as fh:
        raw: Any = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{source} must contain a YAML mapping at the top level")
    cfg = Config.model_validate(raw)
    override = os.environ.get("MM_DATABASE_URL")
    return cfg.with_database(override) if override else cfg

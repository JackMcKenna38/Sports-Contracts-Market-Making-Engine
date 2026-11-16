from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mm.config import DEFAULT_CONFIG_PATH, Config, load_config


def _default_raw() -> dict[str, Any]:
    with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    assert isinstance(raw, dict)
    return raw


def test_default_config_loads() -> None:
    cfg = load_config()
    assert cfg.leagues == ("nba", "nhl")
    assert cfg.market_type == "moneyline"
    assert cfg.discovery.refresh_minutes == 15
    assert cfg.recorder.rest_snapshot_seconds == 60
    assert (cfg.quoting.min_price, cfg.quoting.max_price) == (0.01, 0.99)


def test_unknown_key_is_rejected() -> None:
    raw = _default_raw()
    raw["quoting"]["not_a_real_setting"] = 1
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_price_bounds_must_be_ordered() -> None:
    raw = _default_raw()
    raw["quoting"] = {"min_price": 0.9, "max_price": 0.1}
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_league_without_odds_sport_key_is_rejected() -> None:
    raw = _default_raw()
    raw["leagues"] = ["nba", "nhl", "mlb"]
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_config_is_immutable() -> None:
    cfg = load_config()
    with pytest.raises(ValidationError):
        cfg.market_type = "spread"  # type: ignore[misc]


def test_non_mapping_file_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(bad)

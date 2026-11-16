"""Integrity: no lookahead, determinism down to report bytes, and report-to-run provenance."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mm.analytics import metrics
from mm.analytics.report import write_run_report
from mm.config import Config
from mm.recorder.writer import Writer
from mm.sim.backtest import run_backtest
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import BookSnapshot, OddsQuote, Trade
from tests.unit.test_engine_backtest import CID, HOUR, MIN, _cfg, _run_rows, _seed

DECISION_TABLES = ("sim_orders", "sim_fills", "pnl_ledger", "risk_events")


def _poison_after(cfg: Config, store: Store, cut: int, start: int) -> None:
    """Synthetic future data chosen to change any decision that could see it."""
    w = Writer(cfg, store)
    w.add_odds(
        OddsQuote(cut + MIN, "basketball_nba", "ev1", start, "Synthetic Owls", "Synthetic Hawks",
                  "pinnacle", 1.01, 30.0)
    )  # fmt: skip
    w.add_trade(Trade(cut + 2 * MIN, cut + 2 * MIN, CID, "111", "SELL", 0.05, 10_000.0))
    w.add_trade(Trade(cut + 2 * MIN, cut + 2 * MIN, CID, "111", "BUY", 0.95, 10_000.0))
    w.add_snapshot(
        BookSnapshot(cut + 3 * MIN, cut + 3 * MIN + 40, "111", ((0.90, 5_000.0),),
                     ((0.91, 5_000.0),), None, "ws")
    )  # fmt: skip
    w.flush()


def _until(rows: dict[str, list[dict[str, Any]]], cut: int) -> dict[str, list[dict[str, Any]]]:
    return {name: [r for r in rows[name] if r["ts"] <= cut] for name in DECISION_TABLES}


def test_poisoned_future_data_cannot_change_earlier_decisions(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    start = _seed(cfg, store)
    lo, hi = start - 3 * HOUR, start + HOUR
    cut = start - 100 * MIN

    clean = _run_rows(store, run_backtest(cfg, store, "baseline", lo, hi, sha="test").run_id)
    _poison_after(cfg, store, cut, start)
    poisoned = _run_rows(store, run_backtest(cfg, store, "baseline", lo, hi, sha="test").run_id)

    before = _until(clean, cut)
    assert before == _until(poisoned, cut)
    assert before["sim_fills"] and before["sim_orders"] and before["pnl_ledger"]
    assert clean["sim_fills"] != poisoned["sim_fills"], "the poison must matter once it is visible"


def test_a_window_ending_before_the_poison_is_untouched(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    start = _seed(cfg, store)
    lo, cut = start - 3 * HOUR, start - 100 * MIN

    first = run_backtest(cfg, store, "baseline", lo, cut, sha="test")
    rows = _run_rows(store, first.run_id)
    _poison_after(cfg, store, cut, start)
    second = run_backtest(cfg, store, "baseline", lo, cut, sha="test")

    assert second.run_id == first.run_id
    assert second.summary == first.summary
    assert _run_rows(store, second.run_id) == rows


def test_report_of_an_engine_run_matches_its_summary_and_is_byte_identical(
    cfg: Config, store: Store
) -> None:
    cfg = _cfg(cfg)
    _seed(cfg, store)
    result = run_backtest(cfg, store, "skew", sha="test")
    data = metrics.load_run(store, result.run_id)

    attr = metrics.attribution(data)
    assert attr.pnl == pytest.approx(result.summary["pnl"])
    assert attr.fills == result.summary["fills"] == 2
    assert result.summary["never_quoted"] == {}
    assert attr.rewards_estimate == pytest.approx(result.summary["rewards_estimate"])
    assert metrics.pnl_by_league(data) == {"nba": pytest.approx(result.summary["pnl"])}
    curve = metrics.cumulative_pnl(data)
    assert curve[0][1] == 0 and curve[-1][1] == pytest.approx(result.summary["pnl"])
    assert [m.horizon_s for m in metrics.markouts(data)] == sorted(cfg.sim.markout_horizons_seconds)
    buckets = metrics.fill_rate_by_distance(data, cfg.analytics.fill_distance_bucket)
    assert sum(b.filled for b in buckets) == 2
    assert len(metrics.calibration_rows(data)) == 2

    report = write_run_report(cfg, store, result.run_id, binstats=None)
    first = {p.name: p.read_bytes() for p in report.parent.iterdir()}
    run_backtest(cfg, store, "skew", sha="test")
    write_run_report(cfg, store, result.run_id, binstats=None)
    assert {p.name: p.read_bytes() for p in report.parent.iterdir()} == first

    manifest = json.loads((report.parent / "manifest.json").read_text())
    run = store.run(manifest["run_id"])
    assert run is not None
    assert (manifest["config_hash"], manifest["git_sha"]) == (run["config_hash"], "test")
    text = report.read_text(encoding="utf-8")
    assert f"{result.summary['pnl']:+,.2f}" in text


def test_do_nothing_strategy_has_exactly_zero_pnl(cfg: Config, store: Store) -> None:
    """With quoting impossible (no odds match) every component and every ledger row is zero."""
    cfg = _cfg(cfg)
    _seed(cfg, store, mapped=False)
    result = run_backtest(cfg, store, "baseline", sha="test")
    assert store.run_rows(s.sim_orders, result.run_id) == []
    assert store.run_rows(s.sim_fills, result.run_id) == []
    assert result.summary["never_quoted"] == {"no_odds_match": 1}
    report = write_run_report(cfg, store, result.run_id, binstats=None).read_text("utf-8")
    assert "| no_odds_match | 1 |" in report
    for row in store.run_rows(s.pnl_ledger, result.run_id):
        components = json.loads(row["components"])
        assert metrics.pnl_of(components) == 0
        assert row["inventory_yes"] == row["inventory_no"] == 0
    assert metrics.attribution(metrics.load_run(store, result.run_id)).pnl == 0

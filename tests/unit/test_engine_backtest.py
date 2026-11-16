from __future__ import annotations

import json
from typing import Any

import pytest

from mm.config import Config
from mm.discovery.gamma import market_row
from mm.discovery.models import GammaMarket
from mm.recorder.writer import Writer, market_event_row
from mm.sim.backtest import NoData, backtest_run_id, run_backtest
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import BookSnapshot, OddsQuote, Trade
from tests.conftest import fixture_json

MIN = 60_000
HOUR = 60 * MIN
CID = "0xcond_synthetic_a"


def _cfg(cfg: Config, **sim: Any) -> Config:
    return cfg.model_copy(update={"sim": cfg.sim.model_copy(update={"latency_ms": 100, **sim})})


def _seed(cfg: Config, store: Store, *, mapped: bool = True, trades: bool = True) -> int:
    raw = fixture_json("gamma_markets_synthetic.json")[0]
    row = market_row(GammaMarket.model_validate(raw), "nba")
    start = row["game_start_utc"]
    store.upsert_market(row, start - 4 * HOUR)
    if mapped:
        store.upsert_mapping(
            {
                "condition_id": CID,
                "odds_event_id": "ev1",
                "match_method": "teams",
                "confidence": 1.0,
                "confirmed": True,
                "yes_is_home": False,
                "updated_ts": 0,
            }
        )
    w = Writer(cfg, store)
    t0 = start - 3 * HOUR
    for ts in range(t0, start + HOUR, MIN):
        w.add_snapshot(
            BookSnapshot(ts, ts + 40, "111", ((0.43, 100.0),), ((0.50, 100.0),), None, "ws")
        )
    w.add_odds(OddsQuote(t0, "basketball_nba", "ev1", start, "Synthetic Owls", "Synthetic Hawks",
                         "pinnacle", 1.8, 2.1))  # fmt: skip
    if trades:
        w.add_trade(Trade(start - 2 * HOUR, start - 2 * HOUR, CID, "111", "SELL", 0.441, 50.0))
        w.add_trade(Trade(start - 90 * MIN, start - 90 * MIN, CID, "111", "BUY", 0.482, 30.0))
        w.add_trade(Trade(start - 5 * MIN, start - 5 * MIN, CID, "111", "SELL", 0.30, 500.0))
    w.add(
        s.market_events,
        market_event_row(start + 30 * MIN, start + 30 * MIN, CID, None, "market_resolved",
                         {"winning_asset_id": "111"}),
    )  # fmt: skip
    w.flush()
    return int(start)


def _run_rows(store: Store, run_id: str) -> dict[str, list[dict[str, Any]]]:
    return {
        t.name: [{k: v for k, v in r.items() if k != "id"} for r in store.run_rows(t, run_id)]
        for t in s.RUN_TABLES
    }


def test_baseline_fills_merges_and_settles(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    _seed(cfg, store)
    res = run_backtest(cfg, store, "baseline", sha="test")
    rows = _run_rows(store, res.run_id)
    fills = rows["sim_fills"]
    assert [(f["yes_side"], f["yes_price"], f["size"], f["trigger"]) for f in fills] == [
        ("BUY", 0.441, 50.0, "trade"),
        ("SELL", 0.482, 30.0, "trade"),
    ]
    assert fills[1]["token_id"] == "222"
    assert fills[1]["price"] == pytest.approx(0.518)
    rebate = 0.15 * 0.05 * (50 * 0.441 * 0.559 + 30 * 0.482 * 0.518)
    cash_pnl = -50 * 0.441 - 30 * 0.518 + 30 + 20
    summary = res.summary
    assert summary["settled"] == 1
    assert summary["unsettled"] == 0
    assert summary["fills"] == 2
    assert summary["equity"] - cfg.risk.starting_capital == pytest.approx(cash_pnl + rebate)
    assert summary["pnl"] == pytest.approx(cash_pnl + rebate)
    comps = summary["components"]
    assert comps["merged"] == 30
    assert comps["spread"] + comps["inventory"] + comps["rebates"] - comps["fees"] == pytest.approx(
        summary["pnl"]
    )
    actions = {r["action"] for r in rows["sim_orders"]}
    assert {"place", "cancel"} <= actions
    assert {r["reason"] for r in rows["sim_orders"] if r["action"] == "cancel"} >= {"stop_time"}
    assert len(rows["sim_markouts"]) == 2 * len(cfg.sim.markout_horizons_seconds)
    assert all(m["fair_move"] is not None for m in rows["sim_markouts"])
    ledger = rows["pnl_ledger"]
    final = [r for r in ledger if r["condition_id"] == "*"][-1]
    assert json.loads(final["components"])["equity"] == pytest.approx(summary["equity"])
    run = store.run(res.run_id)
    assert run is not None and run["finished"] is not None
    assert json.loads(run["summary_json"])["fills"] == 2


def test_no_quotes_after_stop_time(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    start = _seed(cfg, store)
    res = run_backtest(cfg, store, "baseline", sha="test")
    stop = start - int(cfg.quoting.stop_minutes_before_start * MIN)
    places = [r for r in _run_rows(store, res.run_id)["sim_orders"] if r["action"] == "place"]
    assert places and all(r["ts"] < stop for r in places)


def test_unmatched_market_does_nothing(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    _seed(cfg, store, mapped=False)
    res = run_backtest(cfg, store, "baseline", sha="test")
    assert res.summary["fills"] == 0
    assert res.summary["pnl"] == 0
    assert res.summary["equity"] == cfg.risk.starting_capital
    assert _run_rows(store, res.run_id)["sim_orders"] == []


def test_quoting_without_trades_has_zero_pnl(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    _seed(cfg, store, trades=False)
    res = run_backtest(cfg, store, "baseline", sha="test")
    assert res.summary["fills"] == 0
    assert res.summary["pnl"] == 0
    assert res.summary["equity"] == cfg.risk.starting_capital


def test_same_run_twice_is_identical(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    _seed(cfg, store)
    a = run_backtest(cfg, store, "skew", sha="test")
    first = _run_rows(store, a.run_id)
    b = run_backtest(cfg, store, "skew", sha="test")
    assert a.run_id == b.run_id
    assert a.summary == b.summary
    assert _run_rows(store, b.run_id) == first


def test_gap_blocks_quoting_and_is_excluded(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    start = _seed(cfg, store)
    store.log_gap("market_ws", None, start - 2 * HOUR - MIN, start - 80 * MIN, "error")
    res = run_backtest(cfg, store, "baseline", sha="test")
    assert res.summary["fills"] == 0
    assert res.summary["excluded_ms"] == 41 * MIN
    reasons = {r["reason"] for r in _run_rows(store, res.run_id)["sim_orders"]}
    assert "data_gap" in reasons


def test_risk_stale_book_is_logged(cfg: Config, store: Store) -> None:
    cfg = _cfg(cfg)
    start = _seed(cfg, store)
    with store.engine.begin() as conn:
        conn.execute(
            s.book_snapshots.delete().where(
                (s.book_snapshots.c.ts_exchange > start - 150 * MIN)
                & (s.book_snapshots.c.ts_exchange < start - 140 * MIN)
            )
        )
    res = run_backtest(cfg, store, "baseline", sha="test")
    rules = [r["rule"] for r in _run_rows(store, res.run_id)["risk_events"]]
    assert "stale_book" in rules


def test_empty_database_fails_loudly(cfg: Config, store: Store) -> None:
    with pytest.raises(NoData):
        run_backtest(cfg, store, "baseline", sha="test")


def test_run_id_depends_on_inputs(cfg: Config) -> None:
    a = backtest_run_id(cfg, "x", 0, 1, "baseline")
    assert a == backtest_run_id(cfg, "x", 0, 1, "baseline")
    assert a != backtest_run_id(cfg, "y", 0, 1, "baseline")
    assert a != backtest_run_id(cfg, "x", 0, 2, "baseline")
    assert a != backtest_run_id(cfg.with_strategy("skew"), "x", 0, 1, "skew")


def test_cli_backtest_compares_strategies(
    cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mm import cli

    cfg = _cfg(cfg)
    _seed(cfg, store)
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    assert cli.main(["backtest", "--strategy", "baseline", "--strategy", "skew"]) == 0
    out = capsys.readouterr().out
    assert "baseline" in out and "skew" in out
    assert len(store.list_runs()) == 2

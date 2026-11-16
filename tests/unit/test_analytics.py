from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from mm.analytics import charts, metrics
from mm.analytics.report import MANIFEST, write_comparison, write_run_report
from mm.config import Config
from mm.storage import schema as s
from mm.storage.store import Store

T0 = 1_794_870_000_000
MIN = 60_000


def _market(cid: str, league: str, yes: str, no: str, winner: str | None) -> dict[str, Any]:
    return {
        "condition_id": cid,
        "gamma_id": cid,
        "slug": cid,
        "question": cid,
        "league": league,
        "token_id_yes": yes,
        "token_id_no": no,
        "outcome_yes": f"{cid}-home",
        "outcome_no": f"{cid}-away",
        "home": None,
        "away": None,
        "game_id": None,
        "game_start_utc": T0 + 120 * MIN,
        "tick_size": 0.01,
        "min_size": 5.0,
        "fee_rate": 0.05,
        "fee_exponent": 1.0,
        "rebate_rate": 0.15,
        "rewards_max_spread": 0.045,
        "rewards_min_size": 50.0,
        "rewards_daily_rate": 0.0,
        "seconds_delay": 0,
        "neg_risk": False,
        "accepting_orders": True,
        "closed": winner is not None,
        "winning_token_id": winner,
    }


def _order(run: str, oid: int, cid: str, token: str, price: float, fair: float) -> dict[str, Any]:
    return {
        "run_id": run,
        "ts": T0,
        "order_id": oid,
        "condition_id": cid,
        "token_id": token,
        "side": "BUY",
        "price": price,
        "size": 100.0,
        "action": "place",
        "reason": None,
        "fair": fair,
        "queue_ahead": 0.0,
    }


def _fill(
    run: str, seq: int, oid: int, cid: str, token: str, yes_side: str, yes_price: float, fair: float
) -> dict[str, Any]:
    return {
        "run_id": run,
        "seq": seq,
        "ts": T0 + seq * MIN,
        "order_id": oid,
        "condition_id": cid,
        "token_id": token,
        "side": "BUY",
        "price": yes_price if yes_side == "BUY" else round(1 - yes_price, 6),
        "size": 100.0,
        "liquidity": "maker",
        "fee": 0.0,
        "rebate": 0.1,
        "yes_price": yes_price,
        "yes_side": yes_side,
        "fair": fair,
        "mid": fair,
        "trigger": "trade",
    }


def _ledger(
    run: str, ts: int, cid: str, yes: float, no: float, components: dict[str, float]
) -> dict[str, Any]:
    return {
        "run_id": run,
        "ts": ts,
        "condition_id": cid,
        "cash": 0.0,
        "inventory_yes": yes,
        "inventory_no": no,
        "mark": None if cid == "*" else 0.5,
        "components": json.dumps(components),
    }


def seed_run(
    store: Store, run_id: str = "synthetic-run", strategy: str = "baseline", started: int = T0
) -> str:
    """A hand-built synthetic run: 3 orders, 2 fills, 2 markets, one risk event, one gap."""
    if not store.markets():
        store.upsert_market(_market("0xnba", "nba", "111", "222", "111"), T0)
        store.upsert_market(_market("0xnhl", "nhl", "333", "444", None), T0)
        store.log_gap("market:0", None, T0 + MIN, T0 + 2 * MIN, "disconnect")
        store.log_gap("sports", None, T0 - 10 * MIN, T0 + MIN, "idle")
    store.start_run(
        {
            "run_id": run_id,
            "mode": "backtest",
            "strategy": strategy,
            "git_sha": "abc1234",
            "config_hash": "cfg0000000000001",
            "data_start": T0,
            "data_end": T0 + 60 * MIN,
            "started": started,
            "finished": None,
            "summary_json": None,
        }
    )
    store.insert(
        s.sim_orders,
        [
            _order(run_id, 1, "0xnba", "111", 0.48, 0.50),
            _order(run_id, 2, "0xnba", "222", 0.48, 0.50),
            _order(run_id, 3, "0xnhl", "333", 0.41, 0.42),
            {**_order(run_id, 3, "0xnhl", "333", 0.41, 0.42), "action": "cancel"},
        ],
    )
    store.insert(
        s.sim_fills,
        [
            _fill(run_id, 1, 1, "0xnba", "111", "BUY", 0.48, 0.50),
            _fill(run_id, 2, 2, "0xnba", "222", "SELL", 0.52, 0.50),
        ],
    )
    store.insert(
        s.sim_markouts,
        [
            {
                "run_id": run_id,
                "fill_seq": 1,
                "horizon_s": 60,
                "fair_move": -0.01,
                "mid_move": -0.02,
            },
            {
                "run_id": run_id,
                "fill_seq": 2,
                "horizon_s": 60,
                "fair_move": -0.01,
                "mid_move": None,
            },
            {
                "run_id": run_id,
                "fill_seq": 1,
                "horizon_s": 300,
                "fair_move": None,
                "mid_move": None,
            },
        ],
    )
    nba = {
        "spread": 4.0,
        "inventory": -1.0,
        "rebates": 0.2,
        "fees": 0.0,
        "merged": 100,
        "volume": 200,
    }
    nhl = {"spread": 0.0, "inventory": 0.0, "rebates": 0.0, "fees": 0.0, "merged": 0, "volume": 0}
    store.insert(
        s.pnl_ledger,
        [
            _ledger(run_id, T0 + MIN, "0xnba", 100, 0, {**nba, "spread": 2.0, "inventory": 0.0}),
            _ledger(run_id, T0 + MIN, "*", 100, 0, {"spread": 2.0, "rebates": 0.1}),
            _ledger(run_id, T0 + 2 * MIN, "0xnba", 0, 0, nba),
            _ledger(run_id, T0 + 2 * MIN, "0xnhl", 0, 0, nhl),
            _ledger(run_id, T0 + 2 * MIN, "*", 0, 0, {**nba, "equity": 10_003.2}),
        ],
    )
    store.insert(
        s.risk_events,
        [
            {
                "run_id": run_id,
                "ts": T0,
                "condition_id": "0xnhl",
                "rule": "stale_book",
                "value": 200.0,
                "limit": 180.0,
                "action": "pull_quotes",
            },
            {
                "run_id": run_id,
                "ts": T0 + 1,
                "condition_id": "0xnba",
                "rule": "stale_book",
                "value": 190.0,
                "limit": 180.0,
                "action": "pull_quotes",
            },
        ],
    )
    store.finish_run(
        run_id,
        T0 + 60 * MIN,
        {
            "rewards_estimate": 1.5,
            "markets": 2,
            "settled": 1,
            "unsettled": 1,
            "latency_ms": 120,
            "excluded_ms": 60_000,
        },
    )
    return run_id


def test_attribution_separates_every_component(store: Store) -> None:
    data = metrics.load_run(store, seed_run(store))
    attr = metrics.attribution(data)
    assert (attr.spread, attr.inventory, attr.rebates, attr.fees) == (4.0, -1.0, 0.2, 0.0)
    assert attr.pnl == pytest.approx(3.2)
    assert attr.rewards_estimate == 1.5
    assert (attr.fills, attr.volume) == (2, 200.0)
    assert metrics.pnl_by_league(data) == {"nba": pytest.approx(3.2), "nhl": 0.0}
    curve = metrics.cumulative_pnl(data)
    assert [ts for ts, _ in curve] == [T0 + MIN, T0 + 2 * MIN]
    assert [v for _, v in curve] == pytest.approx([2.1, 3.2])


def test_markouts_are_signed_by_fill_side(store: Store) -> None:
    data = metrics.load_run(store, seed_run(store))
    one, five = metrics.markouts(data)
    assert (one.horizon_s, one.fair_n, one.mid_n) == (60, 2, 1)
    # buy then fair -0.01 is adverse (-0.01); sell then fair -0.01 is favourable (+0.01)
    assert one.fair_mean == pytest.approx(0.0)
    assert one.mid_mean == pytest.approx(-0.02)
    assert (five.horizon_s, five.fair_mean, five.fair_n, five.mid_mean) == (300, None, 0, None)


def test_fill_rate_buckets_use_yes_space_distance(store: Store) -> None:
    data = metrics.load_run(store, seed_run(store))
    buckets = metrics.fill_rate_by_distance(data, 0.005)
    # NO-token order at 0.48 is a YES ask at 0.52: distance 0.02, same bucket as the bid at 0.48
    assert [(round(b.lower, 3), b.placed, b.filled) for b in buckets] == [
        (0.01, 1, 0),
        (0.02, 2, 2),
    ]
    assert buckets[1].rate == 1.0


def test_risk_gap_inventory_and_calibration_summaries(store: Store) -> None:
    data = metrics.load_run(store, seed_run(store))
    assert metrics.risk_summary(data) == [("stale_book", "pull_quotes", 2)]
    gaps = metrics.gap_summary(data)
    assert (gaps["market:0"].count, gaps["market:0"].seconds) == (1, 60.0)
    assert gaps["sports"].seconds == 60.0, "gap is clipped to the data window"
    assert gaps["sports"].reasons == {"idle": 1}
    assert metrics.inventory_series(data) == {"0xnba": [(T0 + MIN, 100), (T0 + 2 * MIN, 0)]}
    assert metrics.calibration_rows(data) == [("0xnba", 0.5, 1), ("0xnba", 0.5, 1)]


def test_unknown_run_is_an_error(store: Store) -> None:
    with pytest.raises(metrics.UnknownRun):
        metrics.load_run(store, "nope")


def test_report_is_stamped_and_reproducible(cfg: Config, store: Store) -> None:
    run_id = seed_run(store)
    report = write_run_report(cfg, store, run_id, binstats=None)
    out = report.parent
    assert out == cfg.paths.reports_dir / run_id
    text = report.read_text(encoding="utf-8")
    for needle in (
        run_id,
        "cfg0000000000001",
        "abc1234",
        "2026-11-16T",
        "Spread capture",
        "+4.00",
        "**+3.20**",
        "Liquidity rewards (estimate, not in PnL) | +1.50",
        "stale_book",
        "market:0",
        "excluded from the simulation because of gaps: 60 s",
        "calibration.csv",
    ):
        assert needle in text, needle
    assert (out / "calibration.csv").read_text().splitlines() == [
        "run_id,condition_id,fair,outcome",
        f"{run_id},0xnba,0.5,1",
        f"{run_id},0xnba,0.5,1",
    ]
    first = {p.name: p.read_bytes() for p in out.iterdir()}
    write_run_report(cfg, store, run_id, binstats=None)
    assert first == {p.name: p.read_bytes() for p in out.iterdir()}


def test_every_figure_carries_a_run_id_that_exists_in_the_database(
    cfg: Config, store: Store
) -> None:
    """Provenance: no figure in reports/ without a run id, and no run id without a run."""
    for run_id in (seed_run(store, "synthetic-a"), seed_run(store, "synthetic-b", "skew")):
        write_run_report(cfg, store, run_id, binstats=None)
    pngs = sorted(cfg.paths.reports_dir.rglob("*.png"))
    assert len(pngs) == 10
    for png in pngs:
        with Image.open(png) as image:
            tag = image.text[charts.RUN_ID_KEY]  # type: ignore[attr-defined]
        run_id = tag.removeprefix("run_id=")
        assert run_id == png.parent.name
        assert store.run(run_id) is not None
        manifest = json.loads((png.parent / MANIFEST).read_text())
        assert manifest["run_id"] == run_id and png.name in manifest["files"]
    for report in cfg.paths.reports_dir.rglob("report.md"):
        assert report.parent.name in report.read_text(encoding="utf-8")


def test_empty_run_still_reports(cfg: Config, store: Store) -> None:
    store.start_run(
        {
            "run_id": "synthetic-empty",
            "mode": "backtest",
            "strategy": "baseline",
            "git_sha": "abc1234",
            "config_hash": "cfg",
            "data_start": T0,
            "data_end": T0 + MIN,
            "started": T0,
            "finished": None,
            "summary_json": None,
        }
    )
    text = write_run_report(cfg, store, "synthetic-empty", binstats=None).read_text("utf-8")
    assert "**+0.00**" in text
    assert "No risk rule was triggered." in text
    assert "No gaps were logged" in text
    assert "nothing to calibrate" in text


@pytest.mark.skipif(sys.platform == "win32", reason="runs a POSIX shell script as the binary")
def test_binstats_output_is_embedded_when_the_binary_exists(
    cfg: Config, store: Store, tmp_path: Path
) -> None:
    fake = tmp_path / "binstats"
    fake.write_text('#!/bin/sh\necho "| forecast | n |"\necho "bins=$2 file=$(basename $3)"\n')
    fake.chmod(0o755)
    text = write_run_report(cfg, store, seed_run(store), binstats=fake).read_text("utf-8")
    assert "| forecast | n |" in text
    assert "bins=10 file=calibration.csv" in text
    fake.write_text("#!/bin/sh\necho broken >&2\nexit 1\n")
    with pytest.raises(RuntimeError, match="broken"):
        write_run_report(cfg, store, "synthetic-run", binstats=fake)


def test_comparison_table_lists_each_strategy(cfg: Config, store: Store) -> None:
    ids = [seed_run(store, "synthetic-a"), seed_run(store, "synthetic-b", "skew")]
    path = write_comparison(cfg, store, ids)
    text = path.read_text(encoding="utf-8")
    assert path.name == "comparison_synthetic-a_synthetic-b.md"
    assert "|  | baseline | skew |" in text
    assert "| Run id | `synthetic-a` | `synthetic-b` |" in text
    assert "| **PnL** | **+3.20** | **+3.20** |" in text

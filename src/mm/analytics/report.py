"""`mm report`: Markdown and PNGs under ``reports/<run_id>/``, rebuilt from the database."""

from __future__ import annotations

import csv
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from mm.analytics import charts, metrics
from mm.analytics.metrics import Attribution, RunData
from mm.config import REPO_ROOT, Config
from mm.storage.store import Store
from mm.types import iso

MANIFEST = "manifest.json"
BINSTATS_BUILD = REPO_ROOT / "ocaml" / "binstats" / "_build" / "default" / "bin" / "main.exe"


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+,.2f}"


def _price(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.4f}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], right_from: int = 1) -> str:
    align = ["---" if i < right_from else "---:" for i in range(len(header))]
    lines = [header, align, *rows]
    return "\n".join("| " + " | ".join(line) + " |" for line in lines)


def _attribution_table(attr: Attribution) -> str:
    rows = [
        ("Spread capture", _money(attr.spread)),
        ("Inventory gains and losses", _money(attr.inventory)),
        ("Maker rebates (estimate)", _money(attr.rebates)),
        ("Fees paid", _money(-attr.fees)),
        ("**PnL**", f"**{_money(attr.pnl)}**"),
        ("Liquidity rewards (estimate, not in PnL)", _money(attr.rewards_estimate)),
    ]
    return _table(("Component", "USD"), rows)


def _write_calibration(data: RunData, path: Path) -> int:
    rows = metrics.calibration_rows(data)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(("run_id", "condition_id", "fair", "outcome"))
        for cid, fair, outcome in rows:
            writer.writerow((data.run_id, cid, repr(fair), outcome))
    return len(rows)


def _binstats(binary: Path | None, csv_path: Path, bins: int) -> str | None:
    if binary is None or not binary.is_file():
        return None
    result = subprocess.run(
        [str(binary), "--bins", str(bins), str(csv_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"binstats failed on {csv_path}: {result.stderr.strip()}")
    return result.stdout.strip()


def write_run_report(
    cfg: Config, store: Store, run_id: str, *, binstats: Path | None = BINSTATS_BUILD
) -> Path:
    """Write one run's report directory and return the path of its ``report.md``."""
    data = metrics.load_run(store, run_id)
    out = cfg.paths.reports_dir / run_id
    out.mkdir(parents=True, exist_ok=True)

    attr = metrics.attribution(data)
    leagues = metrics.pnl_by_league(data)
    marks = metrics.markouts(data)
    buckets = metrics.fill_rate_by_distance(data, cfg.analytics.fill_distance_bucket)
    figures = [
        charts.cumulative_pnl(data, metrics.cumulative_pnl(data), out / "cumulative_pnl.png"),
        charts.pnl_by_league(data, leagues, out / "pnl_by_league.png"),
        charts.markout_curves(data, marks, out / "markouts.png"),
        charts.inventory(data, metrics.inventory_series(data), out / "inventory.png"),
        charts.fill_rate(data, buckets, out / "fill_rate.png"),
    ]
    calibration_csv = out / "calibration.csv"
    observations = _write_calibration(data, calibration_csv)
    calibration = (
        _binstats(binstats, calibration_csv, cfg.analytics.calibration_bins)
        if observations
        else None
    )

    run = data.run
    summary = data.summary
    lines = [
        f"# Run {run_id}",
        "",
        _table(
            ("Field", "Value"),
            [
                ("Run id", f"`{run_id}`"),
                ("Mode", str(run["mode"])),
                ("Strategy", str(run["strategy"])),
                ("Data window (UTC)", f"{iso(run['data_start'])} to {iso(run['data_end'])}"),
                ("Config hash", f"`{run['config_hash']}`"),
                ("Git commit", f"`{run['git_sha']}`"),
                ("Markets", str(summary.get("markets", "n/a"))),
                (
                    "Settled / unsettled",
                    f"{summary.get('settled', 'n/a')} / {summary.get('unsettled', 'n/a')}",
                ),
                ("Fills", f"{attr.fills}"),
                ("Shares filled", f"{attr.volume:,.2f}"),
                ("Order latency (ms)", str(summary.get("latency_ms", "n/a"))),
            ],
            right_from=2,
        ),
        "",
        "Every figure below is computed from this run's rows in the database. Fills are "
        "simulated against recorded data with a back-of-queue model; no order was ever sent.",
        "",
        "## PnL attribution",
        "",
        _attribution_table(attr),
        "",
        "![Cumulative PnL](cumulative_pnl.png)",
        "",
        "## PnL by league",
        "",
        _table(("League", "PnL (USD)"), [(k.upper(), _money(v)) for k, v in leagues.items()])
        if leagues
        else "No market ledger rows.",
        "",
        "![PnL by league](pnl_by_league.png)",
        "",
        "## Markouts",
        "",
        "Mean move after a fill, signed in the direction of the fill: a negative value means "
        "the price moved against the position just taken (adverse selection).",
        "",
        _table(
            ("Horizon", "Fair value", "n", "Book mid", "n"),
            [
                (
                    f"{m.horizon_s // 60} min",
                    _price(m.fair_mean),
                    str(m.fair_n),
                    _price(m.mid_mean),
                    str(m.mid_n),
                )
                for m in marks
            ],
        )
        if marks
        else "No markouts recorded.",
        "",
        "![Markouts](markouts.png)",
        "",
        "## Inventory",
        "",
        "![Inventory](inventory.png)",
        "",
        "## Fill rate by distance from fair value",
        "",
        _table(
            ("Distance", "Placed", "Filled", "Rate"),
            [
                (f"{b.lower:.3f}-{b.upper:.3f}", str(b.placed), str(b.filled), f"{b.rate:.1%}")
                for b in buckets
            ],
        )
        if buckets
        else "No orders with a recorded fair value.",
        "",
        "![Fill rate](fill_rate.png)",
        "",
        "## Risk events",
        "",
    ]
    risk = metrics.risk_summary(data)
    lines.append(
        _table(("Rule", "Action", "Count"), [(r, a, str(n)) for r, a, n in risk], right_from=2)
        if risk
        else "No risk rule was triggered."
    )
    lines += ["", "## Recorder gaps in the data window", ""]
    gaps = metrics.gap_summary(data)
    if gaps:
        lines.append(
            _table(
                ("Stream", "Gaps", "Seconds", "Still open", "Reasons"),
                [
                    (
                        stream,
                        str(g.count),
                        f"{g.seconds:,.0f}",
                        str(g.open),
                        ", ".join(f"{k} x{v}" for k, v in sorted(g.reasons.items())),
                    )
                    for stream, g in gaps.items()
                ],
            )
        )
    else:
        lines.append("No gaps were logged in this window.")
    excluded = summary.get("excluded_ms")
    if excluded is not None:
        lines += [
            "",
            f"Market-time excluded from the simulation because of gaps: "
            f"{float(excluded) / 1000:,.0f} s.",
        ]
    never = summary.get("never_quoted")
    lines += ["", "## Markets that were never quoted", ""]
    if never:
        lines.append(
            _table(("Reason", "Markets"), [(str(k), str(v)) for k, v in sorted(never.items())])
        )
    elif never is None:
        lines.append("Not recorded for this run.")
    else:
        lines.append("Every market in the window was quoted at least once.")
    lines += ["", "## Calibration of fair value at fill", ""]
    if calibration is not None:
        lines += [
            f"Produced by `ocaml/binstats` from `calibration.csv` ({observations} fills in "
            "settled markets).",
            "",
            calibration,
        ]
    elif observations:
        lines += [
            f"`calibration.csv` holds {observations} fills in settled markets. Build "
            "`ocaml/binstats` and re-run `mm report`, or run "
            f"`binstats {calibration_csv.name}` by hand."
        ]
    else:
        lines.append("No fills in settled markets, so there is nothing to calibrate.")
    lines += ["", "---", "", charts.stamp(data), ""]

    report = out / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    manifest = {
        "run_id": run_id,
        "config_hash": run["config_hash"],
        "git_sha": run["git_sha"],
        "data_start": run["data_start"],
        "data_end": run["data_end"],
        "files": sorted([p.name for p in figures] + [report.name, calibration_csv.name]),
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")
    return report


def write_comparison(cfg: Config, store: Store, run_ids: Sequence[str]) -> Path:
    """Side-by-side table of several runs (for example baseline against inventory skew)."""
    runs = [metrics.load_run(store, run_id) for run_id in run_ids]
    attrs = [metrics.attribution(data) for data in runs]

    def row(label: str, cells: Sequence[str]) -> list[str]:
        return [label, *cells]

    rows = [
        row("Run id", [f"`{d.run_id}`" for d in runs]),
        row("Mode", [str(d.run["mode"]) for d in runs]),
        row(
            "Data window (UTC)",
            [f"{iso(d.run['data_start'])} to {iso(d.run['data_end'])}" for d in runs],
        ),
        row("Config hash", [f"`{d.run['config_hash']}`" for d in runs]),
        row("Fills", [str(a.fills) for a in attrs]),
        row("Shares filled", [f"{a.volume:,.2f}" for a in attrs]),
        row("Spread capture", [_money(a.spread) for a in attrs]),
        row("Inventory gains and losses", [_money(a.inventory) for a in attrs]),
        row("Maker rebates (estimate)", [_money(a.rebates) for a in attrs]),
        row("Fees paid", [_money(-a.fees) for a in attrs]),
        row("**PnL**", [f"**{_money(a.pnl)}**" for a in attrs]),
        row(
            "Liquidity rewards (estimate, not in PnL)", [_money(a.rewards_estimate) for a in attrs]
        ),
        row("Risk events", [str(len(d.risk_events)) for d in runs]),
    ]
    header = ["", *(str(d.run["strategy"]) for d in runs)]
    out = cfg.paths.reports_dir / ("comparison_" + "_".join(run_ids) + ".md")
    out.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        [
            "# Strategy comparison",
            "",
            _table(header, rows),
            "",
            "Each column is one run; its full report is in the folder named after its run id.",
            "",
        ]
    )
    out.write_text(text, encoding="utf-8")
    return out

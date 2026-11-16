"""Report figures. Each PNG carries its run id in the footer and in the file's metadata."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from mm.analytics.metrics import FillBucket, Markout, RunData
from mm.types import iso

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
MAX_INVENTORY_SERIES = len(SERIES)

RUN_ID_KEY = "Description"
MS_PER_DAY = 86_400_000


def stamp(data: RunData) -> str:
    run = data.run
    return (
        f"run {data.run_id} · {run['mode']} · {run['strategy']} · "
        f"data {iso(run['data_start'])} to {iso(run['data_end'])} · config {run['config_hash']}"
    )


def _figure(title: str) -> tuple[Figure, Axes]:
    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=12, fontweight="bold", pad=12)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    return fig, ax


def _label(ax: Axes, x: str = "", y: str = "") -> None:
    ax.set_xlabel(x, color=INK_SECONDARY, fontsize=9)
    ax.set_ylabel(y, color=INK_SECONDARY, fontsize=9)


def _empty(ax: Axes, message: str) -> None:
    ax.text(
        0.5,
        0.5,
        message,
        transform=ax.transAxes,
        ha="center",
        va="center",
        color=MUTED,
        fontsize=10,
    )
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)


def _save(fig: Figure, path: Path, data: RunData) -> Path:
    fig.text(0.01, 0.01, stamp(data), color=MUTED, fontsize=6.5, ha="left", va="bottom")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        path,
        facecolor=SURFACE,
        metadata={RUN_ID_KEY: f"run_id={data.run_id}", "Software": "tickbook"},
    )
    plt.close(fig)
    return path


def _times(points: Sequence[tuple[int, float]]) -> list[float]:
    """Matplotlib date numbers (days since the Unix epoch)."""
    return [ts / MS_PER_DAY for ts, _ in points]


def _time_axis(ax: Axes) -> None:
    locator = mdates.AutoDateLocator(tz=UTC)  # type: ignore[no-untyped-call]
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(
        mdates.ConciseDateFormatter(locator, tz=UTC)  # type: ignore[no-untyped-call]
    )


def cumulative_pnl(data: RunData, points: Sequence[tuple[int, float]], path: Path) -> Path:
    fig, ax = _figure("Cumulative PnL (excludes estimated rewards)")
    if points:
        ax.plot(_times(points), [v for _, v in points], color=SERIES[0], linewidth=2)
        ax.axhline(0, color=AXIS, linewidth=1)
        _time_axis(ax)
        _label(ax, "UTC", "PnL (USD)")
    else:
        _empty(ax, "no ledger rows in this run")
    return _save(fig, path, data)


def pnl_by_league(data: RunData, leagues: dict[str, float], path: Path) -> Path:
    fig, ax = _figure("PnL by league")
    if leagues:
        names = [name.upper() for name in leagues]
        values = list(leagues.values())
        bars = ax.bar(names, values, color=SERIES[0], width=0.5)
        ax.axhline(0, color=AXIS, linewidth=1)
        ax.bar_label(bars, labels=[f"{v:+,.2f}" for v in values], color=INK, fontsize=9, padding=3)
        ax.margins(y=0.15)
        _label(ax, y="PnL (USD)")
    else:
        _empty(ax, "no market ledger rows in this run")
    return _save(fig, path, data)


def markout_curves(data: RunData, rows: Sequence[Markout], path: Path) -> Path:
    fig, ax = _figure("Markouts after a fill (negative = adverse selection)")
    drawn = False
    curves = (
        ("fair value", SERIES[0], [(m.horizon_s / 60, m.fair_mean) for m in rows]),
        ("book mid", SERIES[1], [(m.horizon_s / 60, m.mid_mean) for m in rows]),
    )
    for label, color, raw in curves:
        pts = [(x, y) for x, y in raw if y is not None]
        if pts:
            xs, ys = zip(*pts, strict=True)
            ax.plot(xs, ys, color=color, linewidth=2, marker="o", markersize=6, label=label)
            drawn = True
    if drawn:
        ax.axhline(0, color=AXIS, linewidth=1)
        ax.set_xticks([m.horizon_s / 60 for m in rows])
        ax.legend(frameon=False, fontsize=9, labelcolor=INK_SECONDARY)
        _label(ax, "minutes after fill", "mean signed move (price)")
    else:
        _empty(ax, "no markouts in this run")
    return _save(fig, path, data)


def inventory(data: RunData, series: dict[str, list[tuple[int, float]]], path: Path) -> Path:
    fig, ax = _figure("Net inventory over time (YES minus NO shares)")
    if series:
        ranked = sorted(series.items(), key=lambda kv: -max(abs(n) for _, n in kv[1]))
        shown = ranked[:MAX_INVENTORY_SERIES]
        for color, (cid, points) in zip(SERIES, shown, strict=False):
            market = data.markets.get(cid)
            name = f"{market['outcome_yes']} v {market['outcome_no']}" if market else cid[:10]
            ax.step(
                _times(points),
                [n for _, n in points],
                where="post",
                color=color,
                linewidth=2,
                label=name,
            )
        ax.axhline(0, color=AXIS, linewidth=1)
        _time_axis(ax)
        title = None
        if len(ranked) > len(shown):
            title = f"largest {len(shown)} of {len(ranked)} markets"
        ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY, title=title, ncols=2)
        _label(ax, "UTC", "shares")
    else:
        _empty(ax, "no inventory was held in this run")
    return _save(fig, path, data)


def fill_rate(data: RunData, buckets: Sequence[FillBucket], path: Path) -> Path:
    fig, ax = _figure("Fill rate by quote distance from fair value")
    if buckets:
        labels = [f"{b.lower:.3f}-{b.upper:.3f}" for b in buckets]
        bars = ax.bar(labels, [100 * b.rate for b in buckets], color=SERIES[0], width=0.6)
        ax.bar_label(
            bars,
            labels=[f"{b.filled}/{b.placed}" for b in buckets],
            color=INK,
            fontsize=8,
            padding=3,
        )
        ax.margins(y=0.15)
        ax.tick_params(axis="x", labelrotation=30)
        _label(ax, "|quote price - fair value| at placement", "orders with a fill (%)")
    else:
        _empty(ax, "no orders with a recorded fair value in this run")
    return _save(fig, path, data)

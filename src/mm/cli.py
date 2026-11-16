"""Command line entry point: ``mm discover | record | backtest | paper | report | verify``."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from mm.config import Config, load_config
from mm.net import setup_logging

Handler = Callable[[argparse.Namespace, Config], int]

COMMANDS: dict[str, str] = {
    "discover": "List NBA and NHL moneyline markets from the Gamma API",
    "record": "Record order books, trades, odds and game status",
    "backtest": "Replay recorded data through the strategy and fill simulator",
    "paper": "Run the strategy on live data with simulated orders and fills",
    "report": "Write the results report and charts into reports/",
    "verify": "Check books rebuilt from recorded deltas against REST checkpoints",
    "status": "Show recorder heartbeat, row counts and the gap log",
}


def _discover(_args: argparse.Namespace, cfg: Config) -> int:
    from mm.discovery.gamma import Discovery, GammaClient
    from mm.net import Http
    from mm.storage.store import Store
    from mm.types import iso, now_ms

    store = Store(cfg.paths.database_url)
    store.init()

    async def go() -> int:
        http = Http(cfg)
        try:
            result = await Discovery(cfg, GammaClient(cfg, http), store).run_once(
                datetime.now(UTC), now_ms()
            )
        finally:
            await http.aclose()
        table = Table(title=f"{len(result.markets)} {cfg.market_type} markets")
        for col in ("league", "start (UTC)", "yes", "no", "tick", "min", "fee", "rebate", "open"):
            table.add_column(col)
        for m in result.markets:
            table.add_row(
                m.league,
                iso(m.game_start) if m.game_start else "-",
                f"{m.outcome_yes} {m.token_yes}",
                f"{m.outcome_no} {m.token_no}",
                f"{m.tick_size:g}",
                f"{m.min_size:g}",
                f"{m.fee_rate:g}^{m.fee_exponent:g}",
                f"{m.rebate_rate:g}",
                "no" if m.closed or not m.accepting_orders else "yes",
            )
        Console().print(table)
        return 0

    try:
        return asyncio.run(go())
    finally:
        store.close()


def _record(_args: argparse.Namespace, cfg: Config) -> int:
    from mm.config import Secrets
    from mm.net import Http
    from mm.recorder.service import Recorder
    from mm.storage.store import Store

    store = Store(cfg.paths.database_url)

    async def go() -> None:
        http = Http(cfg)
        try:
            await Recorder(cfg, store, http, odds_key=Secrets().odds_api_key).run()
        finally:
            await http.aclose()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        return 130
    finally:
        store.close()
    return 0


def _utc_ms(text: str) -> int:
    from mm.types import parse_ts

    value = text if ("+" in text[10:] or text.endswith("Z")) else text + "+00:00"
    return parse_ts(value)


def _backtest_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy", action="append", help="strategy name; repeat to compare")
    p.add_argument("--start", type=_utc_ms, help="window start, ISO-8601 (UTC if no offset)")
    p.add_argument("--end", type=_utc_ms, help="window end, ISO-8601 (UTC if no offset)")


def _backtest(args: argparse.Namespace, cfg: Config) -> int:
    from mm.sim.backtest import run_backtest
    from mm.storage.store import Store
    from mm.types import iso

    store = Store(cfg.paths.database_url)
    store.init()
    try:
        names = args.strategy or sorted(cfg.strategies)
        table = Table(title="backtest")
        for col in ("strategy", "run id", "window", "fills", "pnl", "rewards (est.)", "excluded h"):
            table.add_column(col)
        for name in names:
            res = run_backtest(cfg, store, name, args.start, args.end)
            sm = res.summary
            table.add_row(
                name,
                res.run_id,
                f"{iso(res.start)} .. {iso(res.end)}",
                str(sm["fills"]),
                f"{sm['pnl']:.2f}",
                f"{sm['rewards_estimate']:.2f}",
                f"{sm['excluded_ms'] / 3_600_000:.1f}",
            )
        Console().print(table)
    finally:
        store.close()
    return 0


def _paper_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy", default="baseline", help="strategy name (default: baseline)")
    p.add_argument("--no-dashboard", action="store_true", help="log only; for unattended runs")
    p.add_argument("--minutes", type=float, help="stop after this many minutes (default: run on)")


def _paper(args: argparse.Namespace, cfg: Config) -> int:
    from mm.config import Secrets
    from mm.net import Http
    from mm.paper.trader import PaperTrader, paper_latency
    from mm.recorder.service import Recorder
    from mm.storage.store import Store
    from mm.types import now_ms

    store = Store(cfg.paths.database_url)
    store.init()
    trader = PaperTrader(cfg, store, args.strategy, latency_ms=paper_latency(cfg, store, now_ms()))

    async def go() -> None:
        http = Http(cfg)
        recorder = Recorder(
            cfg, store, http, odds_key=Secrets().odds_api_key, listeners=[trader.on_event]
        )
        try:
            await trader.run(
                recorder,
                dashboard=not args.no_dashboard,
                duration=None if args.minutes is None else args.minutes * 60,
            )
        finally:
            await http.aclose()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
    finally:
        trader.finish()
        store.close()
    print(f"paper run {trader.run_id} written; `mm report --run {trader.run_id}` renders it")
    return 0


def _report_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--run",
        action="append",
        help="run id; repeat to add a comparison table (default: newest run of each strategy)",
    )


def _default_runs(runs: Sequence[Mapping[Any, Any]]) -> list[str]:
    """Newest finished run per (mode, strategy), backtests first."""
    newest: dict[tuple[str, str], str] = {}
    for run in runs:
        if run["finished"] is not None:
            newest[(str(run["mode"]), str(run["strategy"]))] = str(run["run_id"])
    return [newest[key] for key in sorted(newest)]


def _report(args: argparse.Namespace, cfg: Config) -> int:
    from mm.analytics.metrics import UnknownRun
    from mm.analytics.report import write_comparison, write_run_report
    from mm.storage.store import Store

    store = Store(cfg.paths.database_url)
    store.init()
    try:
        run_ids = args.run or _default_runs(store.list_runs())
        if not run_ids:
            print("mm report: the database has no finished run; run `mm backtest`", file=sys.stderr)
            return 1
        try:
            for run_id in run_ids:
                print(write_run_report(cfg, store, run_id))
            if len(run_ids) > 1:
                print(write_comparison(cfg, store, run_ids))
        except UnknownRun as exc:
            print(f"mm report: {exc}", file=sys.stderr)
            return 1
    finally:
        store.close()
    return 0


def _verify_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--start", type=_utc_ms, help="window start, ISO-8601 (UTC if no offset)")
    p.add_argument("--end", type=_utc_ms, help="window end, ISO-8601 (UTC if no offset)")


def _verify(args: argparse.Namespace, cfg: Config) -> int:
    from mm.analytics.replay_check import check_replay
    from mm.storage.store import Store
    from mm.types import iso

    store = Store(cfg.paths.database_url)
    store.init()
    try:
        bounds = store.data_bounds()
        if bounds is None:
            print("mm verify: no recorded book snapshots in the database", file=sys.stderr)
            return 1
        start = args.start if args.start is not None else bounds[0]
        end = args.end if args.end is not None else bounds[1]
        result = check_replay(store, start, end, cfg.analytics.replay_skew_ms)
        unlogged = result.unlogged(store.gaps(start, end))
    finally:
        store.close()
    table = Table(title=f"replay accuracy {iso(start)} .. {iso(end)}")
    for col in (
        "REST checkpoints",
        "matched",
        "matched within skew",
        "mismatched, logged by recorder",
        "mismatched, not logged",
        "no stream book",
    ):
        table.add_column(col, justify="right")
    table.add_row(
        str(result.checkpoints),
        str(result.matched),
        str(result.matched_after_skew),
        str(len(result.mismatches) - len(unlogged)),
        str(len(unlogged)),
        str(result.no_stream_book),
    )
    Console().print(table)
    for miss in unlogged[:20]:
        print(
            f"mismatch: token {miss.token_id} at {iso(miss.ts)} hash {miss.hash}", file=sys.stderr
        )
    return 1 if unlogged else 0


def _status_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--require-live",
        action="store_true",
        help="exit 1 unless a recorder heartbeat is recent (container health check)",
    )


def _status(args: argparse.Namespace, cfg: Config) -> int:
    from mm.analytics.status import read_status
    from mm.storage.store import Store
    from mm.types import iso, now_ms

    store = Store(cfg.paths.database_url)
    store.init()
    try:
        status = read_status(cfg, store, now_ms())
    finally:
        store.close()
    age = status.heartbeat_age_ms
    table = Table(title="recorder status", show_header=False)
    table.add_row("recorder", "running" if status.live else "NOT RUNNING")
    table.add_row("last heartbeat", "never" if age is None else f"{age / 1000:,.0f} s ago")
    window = status.data_window
    table.add_row(
        "books recorded", "none" if window is None else f"{iso(window[0])} .. {iso(window[1])}"
    )
    table.add_row("open markets", str(status.markets_open))
    table.add_row(
        "odds matches",
        f"{status.mappings_confirmed} confirmed, {status.mappings_pending} to review",
    )
    table.add_row("last odds", "never" if status.last_odds is None else iso(status.last_odds))
    for name, count in status.rows.items():
        table.add_row(f"rows: {name}", f"{count:,}")
    console = Console()
    console.print(table)
    gaps = Table(title="gap log")
    for col in ("stream", "reason", "gaps", "seconds", "still open"):
        gaps.add_column(col)
    for line in status.gaps:
        gaps.add_row(
            line.stream, line.reason, str(line.count), f"{line.seconds:,.0f}", str(line.open)
        )
    if status.gaps:
        console.print(gaps)
    else:
        console.print("gap log: empty")
    return 1 if args.require_live and not status.live else 0


HANDLERS: dict[str, Handler] = {
    "discover": _discover,
    "record": _record,
    "backtest": _backtest,
    "paper": _paper,
    "report": _report,
    "verify": _verify,
    "status": _status,
}
ARGS: dict[str, Callable[[argparse.ArgumentParser], None]] = {
    "backtest": _backtest_args,
    "paper": _paper_args,
    "report": _report_args,
    "verify": _verify_args,
    "status": _status_args,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mm",
        description=(
            "Research market-making engine for Polymarket NBA and NHL moneylines. "
            "Records real data and simulates orders; it never places a live order."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to a YAML config file (default: config/default.yaml)",
    )
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    for name, help_text in COMMANDS.items():
        cmd = sub.add_parser(name, help=help_text, description=help_text)
        if name in ARGS:
            ARGS[name](cmd)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(args.log_level)
    from mm.recorder.service import RecorderAlreadyRunning
    from mm.sim.backtest import NoData
    from mm.sim.replay import NoLatencyData

    try:
        return HANDLERS[args.command](args, cfg)
    except (NoData, NoLatencyData, RecorderAlreadyRunning) as exc:
        print(f"mm {args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

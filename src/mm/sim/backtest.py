from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

from mm.config import REPO_ROOT, Config
from mm.engine import Engine
from mm.sim.replay import measured_latency, replay
from mm.storage.store import Store
from mm.types import iso, now_ms

log = logging.getLogger(__name__)


class NoData(RuntimeError):
    pass


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def backtest_run_id(cfg: Config, sha: str, start: int, end: int, strategy: str) -> str:
    key = f"backtest|{cfg.digest()}|{sha}|{start}|{end}|{strategy}"
    return "bt-" + hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    run_id: str
    start: int
    end: int
    summary: dict[str, Any]


def run_backtest(
    cfg: Config,
    store: Store,
    strategy: str,
    start: int | None = None,
    end: int | None = None,
    *,
    sha: str | None = None,
    flush_every: int = 20_000,
) -> BacktestResult:
    bounds = store.data_bounds()
    if bounds is None:
        raise NoData("no recorded book snapshots in the database; nothing to backtest")
    lo, hi = start if start is not None else bounds[0], end if end is not None else bounds[1]
    if hi <= lo:
        raise NoData(f"empty backtest window {iso(lo)} .. {iso(hi)}")
    scfg = cfg.with_strategy(strategy)
    latency = (
        int(scfg.sim.latency_ms)
        if scfg.sim.latency_ms is not None
        else measured_latency(store, lo, hi)
    )
    commit = sha if sha is not None else git_sha()
    run_id = backtest_run_id(scfg, commit, lo, hi, strategy)
    store.start_run(
        {
            "run_id": run_id,
            "mode": "backtest",
            "strategy": strategy,
            "git_sha": commit,
            "config_hash": scfg.digest(),
            "data_start": lo,
            "data_end": hi,
            "started": now_ms(),
        }
    )
    engine = Engine(scfg, run_id, latency)
    n = 0
    warmup = int(scfg.fairvalue.max_odds_age_minutes * 60_000)
    for event in replay(store, lo, hi, int(scfg.sim.tick_seconds * 1000), warmup):
        engine.on_event(event)
        n += 1
        if n % flush_every == 0:
            store.insert_many(engine.drain())
    winners = {
        r["condition_id"]: r["winning_token_id"]
        for r in store.markets()
        if r["winning_token_id"] is not None
        and r["condition_id"] in engine.markets
        and r["game_start_utc"] is not None
        and r["game_start_utc"] <= hi
    }
    engine.settle_from_final_state(winners)
    summary = engine.finish()
    summary["events"] = n
    store.insert_many(engine.drain())
    store.finish_run(run_id, now_ms(), summary)
    log.info("backtest %s over %s .. %s: %s", run_id, iso(lo), iso(hi), summary)
    return BacktestResult(run_id, lo, hi, summary)

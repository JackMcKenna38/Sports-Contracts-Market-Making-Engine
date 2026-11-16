"""Runs the recorder against live data for five minutes and checks rows land in the tables."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy import Table

from mm.analytics.replay_check import check_replay
from mm.config import Config, Secrets
from mm.net import Http
from mm.recorder.service import Recorder
from mm.storage import schema as s
from mm.storage.store import Store

pytestmark = pytest.mark.integration

RUN_SECONDS = 300


def test_recorder_fills_its_tables_in_five_minutes(cfg: Config, store: Store) -> None:
    async def go() -> Recorder:
        http = Http(cfg)
        recorder = Recorder(cfg, store, http, odds_key=Secrets().odds_api_key)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(recorder.run(), RUN_SECONDS)
        finally:
            await http.aclose()
        return recorder

    recorder = asyncio.run(go())
    counts = {t.name: store.count(t) for t in s.metadata.sorted_tables}
    print(counts)

    if not recorder.feed.tokens:
        pytest.skip(f"no open market to record (off-season?); table counts: {counts}")

    always: tuple[Table, ...] = (
        s.markets,
        s.market_meta_history,
        s.book_snapshots,
        s.recorder_heartbeat,
    )
    for table in always:
        assert counts[table.name] > 0, f"no rows in {table.name}: {counts}"

    sources = {row["source"] for row in store.iter_snapshots(0, 2**62)}
    assert sources == {"ws", "rest"}, f"snapshot sources recorded: {sources}"
    assert recorder.feed.quarantined == 0, "market messages failed validation; see quarantine"
    assert counts["quarantine"] == 0, "messages failed validation; see the quarantine table"
    accuracy = check_replay(store, 0, 2**62, cfg.analytics.replay_skew_ms)
    print(accuracy.checkpoints, accuracy.matched, accuracy.matched_after_skew, accuracy.mismatches)
    assert accuracy.verified > 0
    assert accuracy.unlogged(store.gaps(0, 2**62)) == [], (
        "rebuilt books diverged from REST checkpoints without the recorder logging it"
    )
    assert not [g for g in store.gaps(0, 2**62) if g["ended_ts"] is None], "a gap was left open"

    # Activity-dependent tables: quiet markets can legitimately leave these empty in five
    # minutes, so they are reported above rather than asserted.

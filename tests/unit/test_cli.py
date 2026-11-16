from __future__ import annotations

import pytest

from mm.cli import COMMANDS, HANDLERS, main
from mm.config import Config
from mm.storage import schema as s
from mm.storage.store import Store
from tests.unit.test_analytics import seed_run
from tests.unit.test_replay_accuracy import ASKS, BIDS, T0
from tests.unit.test_replay_accuracy import _seed as seed_books
from tests.unit.test_replay_accuracy import _snap as rest_snap


def test_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in COMMANDS:
        assert name in out


def test_no_command_is_an_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0


def test_every_command_has_a_handler() -> None:
    assert set(COMMANDS) == set(HANDLERS)


@pytest.fixture
def cli_cfg(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    from mm import cli

    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    return cfg


def test_report_without_runs_fails_loudly(
    cli_cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["report"]) == 1
    assert "no finished run" in capsys.readouterr().err
    assert main(["report", "--run", "nope"]) == 1
    assert "nope" in capsys.readouterr().err


def test_report_defaults_to_newest_run_per_strategy_and_compares(
    cli_cfg: Config, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_run(store, "synthetic-old", started=T0)
    seed_run(store, "synthetic-a", started=T0 + 1)
    seed_run(store, "synthetic-b", "skew", started=T0 + 2)
    assert main(["report"]) == 0
    out = capsys.readouterr().out
    reports = cli_cfg.paths.reports_dir
    assert (reports / "synthetic-a" / "report.md").is_file()
    assert (reports / "synthetic-b" / "report.md").is_file()
    assert not (reports / "synthetic-old").exists()
    assert (reports / "comparison_synthetic-a_synthetic-b.md").is_file()
    assert "comparison_synthetic-a_synthetic-b.md" in out
    assert main(["report", "--run", "synthetic-old"]) == 0
    assert (reports / "synthetic-old" / "report.md").is_file()


def test_verify_fails_only_on_mismatches_the_recorder_did_not_log(
    cli_cfg: Config, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["verify"]) == 1
    assert "no recorded book snapshots" in capsys.readouterr().err
    seed_books(store, [rest_snap(T0 + 500, BIDS, ASKS, "h0", "rest")])
    assert main(["verify"]) == 0
    wrong = rest_snap(T0 + 2500, ((0.47, 250.0), (0.49, 45.0)), ASKS, "h2", "rest")
    store.insert(s.book_snapshots, [wrong])
    capsys.readouterr()
    assert main(["verify", "--start", "2026-11-16T00:00:00"]) == 1
    assert "mismatch: token 111" in capsys.readouterr().err
    store.log_gap("market_ws", "111", wrong["ts_recv"], wrong["ts_recv"], "lost_update")
    assert main(["verify"]) == 0
    assert "mismatch" not in capsys.readouterr().err


def test_missing_data_is_a_clean_error_not_a_traceback(
    cli_cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["paper", "--no-dashboard", "--minutes", "0.01"]) == 1
    assert "set sim.latency_ms" in capsys.readouterr().err
    assert main(["backtest"]) == 1
    assert "nothing to backtest" in capsys.readouterr().err


def test_status_summarises_the_gap_log_and_checks_the_heartbeat(
    cli_cfg: Config, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    from mm.analytics.status import read_status
    from mm.types import now_ms

    assert main(["status"]) == 0
    assert "NOT RUNNING" in capsys.readouterr().out
    assert main(["status", "--require-live"]) == 1
    capsys.readouterr()

    now = now_ms()
    seed_run(store)
    store.log_gap("market_ws", "111", now - 5000, now - 5000, "lost_update")
    store.open_gap("sports_ws", None, now - 30_000, "idle_timeout")
    store.set_heartbeat(now - 1000)
    status = read_status(cli_cfg, store, now)
    assert status.live and status.heartbeat_age_ms == 1000
    assert (status.markets_open, status.rows["markets"]) == (1, 2)
    by_key = {(g.stream, g.reason): g for g in status.gaps}
    assert (
        by_key[("sports_ws", "idle_timeout")].open,
        by_key[("sports_ws", "idle_timeout")].seconds,
    ) == (1, 30.0)
    assert by_key[("market_ws", "lost_update")].count == 1

    assert main(["status", "--require-live"]) == 0
    out = capsys.readouterr().out
    assert "running" in out and "lost_update" in out and "idle_timeout" in out
    store.set_heartbeat(now - 10 * 60_000)
    assert main(["status", "--require-live"]) == 1

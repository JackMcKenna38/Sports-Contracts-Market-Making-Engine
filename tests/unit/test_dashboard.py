from __future__ import annotations

from rich.console import Console

from mm.paper.dashboard import BANNER, DashboardView, MarketView, render
from mm.risk.engine import RiskEvent

NOW = 1_794_870_000_000


def _text(view: DashboardView) -> str:
    console = Console(width=200, record=True, force_terminal=False)
    console.print(render(view, risk_rows=2))
    return console.export_text()


def _view(**overrides: object) -> DashboardView:
    base: dict[str, object] = {
        "run_id": "paper-synthetic",
        "now": NOW,
        "cash": 9_950.0,
        "equity": 10_003.2,
        "pnl": 3.2,
        "components": {"spread": 4.0, "inventory": -1.0, "rebates": 0.2, "fees": 0.0},
        "rewards_estimate": 0.75,
        "fills": 2,
        "halted": False,
        "feeds": {"market": True, "sports": False},
        "markets": (
            MarketView(
                "Hawks v Owls",
                "nba",
                NOW + 3_600_000,
                0.5012,
                (0.48, 100.0),
                None,
                0.47,
                0.53,
                100.0,
                0.0,
                3.2,
                "quoting",
            ),
            MarketView(
                "Bears v Wolves",
                "nhl",
                None,
                None,
                None,
                None,
                None,
                None,
                0.0,
                0.0,
                0.0,
                "stale_odds",
            ),
        ),
        "risk_events": tuple(
            RiskEvent(NOW + i, "0xcond_synthetic_a", f"rule{i}", 1.0 + i, 2.0, "pull_quotes")
            for i in range(3)
        ),
    }
    return DashboardView(**{**base, **overrides})  # type: ignore[arg-type]


def test_dashboard_shows_quotes_inventory_pnl_and_risk_state() -> None:
    text = _text(_view())
    assert BANNER in text
    for needle in (
        "paper-synthetic", "2026-11-16T23:00:00Z", "sports:DOWN", "market:up", "quoting",
        "10,003.20", "+3.20", "+4.00", "-1.00", "0.75",
        "NBA Hawks v Owls", "0.501", "0.480 x 100", "0.470", "0.530", "stale_odds",
        "rule1", "rule2", "pull_quotes",
    ):  # fmt: skip
        assert needle in text, needle
    assert "rule0" not in text, "only the latest risk events are shown"


def test_dashboard_shows_halt_and_empty_states() -> None:
    text = _text(_view(halted=True, markets=(), risk_events=()))
    assert "HALTED: daily loss cap" in text
    assert "no markets discovered" in text
    assert "none" in text

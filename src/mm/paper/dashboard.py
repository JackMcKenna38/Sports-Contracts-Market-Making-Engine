"""Terminal dashboard for paper trading. Pure rendering: it only reads a `DashboardView`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mm.risk.engine import RiskEvent
from mm.types import iso

BANNER = "PAPER TRADING · simulated orders and fills · no order is ever sent"


@dataclass(frozen=True, slots=True)
class MarketView:
    label: str
    league: str
    game_start: int | None
    fair: float | None
    bid: tuple[float, float] | None
    ask: tuple[float, float] | None
    best_bid: float | None
    best_ask: float | None
    inventory_yes: float
    inventory_no: float
    pnl: float
    state: str


@dataclass(frozen=True, slots=True)
class DashboardView:
    run_id: str
    now: int
    cash: float
    equity: float
    pnl: float
    components: dict[str, float]
    rewards_estimate: float
    fills: int
    halted: bool
    feeds: dict[str, bool]
    markets: Sequence[MarketView]
    risk_events: Sequence[RiskEvent]


def _px(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _quote(quote: tuple[float, float] | None) -> str:
    return "-" if quote is None else f"{quote[0]:.3f} x {quote[1]:g}"


def _money(value: float) -> Text:
    style = "green" if value > 0 else "red" if value < 0 else ""
    return Text(f"{value:+,.2f}", style=style)


def _header(view: DashboardView) -> Panel:
    feeds = Text()
    for name, up in sorted(view.feeds.items()):
        feeds.append(f" {name}:", style="dim")
        feeds.append("up" if up else "DOWN", style="green" if up else "bold red")
    grid = Table.grid(padding=(0, 3))
    grid.add_row(
        Text(f"run {view.run_id}", style="bold"),
        Text(iso(view.now)),
        Text("HALTED: daily loss cap", style="bold red") if view.halted else Text("quoting"),
        feeds,
    )
    return Panel(grid, title=BANNER, title_align="left", border_style="yellow")


def _pnl(view: DashboardView) -> Table:
    table = Table(title="PnL", title_justify="left", expand=True)
    for column in ("cash", "equity", "PnL", "spread", "inventory", "rebates", "fees", "fills"):
        table.add_column(column, justify="right")
    table.add_column("rewards (est.)", justify="right")
    c = view.components
    table.add_row(
        f"{view.cash:,.2f}",
        f"{view.equity:,.2f}",
        _money(view.pnl),
        _money(c.get("spread", 0.0)),
        _money(c.get("inventory", 0.0)),
        _money(c.get("rebates", 0.0)),
        _money(-c.get("fees", 0.0)),
        str(view.fills),
        f"{view.rewards_estimate:,.2f}",
    )
    return table


def _markets(view: DashboardView) -> Table:
    table = Table(title="Markets", title_justify="left", expand=True)
    table.add_column("market")
    table.add_column("start (UTC)")
    for column in ("fair", "our bid", "our ask", "book bid", "book ask", "YES", "NO", "PnL"):
        table.add_column(column, justify="right")
    table.add_column("state")
    for m in view.markets:
        table.add_row(
            f"{m.league.upper()} {m.label}",
            iso(m.game_start) if m.game_start is not None else "-",
            _px(m.fair),
            _quote(m.bid),
            _quote(m.ask),
            _px(m.best_bid),
            _px(m.best_ask),
            f"{m.inventory_yes:g}",
            f"{m.inventory_no:g}",
            _money(m.pnl),
            Text(m.state, style="" if m.state == "quoting" else "yellow"),
        )
    if not view.markets:
        table.add_row("no markets discovered", *[""] * 10)
    return table


def _risk(view: DashboardView, limit: int) -> Table:
    table = Table(title="Latest risk events", title_justify="left", expand=True)
    for column in ("time (UTC)", "market", "rule", "action"):
        table.add_column(column)
    table.add_column("value", justify="right")
    table.add_column("limit", justify="right")
    for event in list(view.risk_events)[-limit:]:
        table.add_row(
            iso(event.ts),
            event.condition_id[:14],
            event.rule,
            event.action,
            f"{event.value:,.2f}",
            f"{event.limit:,.2f}",
        )
    if not view.risk_events:
        table.add_row("none", "", "", "", "", "")
    return table


def render(view: DashboardView, risk_rows: int = 8) -> RenderableType:
    return Group(_header(view), _pnl(view), _markets(view), _risk(view, risk_rows))

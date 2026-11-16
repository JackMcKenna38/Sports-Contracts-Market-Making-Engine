from __future__ import annotations

import csv
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mm.config import Config
from mm.discovery.gamma import market_row
from mm.discovery.models import GammaMarket
from mm.fairvalue.consensus import FairValue, LookaheadError
from mm.fairvalue.devig import devig, implied, multiplicative, power, shin
from mm.fairvalue.matcher import (
    Aliases,
    MarketTeams,
    Matcher,
    OddsEventRef,
    norm,
    run_matching,
)
from mm.storage.store import Store
from mm.types import OddsQuote
from tests.conftest import fixture_json

METHODS = ("multiplicative", "power", "shin")
odds_lists = st.lists(st.floats(1.01, 50.0), min_size=2, max_size=4)


@pytest.mark.parametrize("method", METHODS)
@given(odds=odds_lists)
def test_devig_sums_to_one(method: str, odds: list[float]) -> None:
    p = devig(odds, method)  # type: ignore[arg-type]
    assert sum(p) == pytest.approx(1.0, abs=1e-9)
    assert all(0 < x < 1 for x in p)


@pytest.mark.parametrize("method", METHODS)
def test_devig_preserves_favourite_order(method: str) -> None:
    p = devig([1.5, 2.8], method)  # type: ignore[arg-type]
    assert p[0] > p[1]


def test_multiplicative_worked_example() -> None:
    p = multiplicative([1.8, 2.1])
    q = (1 / 1.8, 1 / 2.1)
    assert p[0] == pytest.approx(q[0] / sum(q))


def test_power_and_shin_shade_longshots_more_than_multiplicative() -> None:
    odds = [1.25, 4.5]
    m, pw, sh = multiplicative(odds), power(odds), shin(odds)
    assert pw[1] < m[1]
    assert sh[1] < m[1]


def test_extreme_favourite() -> None:
    for method in METHODS:
        p = devig([1.01, 30.0], method)  # type: ignore[arg-type]
        assert p[0] > 0.95
        assert sum(p) == pytest.approx(1.0)


def test_three_way_price() -> None:
    for method in METHODS:
        p = devig([2.2, 2.9, 4.0], method)  # type: ignore[arg-type]
        assert len(p) == 3
        assert sum(p) == pytest.approx(1.0)


def test_book_without_margin_is_unchanged() -> None:
    for method in METHODS:
        assert devig([2.0, 2.0], method) == pytest.approx((0.5, 0.5))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [[1.0, 2.0], [0.5, 3.0], [2.0], [float("nan"), 2.0]])
def test_invalid_odds_rejected(bad: list[float]) -> None:
    with pytest.raises(ValueError):
        implied(bad)


def _q(ts: int, book: str, home: float, away: float, event: str = "e1") -> OddsQuote:
    return OddsQuote(ts, "basketball_nba", event, 10**12, "H", "A", book, home, away)


def test_consensus_weights_anchor(cfg: Config) -> None:
    fv = FairValue(cfg)
    fv.observe(_q(1, "pinnacle", 1.8, 2.1))
    fv.observe(_q(2, "otherbook", 2.0, 1.9))
    c = fv.consensus("e1", 2)
    assert c is not None
    pin = multiplicative([1.8, 2.1])[0]
    other = multiplicative([2.0, 1.9])[0]
    assert c.home_prob == pytest.approx((5 * pin + other) / 6)
    assert c.anchored
    assert c.bookmakers == 2
    yes = fv.yes_prob("e1", yes_is_home=False, t=2)
    assert yes == pytest.approx(1 - c.home_prob)


def test_missing_bookmaker_and_stale_odds(cfg: Config) -> None:
    fv = FairValue(cfg)
    assert fv.consensus("e1", 0) is None
    fv.observe(_q(0, "otherbook", 1.9, 1.9))
    c = fv.consensus("e1", 0)
    assert c is not None and not c.anchored
    age = int(cfg.fairvalue.max_odds_age_minutes * 60_000)
    assert fv.consensus("e1", age) is not None
    assert fv.consensus("e1", age + 1) is None


def test_lookahead_is_refused(cfg: Config) -> None:
    fv = FairValue(cfg)
    fv.observe(_q(100, "pinnacle", 1.8, 2.1))
    with pytest.raises(LookaheadError):
        fv.consensus("e1", 99)
    with pytest.raises(LookaheadError):
        fv.observe(_q(50, "pinnacle", 1.1, 9.0))


def test_alias_table_loads_and_normalises(cfg: Config) -> None:
    aliases = Aliases.load(cfg.matching.aliases_file)
    assert aliases.canonical("nhl", "Montreal Canadiens") == norm("Montréal Canadiens")
    assert aliases.canonical("nba", "Lakers") == "losangeleslakers"
    assert aliases.canonical("nhl", "Kings") == "losangeleskings"
    assert aliases.canonical("nba", "Kings") == "sacramentokings"
    assert aliases.canonical("nba", "Unknown FC") == "unknownfc"


def test_conflicting_alias_rejected() -> None:
    with pytest.raises(ValueError, match="two"):
        Aliases({"nba": {"A Team": ["X"], "B Team": ["X"]}})


ALIASES = Aliases({"nba": {"Synthetic Owls": ["Owls"], "Synthetic Hawks": ["Hawks"]}})
HOUR = 3_600_000


def _market(start: int = 100 * HOUR, yes: str = "Hawks", home: str | None = "Owls") -> MarketTeams:
    away = "Hawks" if home == "Owls" else "Owls" if home else None
    no = "Owls" if yes == "Hawks" else "Hawks"
    return MarketTeams("c1", "nba", home, away, yes, no, start)


def _event(commence: int = 100 * HOUR, eid: str = "ev1") -> OddsEventRef:
    return OddsEventRef(eid, "basketball_nba", commence, "Synthetic Owls", "Synthetic Hawks")


def test_full_match_is_confirmed(cfg: Config) -> None:
    m = Matcher(cfg, ALIASES).match(_market(), [_event()])
    assert m.confirmed
    assert m.yes_is_home is False
    assert m.method == "teams"


@pytest.mark.parametrize(
    ("market", "events", "method"),
    [
        (_market(home="Hawks"), [_event()], "teams_swapped"),
        (_market(home=None), [_event()], "outcomes_only"),
        (_market(start=103 * HOUR), [_event()], "teams_time_loose"),
        (_market(), [_event(eid="a"), _event(eid="b")], "ambiguous"),
    ],
)
def test_partial_matches_go_to_review(
    cfg: Config, market: MarketTeams, events: list[OddsEventRef], method: str
) -> None:
    m = Matcher(cfg, ALIASES).match(market, events)
    assert m.method == method
    assert not m.confirmed


def test_no_match_outside_loose_window_or_other_sport(cfg: Config) -> None:
    matcher = Matcher(cfg, ALIASES)
    assert matcher.match(_market(start=200 * HOUR), [_event()]).event is None
    nhl = OddsEventRef("x", "icehockey_nhl", 100 * HOUR, "Synthetic Owls", "Synthetic Hawks")
    assert matcher.match(_market(), [nhl]).method == "no_odds_event"


def _seed(store: Store, cid: str, **over: object) -> None:
    raw = fixture_json("gamma_markets_synthetic.json")[0] | {"conditionId": cid} | over
    store.upsert_market(market_row(GammaMarket.model_validate(raw), "nba"), 1)


def test_run_matching_writes_review_and_reads_confirmations(
    cfg: Config, store: Store, tmp_path: Path
) -> None:
    review = tmp_path / "review.csv"
    _seed(store, "c1")
    start = store.market("c1")["game_start_utc"]  # type: ignore[index]
    owls = "Owls"
    events = [OddsEventRef("ev1", "basketball_nba", start + 2 * HOUR, owls, "Hawks")]
    results = run_matching(cfg, store, events, 5, review)
    assert [r.method for r in results] == ["teams_time_loose"]
    assert store.mappings(confirmed_only=True) == []
    with review.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["odds_event_id"] == "ev1"
    rows[0]["confirmed"] = "yes"
    with review.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert run_matching(cfg, store, events, 6, review) == []
    confirmed = store.mappings(confirmed_only=True)
    assert [(r["odds_event_id"], r["match_method"], r["yes_is_home"]) for r in confirmed] == [
        ("ev1", "manual", False)
    ]

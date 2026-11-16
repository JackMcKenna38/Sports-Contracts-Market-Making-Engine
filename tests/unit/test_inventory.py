from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mm.inventory.portfolio import InsufficientCash, Portfolio, PositionError

CID = "0xcond_synthetic_a"


def test_buy_yes_books_edge_to_fair_as_spread() -> None:
    pf = Portfolio(1000)
    assert pf.fill(CID, "BUY", 0.48, 100, fair=0.50) == 0
    pos = pf.position(CID)
    assert (pos.yes, pos.no) == (100, 0)
    assert pf.cash == pytest.approx(952)
    assert pos.spread == pytest.approx(2.0)
    assert pf.equity() == pytest.approx(1002)


def test_sell_side_buys_no_at_complement() -> None:
    pf = Portfolio(1000)
    pf.fill(CID, "SELL", 0.52, 100, fair=0.50)
    pos = pf.position(CID)
    assert (pos.yes, pos.no) == (0, 100)
    assert pf.cash == pytest.approx(952)
    assert pos.spread == pytest.approx(2.0)
    assert pos.net == -100


def test_matched_pair_merges_to_cash() -> None:
    pf = Portfolio(1000)
    pf.fill(CID, "BUY", 0.48, 100, fair=0.50)
    merged = pf.fill(CID, "SELL", 0.52, 60, fair=0.50)
    pos = pf.position(CID)
    assert merged == 60
    assert (pos.yes, pos.no, pos.merged) == (40, 0, 60)
    # 1000 - 48 - 28.8 + 60
    assert pf.cash == pytest.approx(983.2)
    assert pos.spread == pytest.approx(2.0 + 1.2)
    assert pf.equity() == pytest.approx(1003.2)


def test_mark_move_is_inventory_pnl() -> None:
    pf = Portfolio(1000)
    pf.fill(CID, "BUY", 0.50, 100, fair=0.50)
    assert pf.mark(CID, 0.47) == pytest.approx(-3.0)
    pf.fill("0xother", "SELL", 0.60, 50, fair=0.60)
    assert pf.mark("0xother", 0.55) == pytest.approx(2.5)
    assert pf.components()["inventory"] == pytest.approx(-0.5)
    assert pf.equity() == pytest.approx(999.5)


@pytest.mark.parametrize(("yes_won", "expected"), [(True, 1052.0), (False, 952.0)])
def test_settlement_pays_one_per_winning_token(yes_won: bool, expected: float) -> None:
    pf = Portfolio(1000)
    pf.fill(CID, "BUY", 0.48, 100, fair=0.50)
    payout = pf.settle(CID, yes_won)
    pos = pf.position(CID)
    assert payout == (100 if yes_won else 0)
    assert pos.flat and pos.settled
    assert pf.cash == pytest.approx(expected)
    assert pf.equity() == pytest.approx(expected)
    assert pf.settle(CID, yes_won) == 0
    with pytest.raises(PositionError):
        pf.fill(CID, "BUY", 0.5, 1)


@pytest.mark.parametrize("yes_won", [True, False])
def test_merged_and_unmerged_paths_have_equal_value_at_settlement(yes_won: bool) -> None:
    """Holding 100 YES + 100 NO to settlement pays the same as merging them first."""
    merged = Portfolio(1000)
    merged.fill(CID, "BUY", 0.48, 100, fair=0.50)
    merged.fill(CID, "SELL", 0.53, 100, fair=0.50)
    merged.settle(CID, yes_won)

    yes_leg, no_leg = Portfolio(1000), Portfolio(1000)
    yes_leg.fill(CID, "BUY", 0.48, 100, fair=0.50)
    no_leg.fill(CID, "SELL", 0.53, 100, fair=0.50)
    yes_leg.settle(CID, yes_won)
    no_leg.settle(CID, yes_won)
    unmerged = (yes_leg.cash - 1000) + (no_leg.cash - 1000)

    assert merged.cash - 1000 == pytest.approx(unmerged)
    assert merged.cash == pytest.approx(1005)


def test_rebate_and_fee_are_separate_components() -> None:
    pf = Portfolio(1000)
    pf.fill(CID, "BUY", 0.50, 100, fair=0.50, fee=1.25, rebate=0.1875)
    comp = pf.components()
    assert (comp["fees"], comp["rebates"]) == (1.25, 0.1875)
    assert pf.equity() == pytest.approx(1000 - 1.25 + 0.1875)
    assert pf.pnl() == pytest.approx(pf.equity() - 1000)


def test_fill_without_fair_marks_at_the_fill_price() -> None:
    pf = Portfolio(1000)
    pf.fill(CID, "BUY", 0.40, 10)
    assert pf.position(CID).spread == 0
    assert pf.equity() == pytest.approx(1000)


def test_cannot_spend_more_than_cash() -> None:
    pf = Portfolio(10)
    with pytest.raises(InsufficientCash):
        pf.fill(CID, "BUY", 0.50, 100)
    assert pf.cash == 10 and pf.position(CID).flat


@pytest.mark.parametrize(("price", "size"), [(0.0, 1), (1.0, 1), (0.5, 0), (0.5, -1)])
def test_invalid_fills_are_rejected(price: float, size: float) -> None:
    with pytest.raises(PositionError):
        Portfolio(100).fill(CID, "BUY", price, size)


_price = st.integers(min_value=1, max_value=99).map(lambda c: c / 100)
_op = st.one_of(
    st.tuples(
        st.just("fill"),
        st.sampled_from(["a", "b", "c"]),
        st.sampled_from(["BUY", "SELL"]),
        _price,
        st.integers(min_value=1, max_value=500),
        st.one_of(st.none(), _price),
    ),
    st.tuples(st.just("mark"), st.sampled_from(["a", "b", "c"]), _price),
    st.tuples(st.just("settle"), st.sampled_from(["a", "b", "c"]), st.booleans()),
)


@given(st.lists(_op, max_size=60))
def test_cash_plus_marked_inventory_equals_capital_plus_pnl(ops: list[tuple[Any, ...]]) -> None:
    pf = Portfolio(1_000_000)
    for op in ops:
        cid = op[1]
        if pf.position(cid).settled:
            continue
        if op[0] == "fill":
            _, _, side, price, size, fair = op
            rebate = 0.15 * 0.05 * size * price * (1 - price)
            pf.fill(cid, side, price, size, fair=fair, rebate=rebate)
        elif op[0] == "mark":
            pf.mark(cid, op[2])
        else:
            pf.settle(cid, op[2])
        comp = pf.components()
        pnl = comp["spread"] + comp["inventory"] + comp["rebates"] - comp["fees"]
        assert pf.equity() == pytest.approx(pf.starting_capital + pnl, abs=1e-6)
        assert pf.cash == pytest.approx(
            pf.starting_capital + sum(p.cash_flow for p in pf.positions.values()), abs=1e-6
        )
        for pos in pf.positions.values():
            assert pos.yes >= 0 and pos.no >= 0
            assert pos.yes == 0 or pos.no == 0

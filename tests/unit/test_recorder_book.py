from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from mm.recorder.book import LocalBook
from mm.types import BookDelta, BookSide, BookSnapshot


def _snap(
    ts: int, bids: list[tuple[float, float]], asks: list[tuple[float, float]]
) -> BookSnapshot:
    return BookSnapshot(ts, ts, "t", tuple(bids), tuple(asks), f"h{ts}", "ws")


def _delta(ts: int, side: BookSide, price: float, size: float) -> BookDelta:
    return BookDelta(ts, ts, "t", side, price, size, None)


def test_snapshot_then_deltas() -> None:
    book = LocalBook("t")
    assert not book.ready
    book.apply_snapshot(_snap(10, [(0.48, 100), (0.47, 50)], [(0.52, 80)]))
    assert book.apply_delta(_delta(11, "BUY", 0.49, 30))
    assert book.apply_delta(_delta(12, "SELL", 0.52, 0))
    assert book.levels("BUY") == ((0.49, 30), (0.48, 100), (0.47, 50))
    assert book.levels("SELL") == ()
    assert book.best_bid() == 0.49
    assert book.best_ask() is None
    assert book.mid() is None


def test_delta_removing_a_level() -> None:
    book = LocalBook("t")
    book.apply_snapshot(_snap(10, [(0.48, 100)], [(0.52, 80), (0.53, 10)]))
    book.apply_delta(_delta(11, "SELL", 0.52, 0))
    assert book.best_ask() == 0.53
    assert book.mid() == 0.505


def test_delta_before_snapshot_is_stale() -> None:
    book = LocalBook("t")
    assert not book.apply_delta(_delta(5, "BUY", 0.4, 1))
    book.apply_snapshot(_snap(10, [], []))
    assert not book.apply_delta(_delta(9, "BUY", 0.4, 1))
    assert book.stale == 2
    assert book.levels("BUY") == ()


def test_out_of_order_delta_is_applied_and_counted() -> None:
    book = LocalBook("t")
    book.apply_snapshot(_snap(10, [], []))
    book.apply_delta(_delta(20, "BUY", 0.40, 5))
    book.apply_delta(_delta(15, "BUY", 0.41, 7))
    assert book.out_of_order == 1
    assert book.ts == 20
    assert book.best_bid() == 0.41


def test_older_snapshot_is_ignored() -> None:
    book = LocalBook("t")
    book.apply_snapshot(_snap(10, [(0.5, 1)], []))
    assert not book.apply_snapshot(_snap(9, [(0.1, 1)], []))
    assert book.best_bid() == 0.5


def test_same_levels_ignores_order_and_zero_sizes() -> None:
    book = LocalBook("t")
    book.apply_snapshot(_snap(10, [(0.47, 5), (0.48, 1)], [(0.52, 3)]))
    other = _snap(11, [(0.48, 1), (0.47, 5), (0.46, 0)], [(0.52, 3)])
    assert book.same_levels(other)
    assert not book.same_levels(_snap(11, [(0.48, 2)], [(0.52, 3)]))


_SIDES: list[BookSide] = ["BUY", "SELL"]


@given(
    st.lists(
        st.tuples(
            st.sampled_from(_SIDES),
            st.integers(1, 99).map(lambda c: c / 100),
            st.sampled_from([0.0, 1.0, 5.0, 50.0]),
        ),
        max_size=40,
    )
)
def test_book_equals_last_size_per_level(changes: list[tuple[BookSide, float, float]]) -> None:
    book = LocalBook("t")
    book.apply_snapshot(_snap(0, [], []))
    expected: dict[tuple[BookSide, float], float] = {}
    for i, (side, price, size) in enumerate(changes, start=1):
        book.apply_delta(_delta(i, side, price, size))
        expected[(side, price)] = size
    for (side, price), size in expected.items():
        assert book.size_at(side, price) == size
    assert all(s > 0 for _, s in book.levels("BUY") + book.levels("SELL"))

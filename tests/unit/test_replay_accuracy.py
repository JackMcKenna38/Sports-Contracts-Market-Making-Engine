from __future__ import annotations

from typing import Any

from mm.analytics.replay_check import Mismatch, check_replay
from mm.recorder.writer import delta_row, snapshot_row
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import BookDelta, BookSnapshot, Levels

T0 = 1_794_870_000_000
BIDS: Levels = ((0.48, 100.0), (0.47, 250.0))
ASKS: Levels = ((0.52, 80.0),)


def _snap(ts: int, bids: Levels, asks: Levels, h: str, source: Any, token: str = "111") -> Any:
    return snapshot_row(BookSnapshot(ts, ts + 40, token, bids, asks, h, source))


def _delta(ts: int, side: Any, price: float, size: float, h: str) -> Any:
    return delta_row(BookDelta(ts, ts + 40, "111", side, price, size, h))


def _seed(store: Store, rest: list[Any]) -> None:
    store.insert(s.book_snapshots, [_snap(T0, BIDS, ASKS, "h0", "ws"), *rest])
    store.insert(
        s.book_deltas,
        [
            _delta(T0 + 1000, "BUY", 0.49, 40, "h1"),
            _delta(T0 + 2000, "BUY", 0.48, 0, "h2"),
            _delta(T0 + 3000, "SELL", 0.52, 30, "h3"),
        ],
    )


SKEW = 250
AFTER_BID = ((0.47, 250.0), (0.49, 40.0))


def test_rebuilt_books_match_rest_checkpoints(store: Store) -> None:
    _seed(
        store,
        [
            _snap(T0 + 500, BIDS, ASKS, "h0", "rest"),
            _snap(T0 + 2500, AFTER_BID, ASKS, "h2", "rest"),
            _snap(T0 + 3000, ((0.49, 40.0), (0.47, 250.0)), ((0.52, 30.0),), "other", "rest"),
        ],
    )
    result = check_replay(store, T0, T0 + 10_000, SKEW)
    assert (result.checkpoints, result.matched, result.matched_after_skew) == (3, 3, 0)
    assert result.ok and result.verified == 3 and result.no_stream_book == 0


def test_checkpoint_slightly_ahead_of_the_stream_matches_within_the_skew_window(
    store: Store,
) -> None:
    """Seen live: a REST book already holds a change the stream stamps milliseconds later."""
    _seed(store, [_snap(T0 + 2990, AFTER_BID, ((0.52, 30.0),), "h2", "rest")])
    result = check_replay(store, T0, T0 + 10_000, SKEW)
    assert (result.matched, result.matched_after_skew, result.mismatches) == (0, 1, [])
    assert not check_replay(store, T0, T0 + 10_000, 5).ok, "outside a 5 ms window it is a mismatch"


def test_a_lost_change_is_one_mismatch_and_the_book_recovers_from_the_checkpoint(
    store: Store,
) -> None:
    wrong = ((0.47, 250.0), (0.49, 45.0))
    _seed(
        store,
        [
            _snap(T0 + 2500, wrong, ASKS, "h2", "rest"),
            _snap(T0 + 3500, wrong, ((0.52, 30.0),), "h3", "rest"),
        ],
    )
    result = check_replay(store, T0, T0 + 10_000, SKEW)
    assert result.mismatches == [Mismatch("111", T0 + 2500, T0 + 2540, "h2")]
    assert (result.checkpoints, result.matched) == (2, 1)
    assert not result.ok
    logged = {"token_id": "111", "started_ts": T0 + 2540, "reason": "lost_update"}
    assert result.unlogged([logged]) == []
    assert result.unlogged([{**logged, "reason": "error"}]) == result.mismatches
    assert result.unlogged([{**logged, "started_ts": 1}]) == result.mismatches


def test_checkpoint_still_wrong_when_the_stream_ends_is_a_mismatch(store: Store) -> None:
    _seed(store, [_snap(T0 + 3100, BIDS, ASKS, "h0", "rest")])
    result = check_replay(store, T0, T0 + 10_000, SKEW)
    assert [m.ts for m in result.mismatches] == [T0 + 3100]


def test_checkpoint_for_a_token_with_no_stream_book_is_not_counted_as_verified(
    store: Store,
) -> None:
    _seed(store, [_snap(T0 + 1500, BIDS, ASKS, "h0", "rest", token="999")])
    result = check_replay(store, T0, T0 + 10_000, SKEW)
    assert (result.checkpoints, result.no_stream_book, result.verified) == (1, 1, 0)
    assert result.ok


def test_window_selects_checkpoints_by_receive_time_and_keeps_earlier_stream_data(
    store: Store,
) -> None:
    _seed(
        store,
        [
            _snap(T0 + 500, BIDS, ASKS, "h0", "rest"),
            _snap(T0 + 2500, AFTER_BID, ASKS, "h2", "rest"),
        ],
    )
    result = check_replay(store, T0 + 600, T0 + 10_000, SKEW)
    assert (result.checkpoints, result.matched) == (1, 1)

from __future__ import annotations

import pytest

from mm.config import Config
from mm.sim.fees import maker_rebate, taker_fee
from mm.sim.rewards import RewardEstimator, combine, market_score, order_score

DAY = 86_400_000


def test_fee_page_worked_example() -> None:
    assert taker_fee(100, 0.50, 0.05) == pytest.approx(1.25)


def test_fee_is_symmetric_and_zero_at_bounds() -> None:
    assert taker_fee(10, 0.2, 0.05) == pytest.approx(taker_fee(10, 0.8, 0.05))
    assert taker_fee(10, 0.0, 0.05) == 0
    assert taker_fee(10, 1.0, 0.05) == 0


def test_rebate_is_share_of_taker_fee() -> None:
    assert maker_rebate(100, 0.5, 0.05, 1, 0.15) == pytest.approx(0.1875)


def test_bad_fill_rejected() -> None:
    with pytest.raises(ValueError):
        taker_fee(-1, 0.5, 0.05)
    with pytest.raises(ValueError):
        taker_fee(1, 1.5, 0.05)


def test_order_score_formula() -> None:
    assert order_score(0.045, 0.0) == 1.0
    assert order_score(0.045, 0.015) == pytest.approx((0.03 / 0.045) ** 2)
    assert order_score(0.045, 0.015, b=2.0) == pytest.approx(2 * (0.03 / 0.045) ** 2)
    assert order_score(0.045, 0.05) == 0.0


def test_single_sided_is_scaled_inside_band_and_zero_outside(cfg: Config) -> None:
    r = cfg.rewards
    assert combine(30.0, 0.0, 0.5, r) == pytest.approx(10.0)
    assert combine(30.0, 0.0, 0.95, r) == 0.0
    assert combine(30.0, 20.0, 0.95, r) == 20.0
    assert combine(30.0, 24.0, 0.5, r) == 24.0


def test_min_size_excludes_small_orders(cfg: Config) -> None:
    score = market_score([(0.49, 40.0)], [(0.51, 60.0)], 0.5, 0.045, 50, cfg.rewards)
    single = 60.0 * order_score(0.045, 0.01) / cfg.rewards.single_sided_scale
    assert score == pytest.approx(single)


def test_estimator_pays_share_of_daily_rate(cfg: Config) -> None:
    est = RewardEstimator(cfg.rewards)
    n = int(est.samples_per_day)
    for i in range(n):
        share = est.sample(
            "c", i * 60_000, [(0.49, 100)], [(0.51, 100)], [(0.49, 100)], [(0.51, 100)],
            0.5, 0.045, 50, 10.0,
        )  # fmt: skip
        assert share == pytest.approx(0.5)
    assert est.payouts() == {("c", 0): pytest.approx(5.0)}
    assert est.total() == pytest.approx(5.0)


def test_estimator_drops_days_under_minimum_payout(cfg: Config) -> None:
    est = RewardEstimator(cfg.rewards)
    est.sample("c", DAY, [(0.49, 100)], [(0.51, 100)], [], [], 0.5, 0.045, 50, 10.0)
    assert est.payouts()[("c", 1)] < cfg.rewards.min_daily_payout
    assert est.total() == 0.0


def test_estimator_ignores_markets_without_rewards(cfg: Config) -> None:
    est = RewardEstimator(cfg.rewards)
    assert est.sample("c", 0, [(0.49, 100)], [(0.51, 100)], [], [], 0.5, 0.0, 50, 10.0) == 0
    assert est.sample("c", 0, [(0.49, 100)], [(0.51, 100)], [], [], 0.5, 0.045, 50, 0.0) == 0
    assert est.sample("c", 0, [(0.49, 100)], [(0.51, 100)], [], [], None, 0.045, 50, 1.0) == 0

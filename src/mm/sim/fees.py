from __future__ import annotations


def taker_fee(shares: float, price: float, rate: float, exponent: float = 1.0) -> float:
    """``C * rate * (p * (1 - p)) ** exponent``; with exponent 1 this is the published formula."""
    if shares < 0 or not 0 <= price <= 1:
        raise ValueError(f"bad fill: {shares} shares at {price}")
    return shares * rate * float((price * (1.0 - price)) ** exponent)


def maker_rebate(shares: float, price: float, rate: float, exponent: float, rebate: float) -> float:
    """Estimated rebate: ``rebate`` times the taker fee paid by the counterparty of this fill."""
    return rebate * taker_fee(shares, price, rate, exponent)

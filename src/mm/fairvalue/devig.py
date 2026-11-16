"""Bookmaker margin removal for n-way decimal odds."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Literal

Method = Literal["multiplicative", "power", "shin"]

_TOL = 1e-12
_ITER = 200


def implied(odds: Sequence[float]) -> tuple[float, ...]:
    if len(odds) < 2:
        raise ValueError("need at least two outcomes")
    for o in odds:
        if not math.isfinite(o) or o <= 1.0:
            raise ValueError(f"decimal odds must be finite and above 1.0, got {o}")
    return tuple(1.0 / o for o in odds)


def multiplicative(odds: Sequence[float]) -> tuple[float, ...]:
    q = implied(odds)
    total = sum(q)
    return tuple(x / total for x in q)


def _bisect(f: Callable[[float], float], lo: float, hi: float) -> float:
    flo = f(lo)
    for _ in range(_ITER):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < _TOL:
            return mid
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2


def power(odds: Sequence[float]) -> tuple[float, ...]:
    """p_i = q_i ** k with k chosen so the probabilities sum to one."""
    q = implied(odds)
    if abs(sum(q) - 1.0) < _TOL:
        return q
    k = _bisect(lambda k: sum(x**k for x in q) - 1.0, 1e-6, 100.0)
    p = tuple(x**k for x in q)
    total = sum(p)
    return tuple(x / total for x in p)


def shin(odds: Sequence[float]) -> tuple[float, ...]:
    """Shin (1993) insider-trading model. Books without an overround fall back to multiplicative."""
    q = implied(odds)
    booksum = sum(q)
    if booksum <= 1.0 + _TOL:
        return multiplicative(odds)

    def probs(z: float) -> tuple[float, ...]:
        return tuple(
            (math.sqrt(z * z + 4 * (1 - z) * x * x / booksum) - z) / (2 * (1 - z)) for x in q
        )

    z = _bisect(lambda z: sum(probs(z)) - 1.0, 0.0, 0.999999)
    p = probs(z)
    total = sum(p)
    return tuple(x / total for x in p)


METHODS: dict[str, Callable[[Sequence[float]], tuple[float, ...]]] = {
    "multiplicative": multiplicative,
    "power": power,
    "shin": shin,
}


def devig(odds: Sequence[float], method: Method) -> tuple[float, ...]:
    return METHODS[method](odds)

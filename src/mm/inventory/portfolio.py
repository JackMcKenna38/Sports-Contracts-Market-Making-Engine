"""Positions, merges and settlement, priced in YES-token space.

Buying YES at ``p`` costs ``p`` per share; the other side of the market is buying NO at
``1 - p``. One YES plus one NO merges into 1 unit of cash, so holdings are never negative.

Every change in value is assigned to exactly one component, which keeps the identity
``cash + marked inventory == starting capital + spread + inventory + rebates - fees``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mm.types import BookSide


class InsufficientCash(RuntimeError):
    pass


class PositionError(RuntimeError):
    pass


@dataclass(slots=True)
class Position:
    yes: float = 0.0
    no: float = 0.0
    mark: float | None = None
    cash_flow: float = 0.0
    spread: float = 0.0
    inventory: float = 0.0
    rebates: float = 0.0
    fees: float = 0.0
    merged: float = 0.0
    volume: float = 0.0
    settled: bool = False

    @property
    def net(self) -> float:
        return self.yes - self.no

    @property
    def flat(self) -> bool:
        return self.yes == 0.0 and self.no == 0.0

    def value(self) -> float:
        if self.flat:
            return 0.0
        if self.mark is None:
            raise PositionError("a non-flat position has no mark")
        return self.yes * self.mark + self.no * (1.0 - self.mark)

    def pnl(self) -> float:
        return self.spread + self.inventory + self.rebates - self.fees

    def components(self) -> dict[str, float]:
        return {
            "spread": self.spread,
            "inventory": self.inventory,
            "rebates": self.rebates,
            "fees": self.fees,
            "merged": self.merged,
            "volume": self.volume,
        }


class Portfolio:
    def __init__(self, starting_capital: float) -> None:
        self.starting_capital = starting_capital
        self.cash = starting_capital
        self.positions: dict[str, Position] = {}

    def position(self, condition_id: str) -> Position:
        return self.positions.setdefault(condition_id, Position())

    def mark(self, condition_id: str, price: float) -> float:
        """Re-mark a market in YES space; returns the inventory PnL booked by the move."""
        pos = self.position(condition_id)
        if pos.settled:
            return 0.0
        move = 0.0 if pos.mark is None else pos.net * (price - pos.mark)
        pos.mark = price
        pos.inventory += move
        return move

    def fill(
        self,
        condition_id: str,
        side: BookSide,
        yes_price: float,
        size: float,
        *,
        fair: float | None = None,
        fee: float = 0.0,
        rebate: float = 0.0,
    ) -> float:
        """Apply a fill and merge any matched pair. Returns the merged quantity.

        ``side`` is in YES space: BUY takes YES at ``yes_price``; SELL takes NO at
        ``1 - yes_price``. The edge to the mark at fill time is booked as spread capture.
        """
        if size <= 0 or not 0.0 < yes_price < 1.0:
            raise PositionError(f"invalid fill: size={size} yes_price={yes_price}")
        pos = self.position(condition_id)
        if pos.settled:
            raise PositionError(f"{condition_id} is settled")
        unit_cost = yes_price if side == "BUY" else 1.0 - yes_price
        cost = unit_cost * size + fee - rebate
        if cost > self.cash + 1e-9:
            raise InsufficientCash(f"fill costs {cost:.6f}, cash is {self.cash:.6f}")
        if fair is not None:
            self.mark(condition_id, fair)
        elif pos.mark is None:
            pos.mark = yes_price
        assert pos.mark is not None
        if side == "BUY":
            pos.yes += size
            pos.spread += (pos.mark - yes_price) * size
        else:
            pos.no += size
            pos.spread += (yes_price - pos.mark) * size
        pos.fees += fee
        pos.rebates += rebate
        pos.volume += size
        self._cash(pos, -cost)
        merged = min(pos.yes, pos.no)
        if merged > 0:
            pos.yes -= merged
            pos.no -= merged
            pos.merged += merged
            self._cash(pos, merged)
        return merged

    def settle(self, condition_id: str, yes_won: bool) -> float:
        """Pay 1 per winning token, 0 per losing token. Returns the cash paid out."""
        pos = self.position(condition_id)
        if pos.settled:
            return 0.0
        self.mark(condition_id, 1.0 if yes_won else 0.0)
        payout = pos.yes if yes_won else pos.no
        pos.yes = pos.no = 0.0
        pos.settled = True
        self._cash(pos, payout)
        return payout

    def _cash(self, pos: Position, amount: float) -> None:
        self.cash += amount
        pos.cash_flow += amount

    def inventory_value(self) -> float:
        return sum(p.value() for p in self.positions.values())

    def equity(self) -> float:
        return self.cash + self.inventory_value()

    def pnl(self) -> float:
        return sum(p.pnl() for p in self.positions.values())

    def components(self) -> dict[str, float]:
        totals = dict.fromkeys(Position().components(), 0.0)
        for pos in self.positions.values():
            for key, value in pos.components().items():
                totals[key] += value
        return totals

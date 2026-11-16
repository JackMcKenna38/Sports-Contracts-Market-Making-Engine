"""Per-market trade backfill from the Data API v2 after a game resolves.

`GET /v2/trades?condition=<id>` serves a `{data, pagination}` envelope; pages are followed
through `pagination.next_cursor` until `has_more` is false. Wallet and profile fields in the
rows are never read or stored.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mm.config import Config
from mm.net import Http, PayloadError, validate
from mm.storage.store import Row, Store
from mm.types import now_ms

log = logging.getLogger(__name__)


class BackfillError(RuntimeError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class DataTrade(_Model):
    side: Literal["BUY", "SELL"]
    token_id: str
    condition_id: str
    size: float = Field(gt=0)
    price: float = Field(ge=0, le=1)
    timestamp: int = Field(gt=1_000_000_000, lt=100_000_000_000)
    transaction_hash: str

    def ts_ms(self) -> int:
        return self.timestamp * 1000


class Pagination(_Model):
    has_more: bool
    next_cursor: str | None = None


class TradesPage(_Model):
    data: list[DataTrade]
    pagination: Pagination


class TradeBackfill:
    def __init__(self, cfg: Config, http: Http, store: Store) -> None:
        self._cfg = cfg
        self._http = http
        self._store = store
        self._url = cfg.endpoints.data_api.rstrip("/") + "/trades"
        self._attempted: set[str] = set()

    async def fetch(self, condition_id: str) -> list[Row]:
        """Every taker-side fill on a market, oldest first."""
        rec = self._cfg.recorder
        params: dict[str, object] = {"condition": condition_id, "limit": rec.trade_backfill_limit}
        trades: list[DataTrade] = []
        for _ in range(rec.trade_backfill_max_pages):
            payload = await self._http.get(self._url, params)
            page = validate(TradesPage, payload, "data_api_trades", self._cfg.paths.data_dir)
            trades.extend(page.data)
            if not page.pagination.has_more:
                break
            if not page.pagination.next_cursor:
                raise PayloadError("Data API /v2/trades reported has_more without a next_cursor")
            params = {"condition": condition_id, "cursor": page.pagination.next_cursor}
        else:
            raise BackfillError(
                f"{condition_id}: more than {rec.trade_backfill_max_pages} pages of trades; "
                "raise recorder.trade_backfill_max_pages rather than store a partial history"
            )
        foreign = {t.condition_id for t in trades} - {condition_id}
        if foreign:
            raise PayloadError(f"Data API /v2/trades returned other markets: {sorted(foreign)}")
        recv = now_ms()
        rows: list[Row] = [
            {
                "ts": t.ts_ms(),
                "ts_recv": recv,
                "condition_id": t.condition_id,
                "token_id": t.token_id,
                "side": t.side,
                "price": t.price,
                "size": t.size,
                "tx_hash": t.transaction_hash,
                "source": "data_api",
            }
            for t in trades
        ]
        rows.sort(key=lambda r: r["ts"])
        return rows

    async def run_once(self) -> int:
        done = 0
        pending = await asyncio.to_thread(self._store.conditions_needing_backfill)
        for cid in pending:
            if cid in self._attempted:
                continue
            self._attempted.add(cid)
            try:
                rows = await self.fetch(cid)
            except (BackfillError, PayloadError) as exc:
                log.error("trade backfill for %s failed: %s", cid, exc)
                continue
            await asyncio.to_thread(self._store.replace_backfilled_trades, cid, rows)
            log.info("backfilled %d trades for %s", len(rows), cid)
            done += 1
        return done

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from mm.config import Config
from mm.net import Http, validate_list
from mm.recorder.market_ws import MarketFeed
from mm.recorder.messages import RestBook
from mm.recorder.writer import Writer
from mm.types import BookSnapshot, now_ms

log = logging.getLogger(__name__)


class RestBooks:
    """Periodic ``POST /books`` checkpoints, also used to re-seed books after a reconnect."""

    def __init__(self, cfg: Config, http: Http, writer: Writer, feed: MarketFeed) -> None:
        self._cfg = cfg
        self._url = cfg.endpoints.clob.rstrip("/") + "/books"
        self._http = http
        self._writer = writer
        self._feed = feed

    async def fetch(self, tokens: Sequence[str]) -> list[BookSnapshot]:
        out: list[BookSnapshot] = []
        size = self._cfg.recorder.books_batch_size
        for i in range(0, len(tokens), size):
            chunk = tokens[i : i + size]
            payload = await self._http.post(self._url, [{"token_id": t} for t in chunk])
            recv = now_ms()
            books = validate_list(RestBook, payload, "clob_books", self._cfg.paths.data_dir)
            out.extend(b.to_event(recv) for b in books)
        return out

    async def snapshot(self, tokens: Sequence[str]) -> int:
        if not tokens:
            return 0
        snaps = await self.fetch(list(tokens))
        for snap in snaps:
            self._writer.add_snapshot(snap)
            self._feed.reconcile(snap)
        missing = set(tokens) - {s.token_id for s in snaps}
        if missing:
            log.warning("POST /books returned no book for %d tokens", len(missing))
        return len(snaps)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.recorder.rest_snapshot_seconds)
            try:
                await self.snapshot(self._feed.tokens)
            except Exception:
                log.exception("REST checkpoint failed")

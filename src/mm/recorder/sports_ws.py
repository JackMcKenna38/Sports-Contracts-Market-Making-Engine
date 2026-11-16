from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from pydantic import ValidationError

from mm.config import Config
from mm.recorder.market_ws import Listener, quarantine_row
from mm.recorder.messages import GameMsg
from mm.recorder.writer import Writer
from mm.recorder.ws import Connector, GapLog, default_connector, recv_text, run_with_reconnect
from mm.storage import schema as s
from mm.storage.store import Store
from mm.types import now_ms

log = logging.getLogger(__name__)


class SportsFeed:
    """Sports WebSocket recorder. Answers the server's ``ping`` with ``pong``."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        writer: Writer,
        *,
        connector: Connector = default_connector,
        listeners: Iterable[Listener] = (),
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._writer = writer
        self._connector = connector
        self.listeners = list(listeners)
        self._leagues = {lg.lower() for lg in cfg.leagues}
        self.quarantined = 0

    def handle_text(self, text: str, recv: int) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            self._quarantine(recv, f"invalid JSON: {exc}", text)
            return
        for item in payload if isinstance(payload, list) else [payload]:
            league = item.get("leagueAbbreviation") if isinstance(item, dict) else None
            if isinstance(league, str) and league.lower() not in self._leagues:
                continue
            try:
                game = GameMsg.model_validate(item).to_event(recv)
            except ValidationError as exc:
                self._quarantine(recv, str(exc), json.dumps(item, default=str))
                continue
            if game.league not in self._leagues:
                continue
            self._writer.add_game(game)
            for fn in self.listeners:
                fn(game)

    def _quarantine(self, recv: int, error: str, payload: str) -> None:
        self.quarantined += 1
        self._writer.add(s.quarantine, quarantine_row(recv, "sports_ws", error, payload))
        log.error("sports_ws message quarantined: %s", error[:300])

    async def _session(self, gaps: GapLog) -> None:
        rec = self._cfg.recorder
        async with self._connector(self._cfg.endpoints.sports_ws) as ws:
            await gaps.close()
            while True:
                text = await recv_text(ws, rec.sports_ws_idle_timeout_seconds)
                if text.strip().lower() == "ping":
                    await ws.send("pong")
                    continue
                self.handle_text(text, now_ms())

    async def run(self) -> None:
        gaps = GapLog(self._store, "sports_ws", self.listeners)
        rec = self._cfg.recorder
        await run_with_reconnect(
            "sports_ws",
            lambda: self._session(gaps),
            gaps,
            rec.reconnect_base_seconds,
            rec.reconnect_max_seconds,
        )

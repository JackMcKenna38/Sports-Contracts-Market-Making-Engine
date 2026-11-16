from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from websockets.asyncio.client import connect as ws_connect

from mm.net import backoff_delay
from mm.storage.store import Store
from mm.types import Event, Gap, now_ms

log = logging.getLogger(__name__)


class WsConn(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


Connector = Callable[[str], AbstractAsyncContextManager[WsConn]]


def default_connector(url: str) -> AbstractAsyncContextManager[WsConn]:
    return ws_connect(url, ping_interval=None, max_size=None)


class IdleTimeout(TimeoutError):
    pass


async def recv_text(conn: WsConn, timeout: float) -> str:
    try:
        raw = await asyncio.wait_for(conn.recv(), timeout)
    except TimeoutError as exc:
        raise IdleTimeout(f"no message for {timeout:g}s") from exc
    return raw.decode() if isinstance(raw, bytes) else raw


class GapLog:
    """Writes one row per outage to ``recorder_gaps``: opened on loss, closed on recovery."""

    def __init__(
        self, store: Store, stream: str, listeners: Iterable[Callable[[Event], None]] = ()
    ) -> None:
        self._store = store
        self._stream = stream
        self._listeners = list(listeners)
        self._open: int | None = None

    def _emit(self, ts: int, is_open: bool, reason: str) -> None:
        for fn in self._listeners:
            fn(Gap(ts, self._stream, None, is_open, reason))

    @property
    def is_open(self) -> bool:
        return self._open is not None

    async def open(self, reason: str, detail: str = "", token_id: str | None = None) -> None:
        if self._open is None:
            ts = now_ms()
            self._open = await asyncio.to_thread(
                self._store.open_gap, self._stream, token_id, ts, reason, detail[:2000]
            )
            self._emit(ts, True, reason)
            log.warning("%s gap opened: %s %s", self._stream, reason, detail[:200])

    async def close(self) -> None:
        if self._open is not None:
            gap, self._open = self._open, None
            ts = now_ms()
            await asyncio.to_thread(self._store.close_gap, gap, ts)
            self._emit(ts, False, "")
            log.info("%s gap closed", self._stream)


async def run_with_reconnect(
    name: str,
    session: Callable[[], Awaitable[None]],
    gaps: GapLog,
    base: float,
    cap: float,
    max_attempts: int | None = None,
) -> None:
    """Run ``session`` forever; each failure opens a gap and waits with jittered backoff.

    ``session`` must close the gap itself once its data can be trusted again.
    """
    attempt = 0
    sessions = 0
    loop = asyncio.get_running_loop()
    while max_attempts is None or sessions < max_attempts:
        sessions += 1
        started = loop.time()
        try:
            await session()
            reason, detail = "closed", "server closed the connection"
        except asyncio.CancelledError:
            raise
        except IdleTimeout as exc:
            reason, detail = "idle_timeout", str(exc)
        except Exception as exc:
            reason, detail = "error", f"{type(exc).__name__}: {exc}"
        await gaps.open(reason, detail)
        if loop.time() - started > cap:
            attempt = 0
        delay = backoff_delay(min(attempt, 16), base, cap)
        log.warning("%s disconnected (%s); reconnecting in %.1fs", name, detail, delay)
        attempt += 1
        await asyncio.sleep(delay)


async def cancel(task: asyncio.Task[None] | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

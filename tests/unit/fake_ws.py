from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager


class Drop(Exception):
    pass


class FakeWs:
    def __init__(self, script: list[str | Exception]) -> None:
        self.incoming: asyncio.Queue[str | Exception] = asyncio.Queue()
        for item in script:
            self.incoming.put_nowait(item)
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item


class FakeServer:
    """Hands out one scripted connection per connect; later connects block forever."""

    def __init__(self, scripts: list[list[str | Exception]]) -> None:
        self.conns = [FakeWs(s) for s in scripts]
        self.urls: list[str] = []
        self.connected = asyncio.Event()

    def connect(self, url: str) -> AbstractAsyncContextManager[FakeWs]:
        @asynccontextmanager
        async def cm() -> AsyncIterator[FakeWs]:
            index = len(self.urls)
            self.urls.append(url)
            if index >= len(self.conns):
                self.connected.set()
                await asyncio.Event().wait()
            yield self.conns[index]

        return cm()

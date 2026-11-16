from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from mm.config import Config

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class PayloadError(RuntimeError):
    """An API response did not match its model. The raw payload is saved to disk."""


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status = status


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def save_payload(data_dir: Path, name: str, payload: Any) -> Path:
    folder = data_dir / "failed_payloads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.time_ns()}_{name}.json"
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    path.write_text(text, encoding="utf-8")
    return path


def validate[T: BaseModel](model: type[T], payload: Any, name: str, data_dir: Path) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        path = save_payload(data_dir, name, payload)
        raise PayloadError(
            f"{name}: does not match {model.__name__}; saved to {path}\n{exc}"
        ) from exc


def validate_list[T: BaseModel](model: type[T], payload: Any, name: str, data_dir: Path) -> list[T]:
    try:
        return TypeAdapter(list[model]).validate_python(payload)  # type: ignore[valid-type]
    except ValidationError as exc:
        path = save_payload(data_dir, name, payload)
        raise PayloadError(
            f"{name}: does not match list[{model.__name__}]; saved to {path}\n{exc}"
        ) from exc


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    ceiling = min(cap, base * (2**attempt))
    return (rng or random).uniform(ceiling / 2, ceiling)


class Http:
    """Async JSON client: explicit User-Agent, per-host pacing, backoff on 429 and 5xx."""

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._cfg = cfg.http
        self._client = httpx.AsyncClient(
            headers={"User-Agent": cfg.http.user_agent, "Accept": "application/json"},
            timeout=cfg.http.timeout_seconds,
            transport=transport,
        )
        self._next_slot: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _pace(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._next_slot.get(host, 0.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_slot[host] = time.monotonic() + self._cfg.min_interval_seconds

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> tuple[Any, httpx.Headers]:
        host = urlsplit(url).netloc
        last: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            await self._pace(host)
            try:
                resp = await self._client.request(method, url, params=params, json=json_body)
            except httpx.TransportError as exc:
                last = exc
                log.warning("%s %s failed (%s); attempt %d", method, url, exc, attempt + 1)
            else:
                if resp.status_code < 400:
                    return resp.json(), resp.headers
                if resp.status_code not in RETRY_STATUS:
                    raise HttpError(resp.status_code, url, resp.text)
                last = HttpError(resp.status_code, url, resp.text)
                log.warning(
                    "%s %s returned %d; attempt %d", method, url, resp.status_code, attempt + 1
                )
                retry_after = resp.headers.get("retry-after", "")
                if retry_after.isdigit():
                    await asyncio.sleep(min(float(retry_after), self._cfg.backoff_max_seconds))
                    continue
            if attempt < self._cfg.max_retries:
                await asyncio.sleep(
                    backoff_delay(
                        attempt, self._cfg.backoff_base_seconds, self._cfg.backoff_max_seconds
                    )
                )
        assert last is not None
        raise last

    async def get(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        data, _ = await self.request("GET", url, params=params)
        return data

    async def post(self, url: str, body: Any) -> Any:
        data, _ = await self.request("POST", url, json_body=body)
        return data

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

import httpx

from .base import AuthExpired

log = logging.getLogger(__name__)


class RateLimited(Exception):
    """The service asked us to wait longer than a sync run should block."""

    def __init__(self, service: str, retry_after: float):
        super().__init__(f"{service} rate limit: retry after {retry_after:.0f}s")
        self.retry_after = retry_after


@dataclass
class Pace:
    get_interval: float
    post_interval: float


class PacedClient:
    """httpx wrapper that spaces requests to stay under a service's per-second
    limits and backs off on 429 and 5xx responses."""

    MAX_ATTEMPTS = 5
    MAX_INLINE_WAIT = 120.0

    def __init__(self, service: str, base_url: str, pace: Pace, headers: dict[str, str], params: dict[str, str] | None = None):
        self.service = service
        self.pace = pace
        self.client = httpx.AsyncClient(base_url=base_url, headers=headers, params=params, timeout=30)
        self._next = {"GET": 0.0, "WRITE": 0.0}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _wait_turn(self, method: str) -> None:
        bucket = "GET" if method == "GET" else "WRITE"
        interval = self.pace.get_interval if bucket == "GET" else self.pace.post_interval
        async with self._lock:
            delay = self._next[bucket] - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next[bucket] = time.monotonic() + interval

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            await self._wait_turn(method)
            response = await self.client.request(method, url, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                wait = _retry_after(response) or min(60.0, 2**attempt) * (0.5 + random.random())
                log.warning(
                    "%s %s %s -> %s (attempt %d, X-Ratelimit=%s), waiting %.1fs",
                    self.service, method, url, response.status_code, attempt,
                    response.headers.get("X-Ratelimit"), wait,
                )
                if wait > self.MAX_INLINE_WAIT:
                    raise RateLimited(self.service, wait)
                if attempt < self.MAX_ATTEMPTS:
                    await asyncio.sleep(wait)
                    continue
            return response
        return response


class AuthedClient:
    """A PacedClient that sends the user's bearer token and retries once with a
    refreshed token on 401."""

    def __init__(self, http: PacedClient, tokens):
        self.http = http
        self.tokens = tokens

    async def call(self, method: str, url: str, **kwargs) -> httpx.Response:
        token = await self.tokens.get()
        response = await self.http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        if response.status_code == 401:
            token = await self.tokens.refresh_after_401(token)
            response = await self.http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
            if response.status_code == 401:
                raise AuthExpired(f"{self.http.service} rejected a freshly refreshed token")
        response.raise_for_status()
        return response


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None

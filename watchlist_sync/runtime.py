"""Wires settings, storage and adapters together and runs syncs one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .adapters.base import OAuthClient
from .adapters.simkl import SimklAdapter, SimklOAuth
from .adapters.trakt import TraktAdapter, TraktOAuth
from .auth import TokenSource
from .config import Settings
from .db import Store
from .engine import Engine

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir / "watchlist.db")
        self.store.mark_interrupted_runs()
        self.oauth: dict[str, OAuthClient] = {}
        if settings.trakt_client_id:
            self.oauth["trakt"] = TraktOAuth(
                settings.trakt_client_id, settings.trakt_client_secret, settings.redirect_uri("trakt")
            )
        if settings.simkl_client_id:
            self.oauth["simkl"] = SimklOAuth(
                settings.simkl_client_id, settings.simkl_client_secret, settings.redirect_uri("simkl")
            )
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def engine(self) -> AsyncIterator[Engine | None]:
        adapters = {}
        for name, oauth in self.oauth.items():
            account = self.store.account(name)
            if not account.connected:
                continue
            tokens = TokenSource(self.store, oauth)
            client_id = self.settings.trakt_client_id if name == "trakt" else self.settings.simkl_client_id
            adapters[name] = (TraktAdapter if name == "trakt" else SimklAdapter)(client_id, tokens)
        try:
            yield Engine(self.store, adapters) if adapters else None
        finally:
            for adapter in adapters.values():
                await adapter.aclose()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def sync(self, dry_run: bool = False) -> int | None:
        async with self._lock, self.engine() as engine:
            if engine is None:
                return None
            return await engine.sync(dry_run=dry_run or self.settings.dry_run)

    async def approve(self, run_id: int) -> int | None:
        async with self._lock, self.engine() as engine:
            if engine is None:
                return None
            return await engine.approve(run_id)

    def spawn(self, coro) -> None:
        """Run a sync job in the background, logging rather than losing failures."""
        task = asyncio.create_task(self._guard(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guard(self, coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("sync job failed")

    async def loop(self) -> None:
        while True:
            await self._guard(self.sync())
            await asyncio.sleep(self.settings.interval_minutes * 60)

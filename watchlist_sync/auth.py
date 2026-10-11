from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from .adapters.base import AuthExpired, OAuthClient, Token
from .db import Store

REFRESH_MARGIN = timedelta(days=1)


def expires_at(seconds: int, issued_at: int | None = None) -> str:
    start = datetime.fromtimestamp(issued_at, UTC) if issued_at else datetime.now(UTC)
    return (start + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class TokenSource:
    """Hands out a valid access token for one service and owns its refresh.
    One owner matters: a Trakt refresh token works once, and a Simkl refresh
    invalidates the previous access token."""

    def __init__(self, store: Store, oauth: OAuthClient):
        self.store = store
        self.oauth = oauth
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            account = self.store.account(self.oauth.name)
            if not account.connected:
                raise AuthExpired(f"{self.oauth.name} is not connected")
            if account.auth_error:
                raise AuthExpired(account.auth_error)
            expiry = datetime.fromisoformat(account.expires_at) if account.expires_at else None
            if expiry is None or expiry - datetime.now(UTC) < REFRESH_MARGIN:
                await self._refresh()
            return self.store.account(self.oauth.name).access_token

    async def refresh_after_401(self, rejected: str) -> str:
        async with self._lock:
            if self.store.account(self.oauth.name).access_token == rejected:
                await self._refresh()
            return self.store.account(self.oauth.name).access_token

    async def _refresh(self) -> None:
        account = self.store.account(self.oauth.name)
        if not account.refresh_token:
            self._fail("no refresh token stored; reconnect")
        current = Token(account.access_token, account.refresh_token, account.expires_at or "", account.scope)
        try:
            fresh = await self.oauth.refresh(current)
        except AuthExpired as exc:
            self._fail(str(exc))
        self.store.save_tokens(self.oauth.name, fresh.access_token, fresh.refresh_token, fresh.expires_at, fresh.scope)

    def _fail(self, reason: str):
        self.store.update_account(self.oauth.name, auth_error=reason)
        raise AuthExpired(reason)

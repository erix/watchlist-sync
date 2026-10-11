from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Protocol

from ..domain import Kind, RemoteItem, RemoteState, Title, WriteResult


class AuthExpired(Exception):
    """The stored login can no longer be refreshed; the user must reconnect."""


@dataclass(frozen=True)
class Token:
    access_token: str
    refresh_token: str | None
    expires_at: str
    scope: str | None


class Adapter(Protocol):
    name: str

    async def activity_marker(self) -> str:
        """A cheap value that changes whenever the watchlist may have changed."""
        ...

    async def fetch(self) -> RemoteState: ...

    async def add(self, titles: list[Title]) -> WriteResult: ...

    async def remove(self, titles: list[Title]) -> WriteResult: ...

    async def lookup(self, title: Title) -> list[RemoteItem]:
        """Candidates for `title` on this service: by external ID first, then by
        title and year."""
        ...

    def can_address(self, title: Title) -> bool:
        """Whether `title` carries an ID this service accepts in writes."""
        ...


class OAuthClient(Protocol):
    name: str

    def authorize_url(self, state: str, challenge: str) -> str: ...

    async def exchange(self, code: str, verifier: str) -> Token: ...

    async def refresh(self, token: Token) -> Token: ...

    async def revoke(self, token: Token) -> None: ...


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def kind_of(media_type: str) -> Kind:
    return Kind.MOVIE if media_type in ("movie", "movies") else Kind.SHOW

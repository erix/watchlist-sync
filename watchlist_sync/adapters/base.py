from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..domain import HistoryState, Kind, RemoteItem, RemoteState, Title, TitleId, WatchKey, WriteResult


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

    async def history_marker(self) -> str:
        """A cheap value that changes whenever the watch history may have changed."""
        ...

    async def fetch_history(self) -> HistoryState: ...

    async def account_created(self) -> str | None:
        """When the account was created, if plays dated earlier can only have
        come from an import; None if every play is trusted."""
        ...

    async def add_history(self, entries: list[tuple[Title, WatchKey, str]]) -> set[WatchKey]:
        """Mark each (title, key, watched_at) watched; return the keys that landed."""
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


def history_body(entries: list[tuple[Title, WatchKey, str]], ids_for: Callable[[Title], dict]) -> dict[str, list]:
    """The /sync/history payload Trakt and Simkl share: movies by ID, episodes
    nested under their show and season."""
    body: dict[str, list] = {}
    shows: dict[TitleId, dict[int, list]] = {}
    titles: dict[TitleId, Title] = {}
    for title, key, watched_at in entries:
        if title.kind is Kind.MOVIE:
            body.setdefault("movies", []).append({"ids": ids_for(title), "watched_at": watched_at})
        else:
            titles[title.id] = title
            seasons = shows.setdefault(title.id, {})
            seasons.setdefault(key.season, []).append({"number": key.episode, "watched_at": watched_at})
    for title_id, seasons in shows.items():
        body.setdefault("shows", []).append(
            {
                "ids": ids_for(titles[title_id]),
                "seasons": [{"number": n, "episodes": episodes} for n, episodes in seasons.items()],
            }
        )
    return body

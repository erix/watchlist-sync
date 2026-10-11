"""Trakt adapter. Endpoints per https://docs.trakt.tv/openapi.json (2026-10)."""

from __future__ import annotations

import json
from urllib.parse import quote, urlencode

import httpx

from .. import __version__
from ..auth import expires_at
from ..domain import Kind, RemoteItem, RemoteState, Title, WriteResult, clean_ids
from .base import AuthExpired, Token
from .http import AuthedClient, Pace, PacedClient

API = "https://api.trakt.tv"
AUTH = "https://auth.trakt.tv"
# AUTHED_API_GET_LIMIT is 500 per 5 minutes; AUTHED_API_POST_LIMIT is 1 per second.
PACE = Pace(get_interval=0.6, post_interval=1.1)
WRITE_IDS = {Kind.MOVIE: ("trakt", "imdb", "tmdb"), Kind.SHOW: ("trakt", "imdb", "tmdb", "tvdb")}
SECTION = {Kind.MOVIE: "movies", Kind.SHOW: "shows"}
BATCH = 100


def _headers(client_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": f"watchlist-sync/{__version__}",
        "trakt-api-key": client_id,
        "trakt-api-version": "2",
    }


class TraktOAuth:
    name = "trakt"

    def __init__(self, client_id: str, client_secret: str | None, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def authorize_url(self, state: str, challenge: str) -> str:
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTH}/oauth/authorize?{urlencode(query, quote_via=quote)}"

    async def exchange(self, code: str, verifier: str) -> Token:
        return await self._token({"grant_type": "authorization_code", "code": code, "code_verifier": verifier})

    async def refresh(self, token: Token) -> Token:
        return await self._token({"grant_type": "refresh_token", "refresh_token": token.refresh_token})

    async def revoke(self, token: Token) -> None:
        body = {"token": token.access_token, "client_id": self.client_id}
        if self.client_secret:
            body["client_secret"] = self.client_secret
        async with httpx.AsyncClient(headers=_headers(self.client_id), timeout=30) as client:
            await client.post(f"{AUTH}/oauth/revoke", json=body)

    async def _token(self, grant: dict[str, str]) -> Token:
        body = {**grant, "client_id": self.client_id, "redirect_uri": self.redirect_uri}
        if self.client_secret:
            body["client_secret"] = self.client_secret
        async with httpx.AsyncClient(headers=_headers(self.client_id), timeout=30) as client:
            response = await client.post(f"{AUTH}/oauth/token", json=body)
        if response.status_code in (400, 401):
            raise AuthExpired(f"Trakt token request failed: {response.text[:300]}")
        response.raise_for_status()
        data = response.json()
        return Token(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=expires_at(data["expires_in"], data.get("created_at")),
            scope=data.get("scope"),
        )


class TraktAdapter:
    name = "trakt"

    def __init__(self, client_id: str, tokens):
        self.http = PacedClient("trakt", API, PACE, _headers(client_id))
        self.api = AuthedClient(self.http, tokens)

    async def activity_marker(self) -> str:
        data = (await self.api.call("GET", "/sync/last_activities")).json()
        # Watching a title can drop it from the watchlist, so watch times count too.
        return json.dumps(
            [
                data["movies"].get("watchlisted_at"),
                data["shows"].get("watchlisted_at"),
                data["movies"].get("watched_at"),
                data["episodes"].get("watched_at"),
            ]
        )

    async def fetch(self) -> RemoteState:
        items: list[RemoteItem] = []
        for kind, section in SECTION.items():
            page, pages = 1, 1
            while page <= pages:
                response = await self.api.call(
                    "GET", f"/sync/watchlist/{section}/rank/asc", params={"page": page, "limit": 250}
                )
                pages = int(response.headers.get("X-Pagination-Page-Count", "1"))
                for entry in response.json():
                    media = entry.get(kind.value)
                    if media:
                        items.append(_item(kind, media))
                page += 1
        return RemoteState(watchlist=items)

    async def add(self, titles: list[Title]) -> WriteResult:
        return await self._write("/sync/watchlist", titles)

    async def remove(self, titles: list[Title]) -> WriteResult:
        return await self._write("/sync/watchlist/remove", titles)

    async def _write(self, path: str, titles: list[Title]) -> WriteResult:
        ok: set[int] = set()
        missing: set[int] = set()
        for start in range(0, len(titles), BATCH):
            batch = titles[start : start + BATCH]
            body: dict[str, list] = {"movies": [], "shows": []}
            for t in batch:
                body[SECTION[t.kind]].append({"ids": _write_ids(t)})
            data = (await self.api.call("POST", path, json=body)).json()
            not_found = data.get("not_found", {})
            for t in batch:
                rejected = any(_same(t, entry.get("ids", {})) for entry in not_found.get(SECTION[t.kind], []))
                (missing if rejected else ok).add(t.id)
        return WriteResult(frozenset(ok), frozenset(missing))

    async def lookup(self, title: Title) -> list[RemoteItem]:
        search_type = title.kind.value
        for ns in ("imdb", "tmdb", "tvdb"):
            value = title.ids.get(ns)
            if not value or (ns == "tvdb" and title.kind is Kind.MOVIE):
                continue
            response = await self.api.call("GET", f"/search/{ns}/{value}", params={"type": search_type})
            found = [_item(title.kind, r[search_type]) for r in response.json() if r.get(search_type)]
            if found:
                return found
        if not title.title:
            return []
        response = await self.api.call(
            "GET", f"/search/{search_type}/exact", params={"query": title.title, "limit": 10}
        )
        return [_item(title.kind, r[search_type]) for r in response.json() if r.get(search_type)]

    def can_address(self, title: Title) -> bool:
        return any(title.ids.get(ns) for ns in WRITE_IDS[title.kind])

    async def aclose(self) -> None:
        await self.http.aclose()


def _item(kind: Kind, media: dict) -> RemoteItem:
    return RemoteItem(
        service="trakt",
        kind=kind,
        ids=clean_ids(media.get("ids")),
        title=media.get("title"),
        year=media.get("year"),
    )


def _write_ids(title: Title) -> dict[str, object]:
    ids: dict[str, object] = {}
    for ns in WRITE_IDS[title.kind]:
        value = title.ids.get(ns)
        if value:
            ids[ns] = int(value) if ns != "imdb" else value
    return ids


def _same(title: Title, ids: dict) -> bool:
    return any(str(v) == title.ids.get(ns) for ns, v in ids.items() if v is not None)

"""Simkl adapter. Endpoints per https://api.simkl.org/openapi.json (2026-10).

Simkl has no separate watchlist: every library item sits in one status bucket.
The watchlist is the `plantowatch` bucket. Items in any other bucket are
reported as `elsewhere` so the engine never moves them, and removal goes
through /sync/history/remove, which deletes the item's history too, so the
engine only removes items that have none.
"""

from __future__ import annotations

import json
from urllib.parse import quote, urlencode

import httpx

from .. import __version__
from ..auth import expires_at
from ..domain import HistoryState, Kind, RemoteItem, RemoteState, RemoteWatch, Title, WatchKey, WriteResult, clean_ids
from .base import AuthExpired, Token, history_body
from .http import AuthedClient, Pace, PacedClient

API = "https://api.simkl.com"
AUTHORIZE = "https://simkl.com/oauth2/authorize"
ISSUER = "https://simkl.com"
SCOPE = "media:read media:write"
APP_NAME = "watchlist-sync"
# 10 GET/s and 1 POST/s; going over on POST triggers a temporary block.
PACE = Pace(get_interval=0.2, post_interval=1.1)
WRITE_IDS = ("simkl", "imdb", "tmdb", "tvdb", "mal", "anidb", "anilist")
STATUSES = ("plantowatch", "watching", "completed", "hold", "dropped")
BATCH = 100


def _ua() -> str:
    return f"{APP_NAME}/{__version__}"


class SimklOAuth:
    """AUTH V2: PKCE is mandatory, refresh tokens do not rotate."""

    name = "simkl"

    def __init__(self, client_id: str, client_secret: str | None, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def authorize_url(self, state: str, challenge: str) -> str:
        query = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE}?{urlencode(query, quote_via=quote)}"

    async def exchange(self, code: str, verifier: str) -> Token:
        token = await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
            }
        )
        # A misspelled or missing scope silently yields a read-only token.
        if "media:write" not in (token.scope or ""):
            raise AuthExpired(f"Simkl granted scope '{token.scope}', which cannot write")
        return token

    async def refresh(self, token: Token) -> Token:
        return await self._token({"grant_type": "refresh_token", "refresh_token": token.refresh_token})

    async def revoke(self, token: Token) -> None:
        async with httpx.AsyncClient(headers={"User-Agent": _ua()}, timeout=30) as client:
            await client.post(f"{API}/oauth2/revoke", data=self._client({"token": token.refresh_token or token.access_token}))

    def _client(self, form: dict[str, str]) -> dict[str, str]:
        form = {**form, "client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        return form

    async def _token(self, grant: dict[str, str]) -> Token:
        async with httpx.AsyncClient(headers={"User-Agent": _ua()}, timeout=30) as client:
            response = await client.post(f"{API}/oauth2/token", data=self._client(grant))
        if response.status_code in (400, 401):
            raise AuthExpired(f"Simkl token request failed: {response.text[:300]}")
        response.raise_for_status()
        data = response.json()
        return Token(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=expires_at(data["expires_in"]),
            scope=data.get("scope"),
        )


class SimklAdapter:
    name = "simkl"

    def __init__(self, client_id: str, tokens):
        self.http = PacedClient(
            "simkl",
            API,
            PACE,
            headers={"User-Agent": _ua(), "Content-Type": "application/json"},
            params={"client_id": client_id, "app-name": APP_NAME, "app-version": __version__},
        )
        self.api = AuthedClient(self.http, tokens)
        self._activity: dict | None = None

    async def _activities(self) -> dict:
        # Read once per adapter, which lives for one sync: the daily quota is small.
        if self._activity is None:
            self._activity = (await self.api.call("GET", "/sync/activities")).json() or {}
        return self._activity

    async def activity_marker(self) -> str:
        data = await self._activities()
        buckets = {}
        for section in ("movies", "tv_shows", "anime"):
            block = data.get(section) or {}
            buckets[section] = {k: block.get(k) for k in (*STATUSES, "removed_from_list")}
        return json.dumps(buckets, sort_keys=True)

    async def fetch(self) -> RemoteState:
        data = (await self.api.call("GET", "/sync/all-items")).json() or {}
        watchlist: list[RemoteItem] = []
        elsewhere: list[RemoteItem] = []
        for section in ("movies", "shows", "anime"):
            for entry in data.get(section) or []:
                item = _library_item(section, entry)
                (watchlist if item.status == "watchlist" else elsewhere).append(item)
        return RemoteState(watchlist=watchlist, elsewhere=elsewhere)

    async def add(self, titles: list[Title]) -> WriteResult:
        return await self._write(
            "/sync/add-to-list",
            titles,
            lambda t: {"to": "plantowatch", "ids": _write_ids(t)},
            lambda t: "anime" if t.anime else _section(t),
        )

    async def remove(self, titles: list[Title]) -> WriteResult:
        # Anime must go under shows[] here; an anime[] array is ignored.
        return await self._write(
            "/sync/history/remove", titles, lambda t: {"ids": _write_ids(t)}, lambda t: "shows" if t.anime else _section(t)
        )

    async def _write(self, path, titles, entry_for, section_for) -> WriteResult:
        ok: set[int] = set()
        missing: set[int] = set()
        for start in range(0, len(titles), BATCH):
            batch = titles[start : start + BATCH]
            body: dict[str, list] = {}
            for t in batch:
                body.setdefault(section_for(t), []).append(entry_for(t))
            data = (await self.api.call("POST", path, json=body)).json() or {}
            rejected = [e for entries in (data.get("not_found") or {}).values() for e in entries or []]
            for t in batch:
                if any(_same(t, (e.get("ids") or {})) for e in rejected):
                    missing.add(t.id)
                else:
                    ok.add(t.id)
        return WriteResult(frozenset(ok), frozenset(missing))

    async def history_marker(self) -> str:
        data = await self._activities()
        return json.dumps([(data.get(section) or {}).get("all") for section in ("movies", "tv_shows")])

    async def fetch_history(self) -> HistoryState:
        """Completed movies and every watched episode of non-anime shows. Anime
        is a separate type on Simkl and is not requested. Simkl lists no
        episodes for some shows (completed and dropped ones, in practice) while
        counting them watched; those are reported as opaque."""
        watches: list[RemoteWatch] = []
        opaque: list[RemoteItem] = []
        movies = (await self.api.call("GET", "/sync/all-items/movies/completed")).json() or {}
        for entry in movies.get("movies") or []:
            if entry.get("last_watched_at"):
                watches.append(RemoteWatch(_library_item("movies", entry), None, None, entry["last_watched_at"]))
        shows = (
            await self.api.call(
                "GET", "/sync/all-items/shows/", params={"extended": "full", "episode_watched_at": "yes"}
            )
        ).json() or {}
        for entry in shows.get("shows") or []:
            item = _library_item("shows", entry)
            listed = 0
            for season in entry.get("seasons") or []:
                for episode in season.get("episodes") or []:
                    listed += 1
                    watched_at = episode.get("watched_at") or entry.get("last_watched_at")
                    if season.get("number") is None or episode.get("number") is None or not watched_at:
                        continue
                    watches.append(RemoteWatch(item, season["number"], episode["number"], watched_at))
            if listed < (entry.get("watched_episodes_count") or 0):
                opaque.append(item)
        return HistoryState(watches, opaque)

    async def account_created(self) -> str | None:
        # Simkl's import invents plays: rated-only movies become completed, and
        # every episode before the latest watched one is filled in.
        data = (await self.api.call("POST", "/users/settings")).json() or {}
        return (data.get("user") or {}).get("joined_at")

    async def add_history(self, entries: list[tuple[Title, WatchKey, str]]) -> set[WatchKey]:
        landed: set[WatchKey] = set()
        for start in range(0, len(entries), BATCH):
            batch = entries[start : start + BATCH]
            data = (await self.api.call("POST", "/sync/history", json=history_body(batch, _write_ids))).json() or {}
            rejected = [e for found in (data.get("not_found") or {}).values() for e in found or []]
            for t, key, _ in batch:
                if not any(_same(t, e.get("ids") or {}) for e in rejected):
                    landed.add(key)
        return landed

    async def lookup(self, title: Title) -> list[RemoteItem]:
        for ns in ("simkl", "imdb", "tmdb", "tvdb", "mal", "anidb", "anilist"):
            value = title.ids.get(ns)
            if not value:
                continue
            params = {ns: value}
            if ns == "tmdb":
                params["type"] = "movie" if title.kind is Kind.MOVIE else "show"
            found = (await self.api.call("GET", "/search/id", params=params)).json() or []
            if found:
                # Simkl matched on this ID, so each hit carries it even if the
                # response does not echo it back.
                return [_search_item(r, extra={ns: value}, kind=title.kind) for r in found]
        if not title.title:
            return []
        params = {"title": title.title}
        if title.year is not None:
            params["year"] = str(title.year)
        found = (await self.api.call("GET", "/search/id", params=params)).json() or []
        return [_search_item(r) for r in found]

    def can_address(self, title: Title) -> bool:
        return any(title.ids.get(ns) for ns in WRITE_IDS)

    async def aclose(self) -> None:
        await self.http.aclose()


def _section(title: Title) -> str:
    return "movies" if title.kind is Kind.MOVIE else "shows"


def _library_item(section: str, entry: dict) -> RemoteItem:
    media = entry.get("movie") or entry.get("show") or {}
    if section == "movies":
        kind = Kind.MOVIE
    elif section == "anime":
        kind = Kind.MOVIE if entry.get("anime_type") == "movie" else Kind.SHOW
    else:
        kind = Kind.SHOW
    status = entry.get("status") or "unknown"
    if status == "notinteresting":
        status = "dropped"
    return RemoteItem(
        service="simkl",
        kind=kind,
        anime=section == "anime",
        ids=clean_ids(media.get("ids")),
        title=media.get("title"),
        year=media.get("year"),
        status="watchlist" if status == "plantowatch" else status,
        has_history=bool(entry.get("watched_episodes_count") or entry.get("last_watched_at")),
    )


def _search_item(result: dict, extra: dict[str, str] | None = None, kind: Kind | None = None) -> RemoteItem:
    simkl_type = result.get("type")
    if kind is None:
        kind = Kind.MOVIE if simkl_type == "movie" else Kind.SHOW
    return RemoteItem(
        service="simkl",
        kind=kind,
        anime=simkl_type == "anime",
        ids={**clean_ids(result.get("ids")), **(extra or {})},
        title=result.get("title"),
        year=result.get("year"),
    )


def _write_ids(title: Title) -> dict[str, object]:
    ids: dict[str, object] = {}
    for ns in WRITE_IDS:
        value = title.ids.get(ns)
        if value:
            ids[ns] = int(value) if ns == "simkl" else value
    return ids


def _same(title: Title, ids: dict) -> bool:
    return any(str(v) == title.ids.get(ns) for ns, v in ids.items() if v is not None)

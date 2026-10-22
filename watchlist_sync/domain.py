from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    MOVIE = "movie"
    SHOW = "show"


class Action(StrEnum):
    ADD = "add"
    REMOVE = "remove"


ServiceName = str
TitleId = int

# Namespaces the engine knows. TMDB, TVDB and Trakt reuse numbers across movies
# and shows, so every ID is stored together with its Kind.
ID_NAMESPACES = ("imdb", "tmdb", "tvdb", "mal", "anidb", "anilist", "trakt", "simkl")


def clean_ids(raw: Mapping[str, object] | None) -> dict[str, str]:
    if not raw:
        return {}
    ids: dict[str, str] = {}
    for ns in ID_NAMESPACES:
        value = raw.get(ns)
        if value is None or value == "" or value == 0:
            continue
        ids[ns] = str(value).strip()
    return ids


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


@dataclass(frozen=True)
class RemoteItem:
    """One title as a service reports it. `status` is "watchlist" for watchlist
    entries; anything else is a service-specific state outside the watchlist
    (e.g. Simkl "completed")."""

    service: ServiceName
    kind: Kind
    ids: Mapping[str, str]
    title: str | None
    year: int | None
    anime: bool = False
    status: str = "watchlist"
    has_history: bool = False

    @property
    def native_id(self) -> str | None:
        return self.ids.get(self.service)


@dataclass(frozen=True)
class RemoteState:
    watchlist: list[RemoteItem]
    elsewhere: list[RemoteItem] = field(default_factory=list)


@dataclass(frozen=True)
class Title:
    id: TitleId
    kind: Kind
    anime: bool
    title: str | None
    year: int | None
    ids: Mapping[str, str]

    @property
    def type(self) -> str:
        return "anime" if self.anime else self.kind.value


@dataclass(frozen=True)
class WriteResult:
    ok: frozenset[TitleId]
    not_found: frozenset[TitleId]


@dataclass(frozen=True)
class ServiceView:
    """What the planner needs to know about one service, in title IDs."""

    live: frozenset[TitleId]
    snapshot: frozenset[TitleId]
    elsewhere: Mapping[TitleId, str] = field(default_factory=dict)
    history: frozenset[TitleId] = frozenset()


@dataclass(frozen=True)
class Op:
    service: ServiceName
    action: Action
    title_id: TitleId


@dataclass(frozen=True)
class Conflict:
    title_id: TitleId
    added_on: frozenset[ServiceName]
    removed_on: frozenset[ServiceName]


@dataclass(frozen=True)
class Skip:
    service: ServiceName
    title_id: TitleId
    reason: str


@dataclass(frozen=True)
class Plan:
    ops: tuple[Op, ...]
    conflicts: tuple[Conflict, ...]
    skips: tuple[Skip, ...]


@dataclass(frozen=True)
class WatchKey:
    """One movie (season and episode both None) or one episode of a show."""

    title_id: TitleId
    season: int | None = None
    episode: int | None = None

    def sort_key(self) -> tuple[int, int, int]:
        return (self.title_id, -1 if self.season is None else self.season, -1 if self.episode is None else self.episode)


@dataclass(frozen=True)
class RemoteWatch:
    """One play as a service reports it."""

    item: RemoteItem
    season: int | None
    episode: int | None
    watched_at: str


@dataclass(frozen=True)
class HistoryState:
    """A service's history as fetched. `opaque` holds shows the service counts
    episodes of as watched without listing which ones."""

    watches: list[RemoteWatch]
    opaque: list[RemoteItem] = field(default_factory=list)


@dataclass(frozen=True)
class HistoryView:
    """What the history planner needs to know about one service. `rejected`
    holds keys the service answered "not found" for, so they are not resent
    every run. `opaque` holds shows whose watched episodes the service does not
    list, so their episodes cannot be told apart from unwatched ones."""

    live: Mapping[WatchKey, str]
    snapshot: frozenset[WatchKey]
    rejected: frozenset[WatchKey] = frozenset()
    opaque: frozenset[TitleId] = frozenset()


@dataclass(frozen=True)
class HistoryOp:
    service: ServiceName
    key: WatchKey
    watched_at: str


@dataclass(frozen=True)
class HistorySkip:
    service: ServiceName
    key: WatchKey
    reason: str


@dataclass(frozen=True)
class HistoryPlan:
    ops: tuple[HistoryOp, ...]
    skips: tuple[HistorySkip, ...]

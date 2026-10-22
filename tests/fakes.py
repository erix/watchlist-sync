from __future__ import annotations

from dataclasses import dataclass, field

from watchlist_sync.domain import (
    HistoryState,
    Kind,
    RemoteItem,
    RemoteState,
    RemoteWatch,
    Title,
    WatchKey,
    WriteResult,
    normalize_title,
)
from watchlist_sync.matching import SHARED_NAMESPACES


class Crash(Exception):
    pass


@dataclass
class Entry:
    native: str
    kind: Kind
    title: str | None
    year: int | None
    ids: dict[str, str] = field(default_factory=dict)
    anime: bool = False


class FakeService:
    """In-memory service with a catalog, a watchlist, and items in other states.
    Resolves writes by native or shared IDs the way the real APIs do."""

    def __init__(self, name: str, catalog: list[Entry]):
        self.name = name
        self.catalog = {e.native: e for e in catalog}
        self.watchlist: set[str] = set()
        self.elsewhere: dict[str, str] = {}
        self.history: set[str] = set()
        self.version = 0
        self.writes: list[tuple[str, str]] = []
        self.crash_after: int | None = None
        # Watch history: (native, season, episode) -> watched_at.
        self.plays: dict[tuple[str, int | None, int | None], str] = {}
        self.history_version = 0
        self.history_fetches = 0
        self.history_writes: list[tuple[str, int | None, int | None, str]] = []
        self.history_crash_after: int | None = None
        # Shows counted as watched whose episodes are not listed.
        self.opaque: set[str] = set()
        self.joined_at: str | None = None
        self.account_reads = 0

    # The user acting on the service directly.

    def user_add(self, native: str) -> None:
        self.elsewhere.pop(native, None)
        self.watchlist.add(native)
        self.version += 1

    def user_remove(self, native: str) -> None:
        self.watchlist.discard(native)
        self.version += 1

    def user_move(self, native: str, status: str) -> None:
        self.watchlist.discard(native)
        self.elsewhere[native] = status
        self.version += 1

    def user_watch(self, native: str, watched_at: str, season: int | None = None, episode: int | None = None) -> None:
        self.plays[(native, season, episode)] = watched_at
        self.history_version += 1

    def user_unwatch(self, native: str, season: int | None = None, episode: int | None = None) -> None:
        del self.plays[(native, season, episode)]
        self.history_version += 1

    # Adapter protocol.

    async def activity_marker(self) -> str:
        return str(self.version)

    async def fetch(self) -> RemoteState:
        return RemoteState(
            watchlist=[self._item(n, "watchlist") for n in sorted(self.watchlist)],
            elsewhere=[self._item(n, s) for n, s in sorted(self.elsewhere.items())],
        )

    async def add(self, titles: list[Title]) -> WriteResult:
        return self._write("add", titles)

    async def remove(self, titles: list[Title]) -> WriteResult:
        return self._write("remove", titles)

    async def lookup(self, title: Title) -> list[RemoteItem]:
        by_id = [e for e in self.catalog.values() if self._matches(e, title)]
        if by_id:
            return [self._item(e.native, "watchlist") for e in by_id]
        wanted = normalize_title(title.title or "")
        return [
            self._item(e.native, "watchlist")
            for e in self.catalog.values()
            if e.title and normalize_title(e.title) == wanted
        ]

    def can_address(self, title: Title) -> bool:
        return bool(title.ids.get(self.name)) or any(title.ids.get(ns) for ns in SHARED_NAMESPACES)

    async def history_marker(self) -> str:
        return str(self.history_version)

    async def fetch_history(self) -> HistoryState:
        self.history_fetches += 1
        return HistoryState(
            [RemoteWatch(self._item(n, "watchlist"), s, e, at) for (n, s, e), at in sorted(self.plays.items(), key=str)],
            [self._item(n, "watchlist") for n in sorted(self.opaque)],
        )

    async def account_created(self) -> str | None:
        self.account_reads += 1
        return self.joined_at

    async def add_history(self, entries: list[tuple[Title, WatchKey, str]]) -> set[WatchKey]:
        landed: set[WatchKey] = set()
        for done, (title, key, watched_at) in enumerate(entries):
            if self.history_crash_after is not None and done >= self.history_crash_after:
                raise Crash(f"{self.name} crashed after {done} history writes")
            entry = next((e for e in self.catalog.values() if self._matches(e, title)), None)
            if entry is None:
                continue
            self.history_writes.append((entry.native, key.season, key.episode, watched_at))
            self.plays[(entry.native, key.season, key.episode)] = watched_at
            self.history_version += 1
            landed.add(key)
        return landed

    # Internals.

    def _write(self, action: str, titles: list[Title]) -> WriteResult:
        ok, missing = set(), set()
        for done, title in enumerate(titles):
            if self.crash_after is not None and done >= self.crash_after:
                raise Crash(f"{self.name} crashed after {done} writes")
            entry = next((e for e in self.catalog.values() if self._matches(e, title)), None)
            if entry is None:
                missing.add(title.id)
                continue
            self.writes.append((action, entry.native))
            if action == "add":
                self.watchlist.add(entry.native)
            else:
                self.watchlist.discard(entry.native)
            ok.add(title.id)
        self.version += 1
        return WriteResult(frozenset(ok), frozenset(missing))

    def _matches(self, entry: Entry, title: Title) -> bool:
        if entry.kind is not title.kind:
            return False
        if title.ids.get(self.name) == entry.native:
            return True
        return any(title.ids.get(ns) and entry.ids.get(ns) == title.ids.get(ns) for ns in SHARED_NAMESPACES)

    def _item(self, native: str, status: str) -> RemoteItem:
        e = self.catalog[native]
        return RemoteItem(
            service=self.name,
            kind=e.kind,
            ids={self.name: native, **e.ids},
            title=e.title,
            year=e.year,
            anime=e.anime,
            status=status,
            has_history=native in self.history,
        )

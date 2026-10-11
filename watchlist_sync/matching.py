"""Decides whether a lookup result is certainly the same title. Never guesses."""

from __future__ import annotations

from .domain import RemoteItem, Title, normalize_title

# IDs that identify the same catalog entry across services.
SHARED_NAMESPACES = ("imdb", "tmdb", "tvdb", "mal", "anidb", "anilist")


def certain_match(title: Title, candidates: list[RemoteItem]) -> RemoteItem | None:
    same_kind = [c for c in candidates if c.kind is title.kind]
    by_id = _distinct(
        c for c in same_kind if any(title.ids.get(ns) and title.ids.get(ns) == c.ids.get(ns) for ns in SHARED_NAMESPACES)
    )
    if len(by_id) == 1:
        return by_id[0]
    if by_id:
        return None
    if not title.title or title.year is None:
        return None
    wanted = normalize_title(title.title)
    by_name = _distinct(
        c for c in same_kind if c.title and c.year == title.year and normalize_title(c.title) == wanted
    )
    return by_name[0] if len(by_name) == 1 else None


def _distinct(items) -> list[RemoteItem]:
    seen: dict[str | None, RemoteItem] = {}
    for item in items:
        seen.setdefault(item.native_id, item)
    return list(seen.values())

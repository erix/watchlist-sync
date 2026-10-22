"""Pure watch-history planning: what each service has watched, and what it
held at earlier syncs, in; plays to add out. Add-only: nothing is ever removed.
No I/O."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Set
from datetime import UTC, datetime

from .domain import HistoryOp, HistoryPlan, HistorySkip, HistoryView, ServiceName, TitleId, WatchKey

ANIME = "anime is not history-synced: Simkl numbers its episodes differently"
OPAQUE = "Simkl lists no episodes for this show but counts them watched"
SPECIALS = "specials are not history-synced: services number them differently"
IMPORTED = "{services} marked it watched in an import, before the account existed"


def parse_time(value: str) -> datetime:
    """Trakt sends "...00.000Z" and Simkl "...00Z"; compare instants, not strings."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def plan_history(
    views: Mapping[ServiceName, HistoryView],
    anime: Set[TitleId],
    addressable: Callable[[ServiceName, TitleId], bool],
) -> HistoryPlan:
    """Mark every movie or episode watched on one service as watched on the
    others, with the latest watched_at any service reports. A service that held
    a key at an earlier sync and no longer does was un-watched there, so it is
    not added back. A play dated before its service's account existed came from
    an import there and is never copied elsewhere."""
    keys: set[WatchKey] = set()
    for view in views.values():
        keys |= view.live.keys()

    ops: list[HistoryOp] = []
    skips: list[HistorySkip] = []
    since = {s: parse_time(v.since) for s, v in views.items() if v.since}
    for key in sorted(keys, key=WatchKey.sort_key):
        holders = [s for s, v in views.items() if key in v.live]
        sources = [s for s in holders if s not in since or parse_time(views[s].live[key]) >= since[s]]
        watched_at = max((views[s].live[key] for s in sources), key=parse_time, default=None)
        for s, v in views.items():
            if key in v.live:
                continue
            if watched_at is None:
                skips.append(HistorySkip(s, key, IMPORTED.format(services=", ".join(holders))))
            elif key.season is not None and key.title_id in v.opaque:
                skips.append(HistorySkip(s, key, OPAQUE))
            elif key.season == 0:
                skips.append(HistorySkip(s, key, SPECIALS))
            elif key in v.snapshot:
                skips.append(HistorySkip(s, key, f"unwatched on {s}"))
            elif key.title_id in anime:
                skips.append(HistorySkip(s, key, ANIME))
            elif not addressable(s, key.title_id):
                skips.append(HistorySkip(s, key, f"no ID {s} accepts"))
            elif key in v.rejected:
                skips.append(HistorySkip(s, key, f"{s} did not find it"))
            else:
                ops.append(HistoryOp(s, key, watched_at))
    return HistoryPlan(tuple(ops), tuple(skips))


def next_history_snapshot(view: HistoryView) -> frozenset[WatchKey]:
    """Everything the service has held at any sync. `view.live` must already
    include the app's own successful writes. Keeping keys the service dropped
    is what makes an un-watch stick: a snapshot of `live` alone would forget
    it, and the next run would add the play back."""
    return frozenset(view.live) | view.snapshot

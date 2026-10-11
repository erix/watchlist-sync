"""Pure sync planning: snapshots + live state in, operations out. No I/O."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .domain import (
    Action,
    Conflict,
    Op,
    Plan,
    ServiceName,
    ServiceView,
    Skip,
    TitleId,
)


def plan(
    views: Mapping[ServiceName, ServiceView],
    blocked: Iterable[tuple[ServiceName, TitleId]] = (),
    resolutions: Mapping[TitleId, Action] | None = None,
) -> Plan:
    """Compare each service against its own snapshot, merge the changes, and
    return what to apply to every service.

    `blocked` holds (service, title) pairs that must not be added there, such as
    titles the service could not match. `resolutions` holds the user's answer to
    earlier conflicts and overrides whatever the diff says for that title.
    """
    blocked = set(blocked)
    resolutions = resolutions or {}
    titles: set[TitleId] = set()
    for view in views.values():
        titles |= view.live | view.snapshot | set(view.elsewhere)

    ops: list[Op] = []
    conflicts: list[Conflict] = []
    skips: list[Skip] = []

    for t in sorted(titles):
        added_on = frozenset(s for s, v in views.items() if t in v.live and t not in v.snapshot)
        removed_on = frozenset(s for s, v in views.items() if t in v.snapshot and t not in v.live)

        if t in resolutions:
            want = resolutions[t] is Action.ADD
        elif added_on and removed_on:
            conflicts.append(Conflict(t, added_on, removed_on))
            continue
        elif added_on:
            want = True
        elif removed_on:
            want = False
        else:
            # No change seen anywhere. Fill gaps (a newly connected service, an
            # earlier add that failed) but never infer a removal.
            want = any(t in v.live for v in views.values())

        for s, v in views.items():
            present = t in v.live
            if want and not present:
                if t in v.elsewhere:
                    skips.append(Skip(s, t, f"already in '{v.elsewhere[t]}' on {s}"))
                elif (s, t) in blocked:
                    skips.append(Skip(s, t, f"not matched on {s}"))
                else:
                    ops.append(Op(s, Action.ADD, t))
            elif not want and present:
                if t in v.history:
                    skips.append(Skip(s, t, f"has watch history on {s}; removing it would delete that history"))
                else:
                    ops.append(Op(s, Action.REMOVE, t))

    return Plan(tuple(ops), tuple(conflicts), tuple(skips))


def next_snapshot(view: ServiceView, conflicted: Iterable[TitleId]) -> frozenset[TitleId]:
    """The snapshot to store after a run: what the service holds now. `view.live`
    must already include the app's own successful writes, so they are never read
    back as user changes. Conflicted titles keep their old membership, so the
    conflict stays visible until the user resolves it."""
    snap = set(view.live)
    for t in conflicted:
        if t in view.snapshot:
            snap.add(t)
        else:
            snap.discard(t)
    return frozenset(snap)

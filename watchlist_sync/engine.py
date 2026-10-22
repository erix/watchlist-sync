"""Runs a sync: read every service, plan, apply through the outbox, commit
snapshots; the watchlist first, then watch history. All decisions live in
planner.py and history.py; this module does the I/O."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Mapping

import httpx

from .adapters.base import Adapter
from .db import Store, now
from .domain import (
    Action,
    HistoryOp,
    HistoryPlan,
    HistoryView,
    Op,
    Plan,
    RemoteItem,
    ServiceName,
    ServiceView,
    Title,
    TitleId,
    WatchKey,
)
from .history import next_history_snapshot, plan_history
from .matching import certain_match
from .planner import next_snapshot, plan

log = logging.getLogger(__name__)

MAX_REPLANS = 500
# A history plan can skip thousands of anime episodes on every run; the run
# keeps the counts and this many rows.
MAX_STORED_SKIPS = 500


class Engine:
    def __init__(self, store: Store, adapters: Mapping[ServiceName, Adapter]):
        self.store = store
        self.adapters = dict(adapters)

    async def sync(self, dry_run: bool = False) -> int:
        """Run one sync and return its run ID. Forced to a dry run while any
        service still awaits approval of its first sync. History has its own
        first approval: until then the history part of each run is a dry run,
        while the watchlist part applies normally."""
        dry_run = dry_run or any(self.store.account(s).needs_dry_run for s in self.adapters)
        history_dry_run = dry_run or any(self.store.account(s).needs_history_dry_run for s in self.adapters)
        run_id = self.store.start_run(dry_run)
        described: dict | None = None
        try:
            pending = [] if dry_run else self.store.pending_ops()
            markers = await self._read_services(force=bool(pending))
            if pending:
                await self._resume(pending)
            result = await self._plan()
            for c in result.conflicts:
                self.store.record_conflict(c.title_id, c.added_on, c.removed_on)
            described = self._describe(result)
            if not dry_run:
                self.store.enqueue(run_id, list(result.ops))
                await self._execute(self.store.pending_ops())
                self._commit(markers, [c.title_id for c in result.conflicts])
            described["history"] = await self._sync_history(run_id, history_dry_run)
            self.store.finish_run(run_id, "dry-run" if dry_run else "ok", json.dumps(described))
            return run_id
        except Exception as exc:
            # The watchlist part may already be committed; keep its plan on the run.
            plan_json = json.dumps(described) if described is not None else None
            self.store.finish_run(run_id, "failed", plan_json, error=f"{type(exc).__name__}: {exc}")
            for s in self.adapters:
                self.store.update_account(s, last_sync_status="failed", last_error=str(exc))
            raise

    async def approve(self, dry_run_id: int) -> int:
        """Apply exactly the operations a run left for approval: its watchlist ops
        if it was a dry run, and its history ops if its history part was one.
        Then mark every service as past that first sync."""
        run = self.store.run(dry_run_id)
        approved = json.loads(run["plan_json"]) if run is not None and run["plan_json"] else {}
        watchlist = run is not None and run["status"] == "dry-run"
        history = approved.get("history") if (approved.get("history") or {}).get("dry_run") else None
        if not watchlist and history is None:
            raise ValueError(f"run {dry_run_id} has nothing awaiting approval")
        known = self.store.titles()
        run_id = self.store.start_run(False)
        try:
            applied = {"ops": [], "conflicts": [], "skips": []}
            if watchlist:
                ops = [
                    Op(o["service"], Action(o["action"]), o["title_id"])
                    for o in approved["ops"]
                    if o["title_id"] in known and o["service"] in self.adapters
                ]
                self.store.enqueue(run_id, ops)
                markers = await self._read_services(force=True)
                await self._resume(self.store.pending_ops())
                self._commit(markers, [c["title_id"] for c in approved["conflicts"]])
                for s in self.adapters:
                    self.store.update_account(s, needs_dry_run=0)
                applied = {k: approved[k] for k in ("ops", "conflicts", "skips")}
            if history is not None:
                history_ops = [
                    HistoryOp(o["service"], WatchKey(o["title_id"], o["season"], o["episode"]), o["watched_at"])
                    for o in history["ops"]
                    if o["title_id"] in known and o["service"] in self.adapters
                ]
                self.store.enqueue_history(run_id, history_ops)
                await self._read_history(force=True)
                await self._resume_history(self.store.pending_history_ops())
                self._commit_history()
                for s in self.adapters:
                    self.store.update_account(s, needs_history_dry_run=0)
                applied["history"] = {**history, "dry_run": False}
            self.store.finish_run(run_id, "ok", json.dumps(applied))
            return run_id
        except Exception as exc:
            self.store.finish_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
            raise

    async def _read_services(self, force: bool) -> dict[ServiceName, str]:
        """Refresh the cached state of each service whose activity marker moved.
        The marker is read before the fetch, so a change made during the fetch
        is picked up next time instead of being skipped."""
        markers: dict[ServiceName, str] = {}
        for name, adapter in self.adapters.items():
            marker = await adapter.activity_marker()
            markers[name] = marker
            if not force and marker == self.store.account(name).activity_marker:
                continue
            state = await adapter.fetch()
            items = state.watchlist + state.elsewhere
            for item in items:
                self.store.resolve(item)
            # Second pass: resolving one item can merge titles another resolved to.
            rows: dict[TitleId, tuple[str, bool]] = {}
            for item in items:
                t = self.store.resolve(item)
                if t not in rows or item.status == "watchlist":
                    rows[t] = (item.status, item.has_history)
            self.store.replace_remote_items(name, [(t, s, h) for t, (s, h) in rows.items()])
        for name in self.adapters:
            self.store.clear_unmatched_present(name)
        return markers

    def _view(self, service: ServiceName) -> ServiceView:
        rows = self.store.remote_items(service)
        return ServiceView(
            live=frozenset(r["title_id"] for r in rows if r["status"] == "watchlist"),
            snapshot=self.store.snapshot(service),
            elsewhere={r["title_id"]: r["status"] for r in rows if r["status"] != "watchlist"},
            history=frozenset(r["title_id"] for r in rows if r["has_history"]),
        )

    def _views(self) -> dict[ServiceName, ServiceView]:
        return {s: self._view(s) for s in self.adapters}

    async def _resume(self, pending: list[tuple[int, Op]]) -> None:
        """Finish ops left by an interrupted run. Each is checked against what the
        service holds now: an op that already took effect is only recorded, so
        nothing is written twice."""
        views = self._views()
        todo: list[tuple[int, Op]] = []
        for op_id, op in pending:
            view = views.get(op.service)
            if view is None:
                self.store.finish_op(op_id, op, "cancelled", "service not connected")
            elif (op.title_id in view.live) == (op.action is Action.ADD):
                self.store.finish_op(op_id, op, "done", "confirmed on service")
            elif op.action is Action.ADD and op.title_id in view.elsewhere:
                self.store.finish_op(op_id, op, "cancelled", f"now '{view.elsewhere[op.title_id]}'")
            elif op.action is Action.REMOVE and op.title_id in view.history:
                self.store.finish_op(op_id, op, "cancelled", "has watch history")
            else:
                todo.append((op_id, op))
        await self._execute(todo)

    async def _plan(self) -> Plan:
        """Plan, and before adding a title to a service that has no ID for it, look
        it up there. A certain match links the title (possibly merging it with a
        title already on that service), so the plan is recomputed."""
        for _ in range(MAX_REPLANS):
            result = plan(self._views(), self.store.blocked_pairs(), self.store.resolutions())
            needs_lookup = [
                op
                for op in result.ops
                if op.action is Action.ADD
                and not self.adapters[op.service].can_address(self.store.title(op.title_id))
            ]
            if not needs_lookup:
                return result
            op = needs_lookup[0]
            await self._link_or_unmatch(self.store.title(op.title_id), op.service)
        raise RuntimeError("planning did not settle")

    async def _link_or_unmatch(self, title: Title, service: ServiceName) -> bool:
        candidates = await self.adapters[service].lookup(title)
        match = certain_match(title, candidates)
        if match is None:
            self.store.add_unmatched(
                service,
                title.id,
                [{"title": c.title, "year": c.year, "type": c.kind.value, "ids": dict(c.ids)} for c in candidates[:10]],
            )
            return False
        self.store.attach_ids(title.id, title.kind, match.ids, source=f"{service}-lookup")
        self.store.clear_unmatched(service, title.id)
        return True

    async def _execute(self, ops: list[tuple[int, Op]]) -> None:
        groups: dict[tuple[ServiceName, Action], list[tuple[int, Op]]] = defaultdict(list)
        for op_id, op in ops:
            groups[(op.service, op.action)].append((op_id, op))
        for (service, action), group in groups.items():
            adapter = self.adapters[service]
            titles = [self.store.title(op.title_id) for _, op in group]
            write = adapter.add if action is Action.ADD else adapter.remove
            try:
                result = await write(titles)
            except httpx.HTTPStatusError as exc:
                if not 400 <= exc.response.status_code < 500:
                    raise
                for op_id, op in group:
                    self.store.finish_op(op_id, op, "failed", f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
                continue
            for (op_id, op), title in zip(group, titles, strict=True):
                if op.title_id in result.ok:
                    self.store.finish_op(op_id, op, "done")
                else:
                    self.store.finish_op(op_id, op, "failed", f"{service} could not match this title")
                    if action is Action.ADD:
                        await self._link_or_unmatch(title, service)

    def _commit(self, markers: Mapping[ServiceName, str], conflicted: list[TitleId]) -> None:
        for s in self.adapters:
            self.store.replace_snapshot(s, next_snapshot(self._view(s), conflicted))
            self.store.update_account(
                s, activity_marker=markers[s], last_sync_at=now(), last_sync_status="ok", last_error=None
            )
        open_conflicts = {r["title_id"] for r in self.store.conflicts()}
        self.store.drop_conflicts(sorted(open_conflicts - set(conflicted)))

    def _describe(self, result: Plan) -> dict:
        ids = {op.title_id for op in result.ops} | {c.title_id for c in result.conflicts} | {s.title_id for s in result.skips}
        titles = self.store.titles(sorted(ids))

        def label(t: TitleId) -> dict:
            title = titles[t]
            return {"title_id": t, "title": title.title, "year": title.year, "type": title.type}

        return {
            "ops": [{**label(o.title_id), "service": o.service, "action": o.action.value} for o in result.ops],
            "conflicts": [
                {**label(c.title_id), "added_on": sorted(c.added_on), "removed_on": sorted(c.removed_on)}
                for c in result.conflicts
            ],
            "skips": [{**label(s.title_id), "service": s.service, "reason": s.reason} for s in result.skips],
        }

    # Watch history

    async def _sync_history(self, run_id: int, dry_run: bool) -> dict:
        pending = [] if dry_run else self.store.pending_history_ops()
        await self._read_history(force=bool(pending))
        if pending:
            await self._resume_history(pending)
        titles = self.store.titles()
        result = plan_history(
            self._history_views(),
            {t for t, title in titles.items() if title.anime},
            lambda s, t: self.adapters[s].can_address(titles[t]),
        )
        if not dry_run:
            self.store.enqueue_history(run_id, list(result.ops))
            await self._execute_history(self.store.pending_history_ops())
            self._commit_history()
        return self._describe_history(result, titles, dry_run)

    async def _read_history(self, force: bool) -> None:
        """Refresh the cached history of each service whose history marker moved,
        keeping the latest watched_at of each movie or episode."""
        for name, adapter in self.adapters.items():
            marker = await adapter.history_marker()
            if not force and marker == self.store.account(name).history_marker:
                continue
            state = await adapter.fetch_history()
            title_of = self._resolve_all([w.item for w in state.watches] + state.opaque)
            live: dict[WatchKey, str] = {}
            for w in state.watches:
                key = WatchKey(title_of[_identity(w.item)], w.season, w.episode)
                if key not in live or w.watched_at > live[key]:
                    live[key] = w.watched_at
            opaque = frozenset(title_of[_identity(i)] for i in state.opaque)
            self.store.replace_remote_history(name, live, opaque, marker)

    def _resolve_all(self, items: list[RemoteItem]) -> dict[tuple, TitleId]:
        """Resolve each distinct item once: a history holds one item per play,
        and the plays of a show share it."""
        distinct = {_identity(i): i for i in items}
        for item in distinct.values():
            self.store.resolve(item)
        # Second pass: resolving one item can merge titles another resolved to.
        return {k: self.store.resolve(item) for k, item in distinct.items()}

    def _history_views(self) -> dict[ServiceName, HistoryView]:
        return {
            s: HistoryView(
                self.store.remote_history(s),
                self.store.history_snapshot(s),
                self.store.rejected_history(s),
                self.store.opaque_history(s),
            )
            for s in self.adapters
        }

    async def _resume_history(self, pending: list[tuple[int, HistoryOp]]) -> None:
        """Finish history ops left by an interrupted run. A play already on the
        service is only recorded, so a resumed write never adds a second play."""
        views = self._history_views()
        finished: list[tuple[int, HistoryOp, str, str | None]] = []
        todo: list[tuple[int, HistoryOp]] = []
        for op_id, op in pending:
            view = views.get(op.service)
            if view is None:
                finished.append((op_id, op, "cancelled", "service not connected"))
            elif op.key in view.live:
                finished.append((op_id, op, "done", "confirmed on service"))
            else:
                todo.append((op_id, op))
        self.store.finish_history_ops(finished)
        await self._execute_history(todo)

    async def _execute_history(self, ops: list[tuple[int, HistoryOp]]) -> None:
        groups: dict[ServiceName, list[tuple[int, HistoryOp]]] = defaultdict(list)
        for op_id, op in ops:
            groups[op.service].append((op_id, op))
        for service, group in groups.items():
            titles = self.store.titles(sorted({op.key.title_id for _, op in group}))
            try:
                landed = await self.adapters[service].add_history(
                    [(titles[op.key.title_id], op.key, op.watched_at) for _, op in group]
                )
            except httpx.HTTPStatusError as exc:
                if not 400 <= exc.response.status_code < 500:
                    raise
                detail = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
                self.store.finish_history_ops([(op_id, op, "failed", detail) for op_id, op in group])
                continue
            self.store.finish_history_ops(
                [
                    (op_id, op, "done", None) if op.key in landed else (op_id, op, "rejected", f"{service} did not find it")
                    for op_id, op in group
                ]
            )

    def _commit_history(self) -> None:
        for s, view in self._history_views().items():
            self.store.replace_history_snapshot(s, next_history_snapshot(view))

    def _describe_history(self, result: HistoryPlan, titles: Mapping[TitleId, Title], dry_run: bool) -> dict:
        def entry(service: ServiceName, key: WatchKey) -> dict:
            title = titles[key.title_id]
            return {
                "service": service,
                "title_id": key.title_id,
                "title": title.title,
                "year": title.year,
                "type": title.type,
                "season": key.season,
                "episode": key.episode,
            }

        adds: dict[ServiceName, int] = defaultdict(int)
        for o in result.ops:
            adds[o.service] += 1
        reasons: dict[str, int] = defaultdict(int)
        for s in result.skips:
            reasons[s.reason] += 1
        return {
            "dry_run": dry_run,
            "adds": dict(adds),
            "skip_reasons": dict(reasons),
            "ops": [{**entry(o.service, o.key), "watched_at": o.watched_at} for o in result.ops],
            "skips": [{**entry(s.service, s.key), "reason": s.reason} for s in result.skips[:MAX_STORED_SKIPS]],
        }


def _identity(item: RemoteItem) -> tuple:
    return (item.kind, tuple(sorted(item.ids.items())))

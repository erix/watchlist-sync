"""Runs a sync: read every service, plan, apply through the outbox, commit
snapshots. All decisions live in planner.py; this module does the I/O."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Mapping

import httpx

from .adapters.base import Adapter
from .db import Store, now
from .domain import Action, Op, Plan, ServiceName, ServiceView, Title, TitleId
from .matching import certain_match
from .planner import next_snapshot, plan

log = logging.getLogger(__name__)

MAX_REPLANS = 500


class Engine:
    def __init__(self, store: Store, adapters: Mapping[ServiceName, Adapter]):
        self.store = store
        self.adapters = dict(adapters)

    async def sync(self, dry_run: bool = False) -> int:
        """Run one sync and return its run ID. Forced to a dry run while any
        service still awaits approval of its first sync."""
        dry_run = dry_run or any(self.store.account(s).needs_dry_run for s in self.adapters)
        run_id = self.store.start_run(dry_run)
        try:
            pending = [] if dry_run else self.store.pending_ops()
            markers = await self._read_services(force=bool(pending))
            if pending:
                await self._resume(pending)
            result = await self._plan()
            for c in result.conflicts:
                self.store.record_conflict(c.title_id, c.added_on, c.removed_on)
            described = self._describe(result)
            if dry_run:
                self.store.finish_run(run_id, "dry-run", described)
                return run_id
            self.store.enqueue(run_id, list(result.ops))
            await self._execute(self.store.pending_ops())
            self._commit(markers, [c.title_id for c in result.conflicts])
            self.store.finish_run(run_id, "ok", described)
            return run_id
        except Exception as exc:
            self.store.finish_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
            for s in self.adapters:
                self.store.update_account(s, last_sync_status="failed", last_error=str(exc))
            raise

    async def approve(self, dry_run_id: int) -> int:
        """Apply exactly the operations a dry run showed, then mark every service
        as past its first sync."""
        run = self.store.run(dry_run_id)
        if run is None or run["status"] != "dry-run":
            raise ValueError(f"run {dry_run_id} is not a dry run")
        approved = json.loads(run["plan_json"])
        known = self.store.titles()
        ops = [
            Op(o["service"], Action(o["action"]), o["title_id"])
            for o in approved["ops"]
            if o["title_id"] in known and o["service"] in self.adapters
        ]
        run_id = self.store.start_run(False)
        try:
            self.store.enqueue(run_id, ops)
            markers = await self._read_services(force=True)
            await self._resume(self.store.pending_ops())
            self._commit(markers, [c["title_id"] for c in approved["conflicts"]])
            for s in self.adapters:
                self.store.update_account(s, needs_dry_run=0)
            self.store.finish_run(run_id, "ok", run["plan_json"])
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

    def _describe(self, result: Plan) -> str:
        ids = {op.title_id for op in result.ops} | {c.title_id for c in result.conflicts} | {s.title_id for s in result.skips}
        titles = self.store.titles(sorted(ids))

        def label(t: TitleId) -> dict:
            title = titles[t]
            return {"title_id": t, "title": title.title, "year": title.year, "type": title.type}

        return json.dumps(
            {
                "ops": [{**label(o.title_id), "service": o.service, "action": o.action.value} for o in result.ops],
                "conflicts": [
                    {**label(c.title_id), "added_on": sorted(c.added_on), "removed_on": sorted(c.removed_on)}
                    for c in result.conflicts
                ],
                "skips": [{**label(s.title_id), "service": s.service, "reason": s.reason} for s in result.skips],
            }
        )

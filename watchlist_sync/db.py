from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .domain import Action, HistoryOp, Kind, Op, RemoteItem, ServiceName, Title, TitleId, WatchKey

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    anime INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    year INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS title_ids (
    title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (namespace, kind, value)
);
CREATE INDEX IF NOT EXISTS title_ids_by_title ON title_ids(title_id);

CREATE TABLE IF NOT EXISTS accounts (
    service TEXT PRIMARY KEY,
    access_token TEXT,
    refresh_token TEXT,
    expires_at TEXT,
    scope TEXT,
    auth_error TEXT,
    connected_at TEXT,
    activity_marker TEXT,
    needs_dry_run INTEGER NOT NULL DEFAULT 1,
    last_sync_at TEXT,
    last_sync_status TEXT,
    last_error TEXT
);

-- Latest fetched state of each service, used when its activity marker is unchanged.
CREATE TABLE IF NOT EXISTS remote_items (
    service TEXT NOT NULL,
    title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    has_history INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (service, title_id)
);

-- Each service's watchlist as of its last successful sync.
CREATE TABLE IF NOT EXISTS snapshots (
    service TEXT NOT NULL,
    title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    PRIMARY KEY (service, title_id)
);

CREATE TABLE IF NOT EXISTS unmatched (
    service TEXT NOT NULL,
    title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'open',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (service, title_id)
);

CREATE TABLE IF NOT EXISTS conflicts (
    title_id INTEGER PRIMARY KEY REFERENCES titles(id) ON DELETE CASCADE,
    added_on TEXT NOT NULL,
    removed_on TEXT NOT NULL,
    resolution TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    dry_run INTEGER NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT,
    error TEXT
);

-- The outbox. Rows in state 'done' are the change log.
CREATE TABLE IF NOT EXISTS ops (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    service TEXT NOT NULL,
    action TEXT NOT NULL,
    title_id INTEGER NOT NULL REFERENCES titles(id),
    state TEXT NOT NULL DEFAULT 'pending',
    detail TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ops_by_state ON ops(state);

-- Watch history: one row per movie or episode watched at least once. Movies
-- have NULL season and episode, so keys are unique over IFNULL(..., -1).
CREATE TABLE IF NOT EXISTS remote_history (
    service TEXT NOT NULL,
    title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    season INTEGER,
    episode INTEGER,
    watched_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS remote_history_key
    ON remote_history(service, title_id, IFNULL(season, -1), IFNULL(episode, -1));

-- Every play each service has held at any history sync. It only grows.
CREATE TABLE IF NOT EXISTS history_snapshots (
    service TEXT NOT NULL,
    title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    season INTEGER,
    episode INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS history_snapshots_key
    ON history_snapshots(service, title_id, IFNULL(season, -1), IFNULL(episode, -1));

-- The history outbox. 'rejected' means the service did not find the title.
CREATE TABLE IF NOT EXISTS history_ops (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    service TEXT NOT NULL,
    title_id INTEGER NOT NULL REFERENCES titles(id),
    season INTEGER,
    episode INTEGER,
    watched_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    detail TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS history_ops_by_state ON history_ops(state);

CREATE TABLE IF NOT EXISTS oauth_pending (
    state TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    verifier TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Columns added after the first release, applied to existing databases too.
ADDED_COLUMNS = {
    "accounts": {
        "history_marker": "TEXT",
        "needs_history_dry_run": "INTEGER NOT NULL DEFAULT 1",
    },
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Account:
    service: ServiceName
    access_token: str | None
    refresh_token: str | None
    expires_at: str | None
    scope: str | None
    auth_error: str | None
    connected_at: str | None
    activity_marker: str | None
    needs_dry_run: bool
    last_sync_at: str | None
    last_sync_status: str | None
    last_error: str | None
    history_marker: str | None
    needs_history_dry_run: bool

    @property
    def connected(self) -> bool:
        return self.access_token is not None


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        conn = sqlite3.connect(self.path)
        try:
            conn.executescript(SCHEMA)
            for table, columns in ADDED_COLUMNS.items():
                existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                for name, decl in columns.items():
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # Titles and IDs

    def title(self, title_id: TitleId) -> Title:
        with self.tx() as c:
            return _load_title(c, title_id)

    def titles(self, ids: list[TitleId] | None = None) -> dict[TitleId, Title]:
        with self.tx() as c:
            rows = c.execute("SELECT id FROM titles").fetchall() if ids is None else [{"id": i} for i in ids]
            return {r["id"]: _load_title(c, r["id"]) for r in rows}

    def resolve(self, item: RemoteItem) -> TitleId:
        """Find the title an item belongs to by its IDs, creating one if no ID is
        known. IDs that a service reports for one item prove those titles are
        the same, so distinct matches are merged."""
        with self.tx() as c:
            found = sorted(
                {
                    r["title_id"]
                    for ns, value in item.ids.items()
                    for r in c.execute(
                        "SELECT title_id FROM title_ids WHERE namespace=? AND kind=? AND value=?",
                        (ns, item.kind.value, value),
                    )
                }
            )
            if found:
                keep = found[0]
                for other in found[1:]:
                    _merge(c, keep, other)
            else:
                keep = c.execute(
                    "INSERT INTO titles(kind, anime, title, year, created_at) VALUES (?,?,?,?,?)",
                    (item.kind.value, int(item.anime), item.title, item.year, now()),
                ).lastrowid
            _fill_title(c, keep, item.title, item.year, item.anime)
            _attach_ids(c, keep, item.kind, item.ids, source=item.service)
            return keep

    def attach_ids(self, title_id: TitleId, kind: Kind, ids: Mapping[str, str], source: str) -> TitleId:
        """Attach IDs to a title. Returns the surviving title ID: when one of the
        IDs already belongs to another title, the two are merged."""
        with self.tx() as c:
            return _attach_ids(c, title_id, kind, ids, source)

    # Accounts

    def account(self, service: ServiceName) -> Account:
        with self.tx() as c:
            row = c.execute("SELECT * FROM accounts WHERE service=?", (service,)).fetchone()
            if row is None:
                c.execute("INSERT INTO accounts(service) VALUES (?)", (service,))
                row = c.execute("SELECT * FROM accounts WHERE service=?", (service,)).fetchone()
            return Account(
                **{
                    **dict(row),
                    "needs_dry_run": bool(row["needs_dry_run"]),
                    "needs_history_dry_run": bool(row["needs_history_dry_run"]),
                }
            )

    def update_account(self, service: ServiceName, **fields: object) -> None:
        self.account(service)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE accounts SET {cols} WHERE service=?", (*fields.values(), service))

    def save_tokens(self, service: ServiceName, access: str, refresh: str | None, expires_at: str, scope: str | None) -> None:
        self.update_account(
            service, access_token=access, refresh_token=refresh, expires_at=expires_at, scope=scope, auth_error=None
        )

    def connect(self, service: ServiceName, access: str, refresh: str | None, expires_at: str, scope: str | None) -> None:
        self.save_tokens(service, access, refresh, expires_at, scope)
        self.update_account(
            service,
            connected_at=now(),
            needs_dry_run=1,
            activity_marker=None,
            needs_history_dry_run=1,
            history_marker=None,
        )

    def disconnect(self, service: ServiceName) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE accounts SET access_token=NULL, refresh_token=NULL, expires_at=NULL, scope=NULL,"
                " auth_error=NULL, connected_at=NULL, activity_marker=NULL, needs_dry_run=1,"
                " history_marker=NULL, needs_history_dry_run=1 WHERE service=?",
                (service,),
            )
            for table in ("snapshots", "remote_items", "history_snapshots", "remote_history"):
                c.execute(f"DELETE FROM {table} WHERE service=?", (service,))

    # Snapshots and cached remote state

    def snapshot(self, service: ServiceName) -> frozenset[TitleId]:
        with self.tx() as c:
            rows = c.execute("SELECT title_id FROM snapshots WHERE service=?", (service,))
            return frozenset(r["title_id"] for r in rows)

    def replace_snapshot(self, service: ServiceName, titles: frozenset[TitleId]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM snapshots WHERE service=?", (service,))
            c.executemany("INSERT INTO snapshots(service, title_id) VALUES (?,?)", [(service, t) for t in titles])

    def remote_items(self, service: ServiceName) -> list[sqlite3.Row]:
        with self.tx() as c:
            return c.execute("SELECT * FROM remote_items WHERE service=?", (service,)).fetchall()

    def replace_remote_items(self, service: ServiceName, rows: list[tuple[TitleId, str, bool]]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM remote_items WHERE service=?", (service,))
            c.executemany(
                "INSERT OR REPLACE INTO remote_items(service, title_id, status, has_history) VALUES (?,?,?,?)",
                [(service, t, status, int(h)) for t, status, h in rows],
            )

    # Unmatched

    def blocked_pairs(self) -> set[tuple[ServiceName, TitleId]]:
        with self.tx() as c:
            return {(r["service"], r["title_id"]) for r in c.execute("SELECT service, title_id FROM unmatched")}

    def add_unmatched(self, service: ServiceName, title_id: TitleId, candidates: list[dict]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO unmatched(service, title_id, candidates_json, created_at) VALUES (?,?,?,?)"
                " ON CONFLICT(service, title_id) DO UPDATE SET candidates_json=excluded.candidates_json",
                (service, title_id, json.dumps(candidates), now()),
            )

    def clear_unmatched_present(self, service: ServiceName) -> None:
        """Drop unmatched entries for titles the service now holds."""
        with self.tx() as c:
            c.execute(
                "DELETE FROM unmatched WHERE service=? AND title_id IN"
                " (SELECT title_id FROM remote_items WHERE service=?)",
                (service, service),
            )

    def unmatched(self) -> list[sqlite3.Row]:
        with self.tx() as c:
            return c.execute("SELECT * FROM unmatched ORDER BY state, created_at").fetchall()

    def set_unmatched_state(self, service: ServiceName, title_id: TitleId, state: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE unmatched SET state=? WHERE service=? AND title_id=?", (state, service, title_id))

    def clear_unmatched(self, service: ServiceName, title_id: TitleId) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM unmatched WHERE service=? AND title_id=?", (service, title_id))

    # Conflicts

    def record_conflict(self, title_id: TitleId, added_on: frozenset[str], removed_on: frozenset[str]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO conflicts(title_id, added_on, removed_on, created_at) VALUES (?,?,?,?)"
                " ON CONFLICT(title_id) DO UPDATE SET added_on=excluded.added_on, removed_on=excluded.removed_on",
                (title_id, ",".join(sorted(added_on)), ",".join(sorted(removed_on)), now()),
            )

    def conflicts(self) -> list[sqlite3.Row]:
        with self.tx() as c:
            return c.execute("SELECT * FROM conflicts ORDER BY created_at").fetchall()

    def resolutions(self) -> dict[TitleId, Action]:
        with self.tx() as c:
            rows = c.execute("SELECT title_id, resolution FROM conflicts WHERE resolution IS NOT NULL")
            return {r["title_id"]: Action(r["resolution"]) for r in rows}

    def resolve_conflict(self, title_id: TitleId, resolution: Action) -> None:
        with self.tx() as c:
            c.execute("UPDATE conflicts SET resolution=? WHERE title_id=?", (resolution.value, title_id))

    def drop_conflicts(self, title_ids: list[TitleId]) -> None:
        with self.tx() as c:
            c.executemany("DELETE FROM conflicts WHERE title_id=?", [(t,) for t in title_ids])

    # Runs and the outbox

    def start_run(self, dry_run: bool) -> int:
        with self.tx() as c:
            return c.execute(
                "INSERT INTO sync_runs(started_at, dry_run, status) VALUES (?,?,'running')", (now(), int(dry_run))
            ).lastrowid

    def finish_run(self, run_id: int, status: str, plan_json: str | None = None, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE sync_runs SET finished_at=?, status=?, plan_json=COALESCE(?, plan_json), error=? WHERE id=?",
                (now(), status, plan_json, error, run_id),
            )

    def mark_interrupted_runs(self) -> None:
        """Runs still 'running' at startup were cut off by a restart; their
        pending ops are resumed by the next sync."""
        with self.tx() as c:
            c.execute("UPDATE sync_runs SET status='interrupted', finished_at=? WHERE status='running'", (now(),))

    def run(self, run_id: int) -> sqlite3.Row | None:
        with self.tx() as c:
            return c.execute("SELECT * FROM sync_runs WHERE id=?", (run_id,)).fetchone()

    def runs(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.tx() as c:
            return c.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def enqueue(self, run_id: int, ops: list[Op]) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT INTO ops(run_id, service, action, title_id, created_at) VALUES (?,?,?,?,?)",
                [(run_id, o.service, o.action.value, o.title_id, now()) for o in ops],
            )

    def pending_ops(self) -> list[tuple[int, Op]]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM ops WHERE state='pending' ORDER BY id").fetchall()
            return [(r["id"], Op(r["service"], Action(r["action"]), r["title_id"])) for r in rows]

    def finish_op(self, op_id: int, op: Op, state: str, detail: str | None = None) -> None:
        """Close an op. A successful op updates that service's snapshot in the same
        transaction, so a crash can never leave the write applied but unrecorded
        locally."""
        with self.tx() as c:
            c.execute(
                "UPDATE ops SET state=?, detail=?, finished_at=? WHERE id=?", (state, detail, now(), op_id)
            )
            if state != "done":
                return
            if op.action is Action.ADD:
                c.execute("INSERT OR IGNORE INTO snapshots(service, title_id) VALUES (?,?)", (op.service, op.title_id))
                c.execute(
                    "INSERT OR REPLACE INTO remote_items(service, title_id, status, has_history) VALUES (?,?,'watchlist',0)",
                    (op.service, op.title_id),
                )
            else:
                c.execute("DELETE FROM snapshots WHERE service=? AND title_id=?", (op.service, op.title_id))
                c.execute("DELETE FROM remote_items WHERE service=? AND title_id=?", (op.service, op.title_id))

    def change_log(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.tx() as c:
            return c.execute(
                "SELECT ops.*, titles.title, titles.year FROM ops JOIN titles ON titles.id = ops.title_id"
                " WHERE state != 'pending' ORDER BY ops.id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    # Watch history

    def remote_history(self, service: ServiceName) -> dict[WatchKey, str]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM remote_history WHERE service=?", (service,))
            return {_key(r): r["watched_at"] for r in rows}

    def replace_remote_history(self, service: ServiceName, live: Mapping[WatchKey, str], marker: str) -> None:
        """Store a fresh fetch together with the marker it was read under."""
        with self.tx() as c:
            c.execute("DELETE FROM remote_history WHERE service=?", (service,))
            c.executemany(
                "INSERT INTO remote_history(service, title_id, season, episode, watched_at) VALUES (?,?,?,?,?)",
                [(service, k.title_id, k.season, k.episode, at) for k, at in live.items()],
            )
            c.execute("UPDATE accounts SET history_marker=? WHERE service=?", (marker, service))

    def history_snapshot(self, service: ServiceName) -> frozenset[WatchKey]:
        with self.tx() as c:
            return frozenset(_key(r) for r in c.execute("SELECT * FROM history_snapshots WHERE service=?", (service,)))

    def replace_history_snapshot(self, service: ServiceName, keys: frozenset[WatchKey]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM history_snapshots WHERE service=?", (service,))
            c.executemany(
                "INSERT INTO history_snapshots(service, title_id, season, episode) VALUES (?,?,?,?)",
                [(service, k.title_id, k.season, k.episode) for k in keys],
            )

    def enqueue_history(self, run_id: int, ops: list[HistoryOp]) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT INTO history_ops(run_id, service, title_id, season, episode, watched_at, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                [(run_id, o.service, o.key.title_id, o.key.season, o.key.episode, o.watched_at, now()) for o in ops],
            )

    def pending_history_ops(self) -> list[tuple[int, HistoryOp]]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM history_ops WHERE state='pending' ORDER BY id").fetchall()
            return [(r["id"], HistoryOp(r["service"], _key(r), r["watched_at"])) for r in rows]

    def rejected_history(self, service: ServiceName) -> frozenset[WatchKey]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM history_ops WHERE service=? AND state='rejected'", (service,))
            return frozenset(_key(r) for r in rows)

    def finish_history_ops(self, results: list[tuple[int, HistoryOp, str, str | None]]) -> None:
        """Close history ops. A successful op lands in that service's live history
        and snapshot in the same transaction, so it is never read back as the
        user's own play or re-sent after a crash."""
        with self.tx() as c:
            for op_id, op, state, detail in results:
                c.execute(
                    "UPDATE history_ops SET state=?, detail=?, finished_at=? WHERE id=?", (state, detail, now(), op_id)
                )
                if state != "done":
                    continue
                k = op.key
                c.execute(
                    "INSERT OR IGNORE INTO remote_history(service, title_id, season, episode, watched_at)"
                    " VALUES (?,?,?,?,?)",
                    (op.service, k.title_id, k.season, k.episode, op.watched_at),
                )
                c.execute(
                    "INSERT OR IGNORE INTO history_snapshots(service, title_id, season, episode) VALUES (?,?,?,?)",
                    (op.service, k.title_id, k.season, k.episode),
                )

    # OAuth state

    def put_oauth_state(self, state: str, service: ServiceName, verifier: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM oauth_pending WHERE created_at < datetime('now', '-1 hour')")
            c.execute(
                "INSERT INTO oauth_pending(state, service, verifier, created_at) VALUES (?,?,?,datetime('now'))",
                (state, service, verifier),
            )

    def pop_oauth_state(self, state: str, service: ServiceName) -> str | None:
        with self.tx() as c:
            row = c.execute(
                "SELECT verifier FROM oauth_pending WHERE state=? AND service=?", (state, service)
            ).fetchone()
            c.execute("DELETE FROM oauth_pending WHERE state=?", (state,))
            return row["verifier"] if row else None


def _key(row: sqlite3.Row) -> WatchKey:
    return WatchKey(row["title_id"], row["season"], row["episode"])


def _load_title(c: sqlite3.Connection, title_id: TitleId) -> Title:
    row = c.execute("SELECT * FROM titles WHERE id=?", (title_id,)).fetchone()
    # Earliest-seen value wins when a namespace has several.
    rows = c.execute("SELECT * FROM title_ids WHERE title_id=? ORDER BY rowid DESC", (title_id,))
    ids = {r["namespace"]: r["value"] for r in rows}
    return Title(row["id"], Kind(row["kind"]), bool(row["anime"]), row["title"], row["year"], ids)


def _fill_title(c: sqlite3.Connection, title_id: TitleId, title: str | None, year: int | None, anime: bool) -> None:
    c.execute(
        "UPDATE titles SET title=COALESCE(title, ?), year=COALESCE(year, ?), anime=MAX(anime, ?) WHERE id=?",
        (title, year, int(anime), title_id),
    )


def _attach_ids(c: sqlite3.Connection, title_id: TitleId, kind: Kind, ids: Mapping[str, str], source: str) -> TitleId:
    for ns, value in ids.items():
        row = c.execute(
            "SELECT title_id FROM title_ids WHERE namespace=? AND kind=? AND value=?", (ns, kind.value, value)
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO title_ids(title_id, namespace, kind, value, source) VALUES (?,?,?,?,?)",
                (title_id, ns, kind.value, value, source),
            )
        elif row["title_id"] != title_id:
            keep, drop = sorted((title_id, row["title_id"]))
            _merge(c, keep, drop)
            title_id = keep
    return title_id


def _merge(c: sqlite3.Connection, keep: TitleId, drop: TitleId) -> None:
    """Fold title `drop` into `keep`, moving every reference."""
    row = c.execute("SELECT title, year, anime FROM titles WHERE id=?", (drop,)).fetchone()
    _fill_title(c, keep, row["title"], row["year"], bool(row["anime"]))
    c.execute("UPDATE title_ids SET title_id=? WHERE title_id=?", (keep, drop))
    for table in ("remote_items", "snapshots", "unmatched", "remote_history", "history_snapshots"):
        c.execute(f"UPDATE OR IGNORE {table} SET title_id=? WHERE title_id=?", (keep, drop))
        c.execute(f"DELETE FROM {table} WHERE title_id=?", (drop,))
    c.execute("UPDATE OR IGNORE conflicts SET title_id=? WHERE title_id=?", (keep, drop))
    c.execute("DELETE FROM conflicts WHERE title_id=?", (drop,))
    c.execute("UPDATE ops SET title_id=? WHERE title_id=?", (keep, drop))
    c.execute("UPDATE history_ops SET title_id=? WHERE title_id=?", (keep, drop))
    c.execute("DELETE FROM titles WHERE id=?", (drop,))

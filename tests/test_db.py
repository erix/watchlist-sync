import sqlite3

from watchlist_sync.db import Store
from watchlist_sync.domain import Action, Kind, Op, RemoteItem

# The accounts table as released before history sync.
ACCOUNTS_BEFORE_HISTORY = """
CREATE TABLE accounts (
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
INSERT INTO accounts(service, access_token, activity_marker, needs_dry_run) VALUES ('trakt', 'tok', 'm', 0);
"""


def test_database_from_before_history_sync_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(ACCOUNTS_BEFORE_HISTORY)
    conn.close()

    store = Store(path)
    Store(path)

    account = store.account("trakt")
    assert (account.access_token, account.activity_marker, account.needs_dry_run) == ("tok", "m", False)
    assert account.needs_history_dry_run is True
    assert account.history_marker is None
    assert store.remote_history("trakt") == {}


def test_old_runs_without_changes_are_pruned(tmp_path):
    store = Store(tmp_path / "test.db")
    title = store.resolve(RemoteItem("trakt", Kind.MOVIE, {"trakt": "1"}, "Heat", 1995))
    old_quiet, old_with_change, recent = (store.start_run(False) for _ in range(3))
    store.enqueue(old_with_change, [Op("trakt", Action.ADD, title)])
    for run in (old_quiet, old_with_change, recent):
        store.finish_run(run, "ok", "{}")
    with sqlite3.connect(tmp_path / "test.db") as c:
        c.execute("UPDATE sync_runs SET finished_at='2020-01-01T00:00:00+00:00' WHERE id IN (?,?)", (old_quiet, old_with_change))

    store.prune_runs()

    assert [r["id"] for r in store.runs()] == [recent, old_with_change]

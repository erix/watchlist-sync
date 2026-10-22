import sqlite3

from watchlist_sync.db import Store

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

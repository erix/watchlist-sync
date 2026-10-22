import sqlite3
import stat
from datetime import UTC, datetime

from watchlist_sync.backup import backup
from watchlist_sync.db import Store


def day(n):
    return datetime(2026, 10, n, 3, 30, tzinfo=UTC)


def test_backup_is_a_readable_private_copy(tmp_path):
    db = tmp_path / "watchlist.db"
    Store(db).connect("trakt", "secret-token", "refresh", "2099-01-01T00:00:00+00:00", None)
    dest = tmp_path / "backups"
    dest.mkdir()

    written = backup(db, dest, keep=14, now=day(22))

    assert written.name == "watchlist-2026-10-22.db"
    assert sqlite3.connect(written).execute("SELECT access_token FROM accounts WHERE service='trakt'").fetchone() == ("secret-token",)
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert [p.name for p in dest.iterdir()] == [written.name]


def test_only_the_newest_copies_are_kept_and_a_rerun_replaces_the_day(tmp_path):
    db = tmp_path / "watchlist.db"
    Store(db)
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "unrelated.txt").write_text("left alone")

    for n in range(1, 6):
        backup(db, dest, keep=3, now=day(n))
    backup(db, dest, keep=3, now=day(5))

    assert sorted(p.name for p in dest.iterdir()) == [
        "unrelated.txt",
        "watchlist-2026-10-03.db",
        "watchlist-2026-10-04.db",
        "watchlist-2026-10-05.db",
    ]

"""Copy the database into a backup directory and keep the newest copies.
The daily backup CronJob runs `python -m watchlist_sync.backup /backup`."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

PREFIX = "watchlist-"


def backup(db: Path, dest: Path, keep: int, now: datetime | None = None) -> Path:
    """Write a checked copy of `db` to `dest` and delete all but the newest
    `keep` copies. Running it twice on one day replaces that day's copy."""
    final = dest / f"{PREFIX}{(now or datetime.now(UTC)):%Y-%m-%d}.db"
    with tempfile.TemporaryDirectory() as work:
        # SQLite locking is unreliable over NFS, so the copy is made and checked
        # on local disk and only the finished file is sent to the share.
        local = Path(work) / final.name
        source, copy = sqlite3.connect(db), sqlite3.connect(local)
        try:
            source.backup(copy)
            # The live database runs in WAL mode; a backup should be one file.
            copy.execute("PRAGMA journal_mode = DELETE")
            check = copy.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            source.close()
            copy.close()
        if check != "ok":
            raise RuntimeError(f"backup failed integrity check: {check}")
        os.chmod(local, 0o600)  # holds the OAuth tokens
        partial = final.with_name(final.name + ".partial")
        shutil.copyfile(local, partial)
        os.chmod(partial, 0o600)
        partial.replace(final)
    for old in sorted(dest.glob(f"{PREFIX}*.db"))[:-keep]:
        old.unlink()
    return final


if __name__ == "__main__":
    data = Path(os.environ.get("WS_DATA_DIR", "/data"))
    written = backup(data / "watchlist.db", Path(sys.argv[1]), int(os.environ.get("WS_BACKUP_KEEP", "14")))
    print(f"wrote {written} ({written.stat().st_size} bytes)")

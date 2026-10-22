"""Whether syncing works, for an outside monitor. Separate from /healthz,
which only says the web server is up."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from .db import Account
from .domain import ServiceName

# One failed run is often a network blip that the next run fixes; report only
# when two runs in a row have passed without a successful sync.
MISSED_RUNS = 2
SLACK = timedelta(minutes=10)


def sync_problems(accounts: Mapping[ServiceName, Account], interval: timedelta, now: datetime) -> list[str]:
    problems = []
    for service, account in accounts.items():
        # A service still awaiting its first approval has never committed a sync.
        if not account.connected or account.needs_dry_run:
            continue
        if account.auth_error:
            problems.append(f"{service}: login needs renewing: {account.auth_error}")
            continue
        last = datetime.fromisoformat(account.last_sync_at) if account.last_sync_at else None
        if last is None or now - last > MISSED_RUNS * interval + SLACK:
            since = account.last_sync_at or "never"
            problems.append(f"{service}: no successful sync since {since}; last error: {account.last_error or 'none recorded'}")
    return problems

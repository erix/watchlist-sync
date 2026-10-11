from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    base_url: str
    interval_minutes: int
    dry_run: bool
    trakt_client_id: str | None
    trakt_client_secret: str | None
    simkl_client_id: str | None
    simkl_client_secret: str | None

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            data_dir=Path(env.get("WS_DATA_DIR", "/data")),
            base_url=env.get("WS_BASE_URL", "http://localhost:8080").rstrip("/"),
            interval_minutes=int(env.get("WS_SYNC_INTERVAL_MINUTES", "15")),
            dry_run=env.get("WS_DRY_RUN", "false").lower() in ("1", "true", "yes"),
            trakt_client_id=env.get("TRAKT_CLIENT_ID") or None,
            trakt_client_secret=env.get("TRAKT_CLIENT_SECRET") or None,
            simkl_client_id=env.get("SIMKL_CLIENT_ID") or None,
            simkl_client_secret=env.get("SIMKL_CLIENT_SECRET") or None,
        )

    def redirect_uri(self, service: str) -> str:
        return f"{self.base_url}/oauth/{service}/callback"

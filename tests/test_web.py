import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from watchlist_sync.config import Settings
from watchlist_sync.db import Store
from watchlist_sync.web.app import create_app


def client(tmp_path):
    settings = Settings(tmp_path, "http://test", 15, False, None, None, None, None)
    return TestClient(create_app(settings))


def test_sync_and_approve_buttons_start_background_jobs(tmp_path):
    with client(tmp_path) as c:
        assert c.post("/sync", data={"dry_run": "true"}, follow_redirects=False).status_code == 303
        assert c.post("/runs/1/approve", follow_redirects=False).status_code == 303


def test_run_page_shows_history_awaiting_approval(tmp_path):
    with client(tmp_path) as c:
        store = Store(tmp_path / "watchlist.db")
        store.connect("trakt", "access", "refresh", "2099-01-01T00:00:00+00:00", None)
        store.update_account("trakt", needs_dry_run=0)
        op = {"service": "simkl", "title_id": 1, "title": "Show", "year": 2020, "type": "show", "season": 1, "episode": 2,
              "watched_at": "2024-01-01T00:00:00Z"}
        history = {"dry_run": True, "adds": {"simkl": 600}, "skip_reasons": {"unwatched on trakt": 1},
                   "ops": [op] * 600, "skips": [{**op, "service": "trakt", "reason": "unwatched on trakt"}]}
        run_id = store.start_run(False)
        store.finish_run(run_id, "ok", json.dumps({"ops": [], "conflicts": [], "skips": [], "history": history}))

        page = c.get(f"/runs/{run_id}").text
        assert "History sync awaits approval" in page and "Approve and apply" in page
        assert "simkl 600" in page and "S01E02" in page and "100 more not shown" in page

        status = c.get("/").text
        assert "History sync awaits approval" in status and f"/runs/{run_id}" in status

        store.update_account("trakt", needs_history_dry_run=0)
        assert "Approve and apply" not in c.get(f"/runs/{run_id}").text
        assert "History sync is on" in c.get("/").text


def test_sync_health_reports_a_stalled_sync_and_a_lost_login(tmp_path):
    settings = Settings(tmp_path, "http://test", 15, False, "trakt-id", None, "simkl-id", None)
    with TestClient(create_app(settings)) as c:
        store = Store(tmp_path / "watchlist.db")
        assert c.get("/healthz/sync").json() == {"ok": True, "problems": []}

        recent = datetime.now(UTC).isoformat(timespec="seconds")
        stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
        for s in ("trakt", "simkl"):
            store.connect(s, "access", "refresh", "2099-01-01T00:00:00+00:00", None)
            store.update_account(s, needs_dry_run=0, last_sync_at=recent)
        assert c.get("/healthz/sync").status_code == 200

        store.update_account("simkl", last_sync_at=stale, last_sync_status="failed", last_error="HTTP 502")
        store.update_account("trakt", auth_error="refresh token revoked")
        response = c.get("/healthz/sync")
        assert response.status_code == 503
        assert response.json()["problems"] == [
            "trakt: login needs renewing: refresh token revoked",
            f"simkl: no successful sync since {stale}; last error: HTTP 502",
        ]


def test_one_failed_run_is_not_reported(tmp_path):
    settings = Settings(tmp_path, "http://test", 15, False, "trakt-id", None, None, None)
    with TestClient(create_app(settings)) as c:
        store = Store(tmp_path / "watchlist.db")
        store.connect("trakt", "access", "refresh", "2099-01-01T00:00:00+00:00", None)
        last_ok = (datetime.now(UTC) - timedelta(minutes=20)).isoformat(timespec="seconds")
        store.update_account("trakt", needs_dry_run=0, last_sync_at=last_ok, last_sync_status="failed", last_error="timeout")
        assert c.get("/healthz/sync").status_code == 200

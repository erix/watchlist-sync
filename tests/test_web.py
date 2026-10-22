import json

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

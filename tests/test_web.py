from fastapi.testclient import TestClient

from watchlist_sync.config import Settings
from watchlist_sync.web.app import create_app


def client(tmp_path):
    settings = Settings(tmp_path, "http://test", 15, False, None, None, None, None)
    return TestClient(create_app(settings))


def test_sync_and_approve_buttons_start_background_jobs(tmp_path):
    with client(tmp_path) as c:
        assert c.post("/sync", data={"dry_run": "true"}, follow_redirects=False).status_code == 303
        assert c.post("/runs/1/approve", follow_redirects=False).status_code == 303

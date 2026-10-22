"""Adapter parsing against response shapes from the Trakt and Simkl OpenAPI specs."""

import asyncio
import json

import httpx

from watchlist_sync.adapters.http import Pace
from watchlist_sync.adapters.simkl import SimklAdapter
from watchlist_sync.adapters.trakt import TraktAdapter
from watchlist_sync.domain import Kind, Title, WatchKey


class Tokens:
    def __init__(self):
        self.token = "old"
        self.refreshes = 0

    async def get(self):
        return self.token

    async def refresh_after_401(self, rejected):
        self.refreshes += 1
        self.token = "new"
        return self.token


def wire(adapter, handler):
    calls = []

    def record(request: httpx.Request):
        calls.append(request)
        return handler(request)

    adapter.http.pace = Pace(0, 0)
    adapter.http.client = httpx.AsyncClient(
        base_url=adapter.http.client.base_url,
        headers=adapter.http.client.headers,
        params=adapter.http.client.params,
        transport=httpx.MockTransport(record),
    )
    return calls


def title(id, kind, **ids):
    return Title(id, kind, False, None, None, ids)


def test_trakt_fetch_follows_pagination_and_reads_ids():
    adapter = TraktAdapter("cid", Tokens())

    def handler(request):
        page = int(request.url.params["page"])
        if request.url.path == "/sync/watchlist/movies/rank/asc":
            body = [{"type": "movie", "movie": {"title": f"M{page}", "year": 2000 + page, "ids": {"trakt": page, "slug": "x", "imdb": f"tt{page}", "tmdb": None}}}]
            return httpx.Response(200, json=body, headers={"X-Pagination-Page-Count": "2"})
        body = [{"type": "show", "show": {"title": "S", "year": 2022, "ids": {"trakt": 9, "tvdb": 423812, "imdb": None}}}]
        return httpx.Response(200, json=body, headers={"X-Pagination-Page-Count": "1"})

    calls = wire(adapter, handler)
    state = asyncio.run(adapter.fetch())

    assert [(i.title, i.kind, dict(i.ids)) for i in state.watchlist] == [
        ("M1", Kind.MOVIE, {"imdb": "tt1", "trakt": "1"}),
        ("M2", Kind.MOVIE, {"imdb": "tt2", "trakt": "2"}),
        ("S", Kind.SHOW, {"tvdb": "423812", "trakt": "9"}),
    ]
    assert calls[0].headers["trakt-api-key"] == "cid"
    assert calls[0].headers["trakt-api-version"] == "2"
    assert calls[0].headers["Authorization"] == "Bearer old"


def test_trakt_add_reports_not_found_items():
    adapter = TraktAdapter("cid", Tokens())
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "added": {"movies": 1, "shows": 0, "seasons": 0, "episodes": 0},
                "existing": {"movies": 0, "shows": 0, "seasons": 0, "episodes": 0},
                "not_found": {"movies": [], "shows": [{"ids": {"tvdb": 423812}}], "seasons": [], "episodes": []},
            },
        )

    wire(adapter, handler)
    result = asyncio.run(adapter.add([title(1, Kind.MOVIE, imdb="tt1"), title(2, Kind.SHOW, tvdb="423812", simkl="5")]))

    assert result.ok == {1} and result.not_found == {2}
    assert sent == {"movies": [{"ids": {"imdb": "tt1"}}], "shows": [{"ids": {"tvdb": 423812}}]}


def test_401_refreshes_once_and_retries():
    tokens = Tokens()
    adapter = TraktAdapter("cid", tokens)

    def handler(request):
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, json={"movies": {}, "shows": {}, "episodes": {}})

    wire(adapter, handler)
    asyncio.run(adapter.activity_marker())
    assert tokens.refreshes == 1


def test_429_waits_for_retry_after():
    adapter = TraktAdapter("cid", Tokens())
    responses = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json={"movies": {}, "shows": {}, "episodes": {}})]
    calls = wire(adapter, lambda r: responses.pop(0))
    asyncio.run(adapter.activity_marker())
    assert len(calls) == 2


SIMKL_LIBRARY = {
    "movies": [
        {"status": "plantowatch", "watched_episodes_count": 0, "last_watched_at": None,
         "movie": {"title": "Arrival", "year": 2016, "ids": {"simkl": 1, "imdb": "tt2543164", "tmdb": "329865"}}},
        {"status": "completed", "last_watched_at": "2024-01-01T00:00:00Z",
         "movie": {"title": "Heat", "year": 1995, "ids": {"simkl": 2, "imdb": "tt0113277"}}},
    ],
    "shows": [
        {"status": "plantowatch", "watched_episodes_count": 3,
         "show": {"title": "Chef's Table: Pizza", "year": 2022, "ids": {"simkl": 1979391}}},
    ],
    "anime": [
        {"status": "plantowatch", "anime_type": "movie", "watched_episodes_count": 0,
         "show": {"title": "Your Name.", "year": 2016, "ids": {"simkl": 3, "mal": "32281"}}},
        {"status": "notinteresting", "anime_type": "tv",
         "show": {"title": "Old", "year": 2001, "ids": {"simkl": 4}}},
    ],
}


def test_simkl_fetch_treats_plan_to_watch_as_the_watchlist():
    adapter = SimklAdapter("cid", Tokens())
    calls = wire(adapter, lambda r: httpx.Response(200, json=SIMKL_LIBRARY))
    state = asyncio.run(adapter.fetch())

    assert [(i.title, i.kind, i.anime, i.has_history) for i in state.watchlist] == [
        ("Arrival", Kind.MOVIE, False, False),
        ("Chef's Table: Pizza", Kind.SHOW, False, True),
        ("Your Name.", Kind.MOVIE, True, False),
    ]
    assert [(i.title, i.status) for i in state.elsewhere] == [("Heat", "completed"), ("Old", "dropped")]
    params = calls[0].url.params
    assert (params["client_id"], params["app-name"]) == ("cid", "watchlist-sync")


def test_simkl_writes_use_plan_to_watch_and_shows_for_anime_removal():
    adapter = SimklAdapter("cid", Tokens())
    bodies = []

    def handler(request):
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"not_found": {"movies": [], "shows": []}})

    wire(adapter, handler)
    anime = Title(3, Kind.MOVIE, True, None, None, {"mal": "32281"})
    asyncio.run(adapter.add([title(1, Kind.MOVIE, imdb="tt1"), anime]))
    asyncio.run(adapter.remove([anime]))

    assert bodies == [
        ("/sync/add-to-list", {"movies": [{"to": "plantowatch", "ids": {"imdb": "tt1"}}], "anime": [{"to": "plantowatch", "ids": {"mal": "32281"}}]}),
        ("/sync/history/remove", {"shows": [{"ids": {"mal": "32281"}}]}),
    ]


def test_simkl_lookup_keeps_the_id_it_searched_by():
    adapter = SimklAdapter("cid", Tokens())
    wire(adapter, lambda r: httpx.Response(200, json=[{"type": "tv", "title": "Chef's Table: Pizza", "year": 2022, "status": "ended", "ids": {"simkl": 1979391}}]))
    found = asyncio.run(adapter.lookup(title(1, Kind.SHOW, tvdb="423812")))
    assert dict(found[0].ids) == {"simkl": "1979391", "tvdb": "423812"}


def test_trakt_history_reads_every_page_of_plays():
    adapter = TraktAdapter("cid", Tokens())

    def handler(request):
        page = int(request.url.params["page"])
        if request.url.path == "/sync/history/movies":
            body = [{"id": page, "watched_at": f"2024-0{page}-01T00:00:00.000Z", "action": "watch", "type": "movie",
                     "movie": {"title": "Heat", "year": 1995, "ids": {"trakt": 7, "imdb": "tt0113277"}}}]
            return httpx.Response(200, json=body, headers={"X-Pagination-Page-Count": "2"})
        body = [{"id": 9, "watched_at": "2024-03-01T00:00:00.000Z", "action": "scrobble", "type": "episode",
                 "episode": {"season": 1, "number": 2, "title": "E2", "ids": {"trakt": 99}},
                 "show": {"title": "S", "year": 2022, "ids": {"trakt": 9, "tvdb": 423812}}}]
        return httpx.Response(200, json=body, headers={"X-Pagination-Page-Count": "1"})

    calls = wire(adapter, handler)
    state = asyncio.run(adapter.fetch_history())

    assert state.opaque == []
    assert [(w.item.title, w.item.kind, w.season, w.episode, w.watched_at) for w in state.watches] == [
        ("Heat", Kind.MOVIE, None, None, "2024-01-01T00:00:00.000Z"),
        ("Heat", Kind.MOVIE, None, None, "2024-02-01T00:00:00.000Z"),
        ("S", Kind.SHOW, 1, 2, "2024-03-01T00:00:00.000Z"),
    ]
    assert [(c.url.path, c.url.params["page"], c.url.params["limit"]) for c in calls] == [
        ("/sync/history/movies", "1", "1000"),
        ("/sync/history/movies", "2", "1000"),
        ("/sync/history/episodes", "1", "1000"),
    ]


def test_trakt_history_marker_shares_the_activities_call():
    adapter = TraktAdapter("cid", Tokens())
    activities = {"movies": {"watched_at": "m", "watchlisted_at": "w"}, "shows": {}, "episodes": {"watched_at": "e"}}
    calls = wire(adapter, lambda r: httpx.Response(200, json=activities))
    asyncio.run(adapter.activity_marker())
    assert json.loads(asyncio.run(adapter.history_marker())) == ["m", "e"]
    assert len(calls) == 1


def test_trakt_add_history_nests_episodes_and_reports_not_found():
    adapter = TraktAdapter("cid", Tokens())
    sent = []

    def handler(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"not_found": {"movies": [{"ids": {"imdb": "tt2"}}], "shows": [], "episodes": []}})

    wire(adapter, handler)
    show = title(3, Kind.SHOW, tvdb="423812", simkl="5")
    entries = [
        (title(1, Kind.MOVIE, imdb="tt1"), WatchKey(1), "2024-01-01T00:00:00Z"),
        (title(2, Kind.MOVIE, imdb="tt2"), WatchKey(2), "2024-01-02T00:00:00Z"),
        (show, WatchKey(3, 1, 1), "2024-01-03T00:00:00Z"),
        (show, WatchKey(3, 1, 2), "2024-01-04T00:00:00Z"),
        (show, WatchKey(3, 2, 1), "2024-01-05T00:00:00Z"),
    ]
    landed = asyncio.run(adapter.add_history(entries))

    assert landed == {WatchKey(1), WatchKey(3, 1, 1), WatchKey(3, 1, 2), WatchKey(3, 2, 1)}
    assert sent == [("/sync/history", {
        "movies": [{"ids": {"imdb": "tt1"}, "watched_at": "2024-01-01T00:00:00Z"},
                   {"ids": {"imdb": "tt2"}, "watched_at": "2024-01-02T00:00:00Z"}],
        "shows": [{"ids": {"tvdb": 423812}, "seasons": [
            {"number": 1, "episodes": [{"number": 1, "watched_at": "2024-01-03T00:00:00Z"},
                                       {"number": 2, "watched_at": "2024-01-04T00:00:00Z"}]},
            {"number": 2, "episodes": [{"number": 1, "watched_at": "2024-01-05T00:00:00Z"}]},
        ]}],
    })]


SIMKL_COMPLETED_MOVIES = {
    "movies": [
        {"status": "completed", "last_watched_at": "2024-01-01T00:00:00Z",
         "movie": {"title": "Heat", "year": 1995, "ids": {"simkl": 2, "imdb": "tt0113277"}}},
    ]
}
SIMKL_SHOWS = {
    "shows": [
        {"status": "watching", "last_watched_at": "2024-02-02T00:00:00Z", "watched_episodes_count": 2,
         "show": {"title": "Chef's Table: Pizza", "year": 2022, "ids": {"simkl": 1979391}},
         "seasons": [{"number": 1, "episodes": [{"number": 1, "watched_at": "2024-02-01T00:00:00Z"},
                                                {"number": 2, "watched_at": "2024-02-02T00:00:00Z"}]}]},
        {"status": "plantowatch", "watched_episodes_count": 0,
         "show": {"title": "Unseen", "year": 2020, "ids": {"simkl": 8}}},
        # Live Simkl lists no seasons for completed and dropped shows.
        {"status": "completed", "last_watched_at": "2023-05-01T00:00:00Z", "watched_episodes_count": 208,
         "show": {"title": "How I Met Your Mother", "year": 2005, "ids": {"simkl": 9, "tvdb": 75760}}},
    ]
}


def test_simkl_history_reads_completed_movies_and_watched_episodes():
    adapter = SimklAdapter("cid", Tokens())

    def handler(request):
        if request.url.path == "/sync/all-items/movies/completed":
            return httpx.Response(200, json=SIMKL_COMPLETED_MOVIES)
        return httpx.Response(200, json=SIMKL_SHOWS)

    calls = wire(adapter, handler)
    state = asyncio.run(adapter.fetch_history())

    assert [(w.item.title, w.item.kind, w.item.anime, w.season, w.episode, w.watched_at) for w in state.watches] == [
        ("Heat", Kind.MOVIE, False, None, None, "2024-01-01T00:00:00Z"),
        ("Chef's Table: Pizza", Kind.SHOW, False, 1, 1, "2024-02-01T00:00:00Z"),
        ("Chef's Table: Pizza", Kind.SHOW, False, 1, 2, "2024-02-02T00:00:00Z"),
    ]
    assert [(i.title, i.kind) for i in state.opaque] == [("How I Met Your Mother", Kind.SHOW)]
    assert calls[1].url.path == "/sync/all-items/shows/"
    assert (calls[1].url.params["extended"], calls[1].url.params["episode_watched_at"]) == ("full", "yes")


def test_simkl_history_marker_uses_the_all_timestamps():
    adapter = SimklAdapter("cid", Tokens())
    activities = {"all": "x", "movies": {"all": "m", "completed": "c"}, "tv_shows": {"all": "t"}, "anime": {"all": "a"}}
    calls = wire(adapter, lambda r: httpx.Response(200, json=activities))
    asyncio.run(adapter.activity_marker())
    assert json.loads(asyncio.run(adapter.history_marker())) == ["m", "t"]
    assert len(calls) == 1


def test_simkl_add_history_sends_watched_at_and_reports_not_found():
    adapter = SimklAdapter("cid", Tokens())
    sent = []

    def handler(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"added": {"movies": 0, "shows": 1}, "not_found": {"movies": [{"ids": {"imdb": "tt1"}}], "shows": []}})

    wire(adapter, handler)
    show = title(3, Kind.SHOW, simkl="1979391")
    landed = asyncio.run(adapter.add_history([
        (title(1, Kind.MOVIE, imdb="tt1"), WatchKey(1), "2024-01-01T00:00:00Z"),
        (show, WatchKey(3, 1, 1), "2024-02-01T00:00:00Z"),
    ]))

    assert landed == {WatchKey(3, 1, 1)}
    assert sent == [("/sync/history", {
        "movies": [{"ids": {"imdb": "tt1"}, "watched_at": "2024-01-01T00:00:00Z"}],
        "shows": [{"ids": {"simkl": 1979391}, "seasons": [
            {"number": 1, "episodes": [{"number": 1, "watched_at": "2024-02-01T00:00:00Z"}]}]}],
    })]

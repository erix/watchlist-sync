import asyncio
import json

import pytest

from tests.fakes import Crash, Entry, FakeService
from watchlist_sync.db import Store
from watchlist_sync.domain import Action, Kind
from watchlist_sync.engine import Engine

MOVIES = [
    ("Arrival", 2016, "tt2543164", "329865"),
    ("Dune", 2021, "tt1160419", "438631"),
    ("Heat", 1995, "tt0113277", "949"),
    ("Alien", 1979, "tt0078748", "348"),
]


def catalogs():
    trakt = [Entry(f"t{i}", Kind.MOVIE, name, year, {"imdb": imdb, "tmdb": tmdb}) for i, (name, year, imdb, tmdb) in enumerate(MOVIES)]
    simkl = [Entry(f"s{i}", Kind.MOVIE, name, year, {"imdb": imdb, "tmdb": tmdb}) for i, (name, year, imdb, tmdb) in enumerate(MOVIES)]
    return trakt, simkl


@pytest.fixture
def world(tmp_path):
    trakt_catalog, simkl_catalog = catalogs()
    trakt = FakeService("trakt", trakt_catalog)
    simkl = FakeService("simkl", simkl_catalog)
    store = Store(tmp_path / "test.db")
    for s in ("trakt", "simkl"):
        store.connect(s, "access", "refresh", "2099-01-01T00:00:00+00:00", None)
        store.update_account(s, needs_dry_run=0)
    return store, Engine(store, {"trakt": trakt, "simkl": simkl}), trakt, simkl


def sync(engine, dry_run=False):
    return asyncio.run(engine.sync(dry_run=dry_run))


def plan_of(store, run_id):
    return json.loads(store.run(run_id)["plan_json"])


def test_add_on_one_side_reaches_the_other(world):
    store, engine, trakt, simkl = world
    trakt.user_add("t0")
    sync(engine)
    assert simkl.watchlist == {"s0"}
    assert trakt.watchlist == {"t0"}


def test_removal_on_one_side_is_applied_to_the_other(world):
    store, engine, trakt, simkl = world
    trakt.user_add("t0")
    trakt.user_add("t1")
    sync(engine)
    simkl.user_remove("s1")
    sync(engine)
    assert trakt.watchlist == {"t0"}
    assert simkl.watchlist == {"s0"}


def test_never_added_is_not_treated_as_removed(world):
    store, engine, trakt, simkl = world
    trakt.user_add("t0")
    sync(engine)
    simkl.user_add("s2")
    sync(engine)
    assert trakt.watchlist == {"t0", "t2"}
    assert simkl.watchlist == {"s0", "s2"}


def test_add_remove_conflict_waits_for_the_user(world):
    store, engine, trakt, simkl = world
    simkl.user_move("s0", "completed")
    trakt.user_add("t0")
    sync(engine)
    assert simkl.watchlist == set()

    trakt.user_remove("t0")
    simkl.user_add("s0")
    run = sync(engine)

    conflicts = plan_of(store, run)["conflicts"]
    assert [(c["title"], c["added_on"], c["removed_on"]) for c in conflicts] == [("Arrival", ["simkl"], ["trakt"])]
    assert trakt.watchlist == set() and simkl.watchlist == {"s0"}

    sync(engine)
    assert trakt.watchlist == set() and simkl.watchlist == {"s0"}
    assert len(store.conflicts()) == 1

    store.resolve_conflict(conflicts[0]["title_id"], Action.ADD)
    sync(engine)
    assert trakt.watchlist == {"t0"} and simkl.watchlist == {"s0"}
    assert store.conflicts() == []


def test_the_apps_own_writes_are_not_read_back_as_changes(world):
    store, engine, trakt, simkl = world
    trakt.user_add("t0")
    trakt.user_add("t1")
    sync(engine)
    writes_after_first = list(simkl.writes) + list(trakt.writes)
    version_after_first = simkl.version

    run = sync(engine)

    assert simkl.version == version_after_first, "second run wrote to Simkl"
    assert plan_of(store, run)["ops"] == []
    assert list(simkl.writes) + list(trakt.writes) == writes_after_first


def test_crash_halfway_is_finished_without_duplicates(world):
    store, engine, trakt, simkl = world
    for n in ("t0", "t1", "t2", "t3"):
        trakt.user_add(n)
    simkl.crash_after = 2

    with pytest.raises(Crash):
        sync(engine)
    assert simkl.watchlist == {"s0", "s1"}

    simkl.crash_after = None
    sync(engine)

    assert simkl.watchlist == {"s0", "s1", "s2", "s3"}
    assert trakt.watchlist == {"t0", "t1", "t2", "t3"}
    assert sorted(simkl.writes) == [("add", "s0"), ("add", "s1"), ("add", "s2"), ("add", "s3")]
    assert trakt.writes == []
    done = [r for r in store.change_log() if r["state"] == "done"]
    assert sorted(r["title"] for r in done) == ["Alien", "Arrival", "Dune", "Heat"]


def chefs_table(tmp_path, trakt_extra=(), simkl_extra=()):
    """Chef's Table: Pizza is on Trakt with TVDB/TMDB/IMDb IDs and on Simkl as
    ID 1979391, which Simkl cannot match by any of those IDs."""
    trakt = FakeService(
        "trakt",
        [Entry("t-pizza", Kind.SHOW, "Chef's Table: Pizza", 2022, {"tvdb": "423812", "tmdb": "208589", "imdb": "tt21839470"}), *trakt_extra],
    )
    simkl = FakeService("simkl", [Entry("1979391", Kind.SHOW, "Chef's Table: Pizza", 2022, {}), *simkl_extra])
    store = Store(tmp_path / "chef.db")
    for s in ("trakt", "simkl"):
        store.connect(s, "access", "refresh", "2099-01-01T00:00:00+00:00", None)
        store.update_account(s, needs_dry_run=0)
    trakt.user_add("t-pizza")
    simkl.user_add("1979391")
    return store, Engine(store, {"trakt": trakt, "simkl": simkl}), trakt, simkl


def test_id_only_mismatch_is_linked_by_certain_title_match(tmp_path):
    store, engine, trakt, simkl = chefs_table(tmp_path)
    sync(engine)
    assert trakt.writes == [] and simkl.writes == []
    assert len(store.titles()) == 1
    assert store.unmatched() == []


def test_id_only_mismatch_without_certainty_is_never_guessed(tmp_path):
    store, engine, trakt, simkl = chefs_table(
        tmp_path,
        trakt_extra=[Entry("t-other", Kind.SHOW, "Chef's Table: Pizza", 2022, {"tvdb": "999"})],
        simkl_extra=[Entry("555", Kind.SHOW, "Chef's Table: Pizza", 2022, {"tvdb": "999"})],
    )
    sync(engine)
    assert trakt.writes == [] and simkl.writes == []
    assert {r["service"] for r in store.unmatched()} == {"trakt", "simkl"}

    # A manual match is stored and reused: link Simkl's item to the Trakt entry.
    simkl_title = next(r["title_id"] for r in store.unmatched() if r["service"] == "trakt")
    survivor = store.attach_ids(simkl_title, Kind.SHOW, {"trakt": "t-pizza"}, source="manual")
    for r in store.unmatched():
        store.clear_unmatched(r["service"], r["title_id"])
    sync(engine)
    sync(engine)
    assert trakt.writes == [] and simkl.writes == []
    assert list(store.titles()) == [survivor]


def test_first_sync_is_a_dry_run_until_approved(world):
    store, engine, trakt, simkl = world
    store.update_account("simkl", needs_dry_run=1)
    trakt.user_add("t0")
    simkl.user_add("s1")

    run = sync(engine)
    assert store.run(run)["status"] == "dry-run"
    assert trakt.writes == [] and simkl.writes == []
    planned = {(o["service"], o["action"], o["title"]) for o in plan_of(store, run)["ops"]}
    assert planned == {("simkl", "add", "Arrival"), ("trakt", "add", "Dune")}

    asyncio.run(engine.approve(run))
    assert trakt.watchlist == {"t0", "t1"} and simkl.watchlist == {"s0", "s1"}

    run = sync(engine)
    assert store.run(run)["status"] == "ok"
    assert plan_of(store, run)["ops"] == []


def test_explicit_dry_run_writes_nothing(world):
    store, engine, trakt, simkl = world
    trakt.user_add("t0")
    sync(engine, dry_run=True)
    assert simkl.watchlist == set()
    sync(engine)
    assert simkl.watchlist == {"s0"}


def test_removal_that_would_delete_history_is_held_back(world):
    store, engine, trakt, simkl = world
    trakt.user_add("t0")
    sync(engine)
    simkl.history.add("s0")
    simkl.version += 1
    trakt.user_remove("t0")
    run = sync(engine)
    assert simkl.watchlist == {"s0"}
    assert [s["service"] for s in plan_of(store, run)["skips"]] == ["simkl"]


def test_titles_in_another_state_are_not_moved(world):
    store, engine, trakt, simkl = world
    simkl.user_move("s0", "completed")
    trakt.user_add("t0")
    run = sync(engine)
    assert simkl.elsewhere == {"s0": "completed"}
    assert simkl.writes == []
    assert plan_of(store, run)["skips"][0]["reason"] == "already in 'completed' on simkl"


def test_leaving_the_watchlist_on_simkl_removes_it_on_trakt(world):
    store, engine, trakt, simkl = world
    trakt.user_add("t0")
    sync(engine)
    simkl.user_move("s0", "completed")
    sync(engine)
    assert trakt.watchlist == set()


def test_connecting_a_second_service_later_fills_it_without_removing_anything(world):
    store, engine, trakt, simkl = world
    trakt.user_add("t0")
    trakt.user_add("t1")
    asyncio.run(Engine(store, {"trakt": trakt}).sync())
    assert store.snapshot("trakt") and simkl.writes == []

    sync(engine)
    assert trakt.watchlist == {"t0", "t1"}
    assert simkl.watchlist == {"s0", "s1"}


JAN = "2024-01-01T20:00:00Z"
FEB = "2024-02-01T20:00:00Z"


def history_of(store, run_id):
    return plan_of(store, run_id)["history"]


def approve_history(store):
    for s in ("trakt", "simkl"):
        store.update_account(s, needs_history_dry_run=0)


def test_first_history_sync_is_a_dry_run_while_the_watchlist_still_syncs(world):
    store, engine, trakt, simkl = world
    trakt.user_watch("t0", JAN)
    simkl.user_watch("s1", FEB)
    trakt.user_add("t2")

    for _ in range(2):
        run = sync(engine)
        assert store.run(run)["status"] == "ok"
        assert history_of(store, run)["dry_run"] is True
        assert {(o["service"], o["title"], o["watched_at"]) for o in history_of(store, run)["ops"]} == {
            ("simkl", "Arrival", JAN),
            ("trakt", "Dune", FEB),
        }
        assert trakt.history_writes == [] and simkl.history_writes == []
        assert simkl.watchlist == {"s2"}


def test_approving_history_applies_exactly_the_shown_plays(world):
    store, engine, trakt, simkl = world
    trakt.user_watch("t0", JAN)
    simkl.user_watch("s1", FEB)
    run = sync(engine)
    trakt.user_watch("t3", FEB)

    approval = asyncio.run(engine.approve(run))

    assert simkl.history_writes == [("s0", None, None, JAN)]
    assert trakt.history_writes == [("t1", None, None, FEB)]
    assert not store.account("trakt").needs_history_dry_run
    assert history_of(store, approval)["dry_run"] is False

    run = sync(engine)
    assert history_of(store, run)["dry_run"] is False
    assert simkl.history_writes == [("s0", None, None, JAN), ("s3", None, None, FEB)]


def test_own_history_writes_are_not_read_back_and_unwatches_stick(world):
    store, engine, trakt, simkl = world
    approve_history(store)
    trakt.user_watch("t0", JAN)
    sync(engine)
    assert simkl.plays == {("s0", None, None): JAN}

    fetches = (trakt.history_fetches, simkl.history_fetches)
    sync(engine)
    run = sync(engine)
    assert history_of(store, run)["ops"] == []
    assert len(simkl.history_writes) == 1 and trakt.history_writes == []
    # Simkl's marker moved once, from the app's own write; nothing else is refetched.
    assert (trakt.history_fetches, simkl.history_fetches) == (fetches[0], fetches[1] + 1)

    simkl.user_unwatch("s0")
    for _ in range(3):
        run = sync(engine)
        assert history_of(store, run)["ops"] == []
    assert simkl.plays == {} and trakt.plays == {("t0", None, None): JAN}
    assert history_of(store, run)["skip_reasons"] == {"unwatched on simkl": 1}


def test_history_crash_halfway_resumes_without_duplicate_plays_and_keeps_the_watchlist(world):
    store, engine, trakt, simkl = world
    approve_history(store)
    for i, n in enumerate(("t0", "t1", "t2", "t3")):
        trakt.user_watch(n, f"2024-01-0{i + 1}T00:00:00Z")
    trakt.user_add("t0")
    simkl.history_crash_after = 2

    with pytest.raises(Crash):
        sync(engine)
    failed = store.runs(1)[0]
    assert failed["status"] == "failed"
    assert [o["title"] for o in json.loads(failed["plan_json"])["ops"]] == ["Arrival"]
    assert simkl.watchlist == {"s0"}
    assert {n for n, _, _ in simkl.plays} == {"s0", "s1"}

    simkl.history_crash_after = None
    run = sync(engine)

    assert plan_of(store, run)["ops"] == []
    assert sorted(n for n, _, _, _ in simkl.history_writes) == ["s0", "s1", "s2", "s3"]
    assert simkl.plays[("s3", None, None)] == "2024-01-04T00:00:00Z"


def test_anime_history_is_reported_and_not_written(tmp_path):
    trakt = FakeService("trakt", [Entry("t-frieren", Kind.SHOW, "Frieren", 2023, {"tvdb": "424536"})])
    simkl = FakeService("simkl", [Entry("s-frieren", Kind.SHOW, "Frieren", 2023, {"tvdb": "424536"}, anime=True)])
    store = Store(tmp_path / "anime.db")
    for s in ("trakt", "simkl"):
        store.connect(s, "access", "refresh", "2099-01-01T00:00:00+00:00", None)
        store.update_account(s, needs_dry_run=0, needs_history_dry_run=0)
    simkl.user_move("s-frieren", "watching")
    trakt.user_watch("t-frieren", JAN, season=1, episode=1)

    run = asyncio.run(Engine(store, {"trakt": trakt, "simkl": simkl}).sync())

    assert simkl.history_writes == []
    assert [(s["service"], s["season"], s["episode"]) for s in history_of(store, run)["skips"]] == [("simkl", 1, 1)]

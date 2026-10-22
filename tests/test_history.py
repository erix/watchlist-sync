from watchlist_sync.domain import HistoryOp, HistoryView, WatchKey
from watchlist_sync.history import ANIME, OPAQUE, SPECIALS, next_history_snapshot, plan_history

MOVIE = WatchKey(1)
EPISODE = WatchKey(2, 1, 3)


def anywhere(service, title_id):
    return True


def run(trakt, simkl, snapshots, anime=frozenset(), addressable=anywhere):
    """One sync: plan, apply every op to the fake live state, commit snapshots."""
    live = {"trakt": dict(trakt), "simkl": dict(simkl)}
    views = {s: HistoryView(live[s], snapshots.get(s, frozenset())) for s in live}
    result = plan_history(views, anime, addressable)
    for op in result.ops:
        live[op.service][op.key] = op.watched_at
    new = {s: next_history_snapshot(HistoryView(live[s], views[s].snapshot)) for s in live}
    return result, live, new


def test_play_on_one_service_is_added_to_the_other_with_the_latest_watched_at():
    views = {
        "trakt": HistoryView({MOVIE: "2024-01-01T00:00:00Z", EPISODE: "2024-02-01T00:00:00Z"}, frozenset()),
        "simkl": HistoryView({EPISODE: "2024-03-01T00:00:00Z"}, frozenset()),
        "other": HistoryView({}, frozenset()),
    }
    result = plan_history(views, set(), anywhere)
    assert set(result.ops) == {
        HistoryOp("simkl", MOVIE, "2024-01-01T00:00:00Z"),
        HistoryOp("other", MOVIE, "2024-01-01T00:00:00Z"),
        HistoryOp("other", EPISODE, "2024-03-01T00:00:00Z"),
    }
    assert result.skips == ()


def test_unwatched_play_is_not_added_back_on_later_runs():
    result, live, snaps = run({MOVIE: "2024-01-01T00:00:00Z"}, {}, {})
    assert [op.service for op in result.ops] == ["simkl"]

    del live["simkl"][MOVIE]
    for _ in range(3):
        result, live, snaps = run(live["trakt"], live["simkl"], snaps)
        assert result.ops == ()
        assert [(s.service, s.reason) for s in result.skips] == [("simkl", "unwatched on simkl")]
        assert MOVIE in live["trakt"] and MOVIE not in live["simkl"]


def test_anime_is_skipped():
    result = plan_history(
        {"trakt": HistoryView({EPISODE: "2024-01-01T00:00:00Z"}, frozenset()), "simkl": HistoryView({}, frozenset())},
        {EPISODE.title_id},
        anywhere,
    )
    assert result.ops == ()
    assert [(s.service, s.key, s.reason) for s in result.skips] == [("simkl", EPISODE, ANIME)]


def test_specials_are_skipped():
    special = WatchKey(2, 0, 1)
    result = plan_history(
        {"trakt": HistoryView({special: "2024-01-01T00:00:00Z"}, frozenset()), "simkl": HistoryView({}, frozenset())},
        set(),
        anywhere,
    )
    assert result.ops == ()
    assert [(s.service, s.key, s.reason) for s in result.skips] == [("simkl", special, SPECIALS)]


def test_title_without_an_id_the_service_accepts_is_skipped():
    result = plan_history(
        {"trakt": HistoryView({MOVIE: "2024-01-01T00:00:00Z"}, frozenset()), "simkl": HistoryView({}, frozenset())},
        set(),
        lambda service, title_id: service != "simkl",
    )
    assert result.ops == ()
    assert [s.reason for s in result.skips] == ["no ID simkl accepts"]


def test_play_the_service_rejected_is_not_resent():
    result = plan_history(
        {
            "trakt": HistoryView({MOVIE: "2024-01-01T00:00:00Z"}, frozenset()),
            "simkl": HistoryView({}, frozenset(), rejected=frozenset({MOVIE})),
        },
        set(),
        anywhere,
    )
    assert result.ops == ()
    assert [s.reason for s in result.skips] == ["simkl did not find it"]


def test_episodes_of_a_show_simkl_does_not_list_are_never_added_there():
    result = plan_history(
        {
            "trakt": HistoryView({MOVIE: "2024-01-01T00:00:00Z", EPISODE: "2024-01-02T00:00:00Z"}, frozenset()),
            "simkl": HistoryView({}, frozenset(), opaque=frozenset({EPISODE.title_id})),
        },
        set(),
        anywhere,
    )
    assert [(o.service, o.key) for o in result.ops] == [("simkl", MOVIE)]
    assert [(s.service, s.key, s.reason) for s in result.skips] == [("simkl", EPISODE, OPAQUE)]


def test_show_moving_to_an_unlisted_status_adds_nothing_anywhere():
    snapshots = {"trakt": frozenset({EPISODE}), "simkl": frozenset({EPISODE, WatchKey(2, 1, 4)})}
    views = {
        "trakt": HistoryView({EPISODE: "2024-01-01T00:00:00Z"}, snapshots["trakt"]),
        "simkl": HistoryView({}, snapshots["simkl"], opaque=frozenset({EPISODE.title_id})),
    }
    assert plan_history(views, set(), anywhere).ops == ()


def test_nothing_to_do_when_both_services_agree():
    both = {MOVIE: "2024-01-01T00:00:00Z", EPISODE: "2024-01-02T00:00:00Z"}
    result, _, snaps = run(both, {MOVIE: "2023-12-31T00:00:00Z", EPISODE: "2024-01-02T00:00:00Z"}, {})
    assert result.ops == () and result.skips == ()
    assert snaps == {"trakt": frozenset(both), "simkl": frozenset(both)}

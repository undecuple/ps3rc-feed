"""Catalog view over the observation store, plus null-aware sorting.

Unknown values are null, never 0, and sort after known values. Metrics of
a stale or partial entry are the values preserved from its last successful
refresh, flagged as not complete; they are never recomputed against a
later run time.
"""

from datetime import datetime, timedelta, timezone

from . import text

THIRTY_DAYS = timedelta(days=30)


def parse_timestamp(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def nulls_last(value):
    """Sort key: known values in natural order, None after all of them."""
    if value is None:
        return (1, 0)
    return (0, value)


def nulls_last_desc(value):
    """Sort key for descending order of known values, None still last."""
    if value is None:
        return (1, 0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, -value)
    # Strings and timestamps: fall back to ascending with None last.
    return (0, value)


def sort_entries(entries, field, descending=False):
    key = nulls_last_desc if descending else nulls_last
    return sorted(entries, key=lambda e: key(e.get(field)))


def commits_in_window(obs):
    """Observed commits inside the 30 days before the last successful refresh.

    Returns (count, complete). count is None when nothing is known (never a
    successful commit listing). complete is False for a stale or partial
    entry, an incomplete listing or a suspected history rewrite.
    """
    reference = obs["last_successful_refresh_at"]
    if reference is None or not obs["commits_baseline_done"]:
        return None, None
    now = parse_timestamp(reference)
    count = 0
    for info in obs["observed_commits"].values():
        if now - THIRTY_DAYS <= parse_timestamp(info["committed_at"]) <= now:
            count += 1
    complete = (
        obs["commits_complete"] is True
        and not obs["history_rewrite_suspected"]
        and not obs["stale"]
        and not obs["partial"]
    )
    return count, complete


def build_entry(state, obs):
    commits_30d, commits_30d_complete = commits_in_window(obs)
    parent = state["repositories"].get(str(obs["parent_repository_id"])) if obs["parent_repository_id"] is not None else None
    root = (
        state["repositories"].get(str(obs["network_root_repository_id"]))
        if obs["network_root_repository_id"] is not None
        else None
    )
    return {
        "id": obs["repository_id"],
        "repo": obs["full_name"],
        "owner": obs["owner"],
        "source_url": text.repository_url(obs["full_name"]),
        "description": text.escape_text(obs["description"]),
        "game": text.escape_text(obs["game"]["title"], text.MAX_TITLE),
        "game_key": obs["game"]["key"],
        "game_inferred": True,
        "game_verified": False,
        "game_inherited": False,
        "game_confidence": obs["game"]["confidence"],
        "game_basis": obs["game"]["basis"],
        "category": obs["category"],
        "toolkit": obs["toolkit"]["name"],
        "fork": obs["is_fork"],
        "parent": parent["full_name"] if parent else None,
        "parent_repository_id": obs["parent_repository_id"],
        "root": root["full_name"] if root else None,
        "root_repository_id": obs["network_root_repository_id"],
        "ahead": None,
        "behind": None,
        "comparison_state": None,
        "stars": obs["stars"],
        "commits_30d": commits_30d,
        "commits_30d_complete": commits_30d_complete,
        "commits_complete": obs["commits_complete"],
        "history_rewrite_suspected": obs["history_rewrite_suspected"],
        "first_seen_at": obs["first_seen_at"],
        "last_commit_at": obs["tip_committed_at"],
        "archived": obs["archived"],
        "stale": obs["stale"],
        "partial": obs["partial"],
        "collection_failing": obs["consecutive_failures"] > 0,
        "collection_state": obs["collection_state"],
        "last_successful_refresh_at": obs["last_successful_refresh_at"],
        "last_attempted_refresh_at": obs["last_attempted_refresh_at"],
        "previous_names": [h["full_name"] for h in obs["name_history"]],
        "discovery_sources": [text.escape_text(s) for s in obs["discovery_sources"]],
    }


def build_catalog(state):
    entries = [build_entry(state, obs) for obs in state["repositories"].values()]
    entries.sort(key=lambda e: e["id"])
    return entries


def incomplete_work(collection):
    """Every stored search or root scan that is still recorded as incomplete."""
    searches = sorted(q for q, s in collection["searches"].items() if not s["complete"])
    roots = sorted(int(k) for k, r in collection["roots"].items() if not r["forks_scan_complete"])
    return searches, roots


def collection_status(state):
    col = state["collection"]
    repos = state["repositories"].values()
    searches, roots = incomplete_work(col)
    return {
        "last_run_at": state["last_run_at"],
        "baseline_done": state["baseline_done"],
        "baseline_at": state["baseline_at"],
        "repositories": len(state["repositories"]),
        "incomplete": bool(searches or roots),
        "incomplete_searches": searches,
        "incomplete_roots": roots,
        "failing": sorted(o["repository_id"] for o in repos if o["consecutive_failures"] > 0),
        "stale": sorted(o["repository_id"] for o in repos if o["stale"]),
        "partial": sorted(o["repository_id"] for o in repos if o["partial"]),
        "not_found": sorted(o["repository_id"] for o in repos if o["collection_state"] == "not_found"),
        "partial_commits": sorted(o["repository_id"] for o in repos if o["commits_complete"] is False),
        "rewrite_suspected": sorted(o["repository_id"] for o in repos if o["history_rewrite_suspected"]),
    }

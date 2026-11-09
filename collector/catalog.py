"""Catalog view over the observation store, plus null-aware sorting.

Unknown values are null, never 0, and sort after known values.
"""

from datetime import datetime, timedelta, timezone

from . import text

THIRTY_DAYS = timedelta(days=30)


def _parse(ts):
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


def commits_in_window(obs, now_ts):
    """Observed commits with committed_at inside the 30 days before now_ts.

    Returns (count, complete). count is None when nothing is known.
    """
    if obs["commits_complete"] is None and not obs["observed_commits"]:
        return None, None
    now = _parse(now_ts)
    count = 0
    for info in obs["observed_commits"].values():
        if now - THIRTY_DAYS <= _parse(info["committed_at"]) <= now:
            count += 1
    return count, bool(obs["commits_complete"]) and not obs["history_rewrite_suspected"]


def build_entry(state, obs):
    now_ts = state["last_run_at"] or obs["last_attempted_refresh_at"]
    commits_30d, commits_30d_complete = commits_in_window(obs, now_ts)
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


def collection_status(state):
    col = state["collection"]
    repos = state["repositories"].values()
    return {
        "last_run_at": state["last_run_at"],
        "baseline_at": state["baseline_at"],
        "repositories": len(state["repositories"]),
        "incomplete": col["incomplete"],
        "incomplete_searches": list(col["incomplete_searches"]),
        "incomplete_roots": list(col["incomplete_roots"]),
        "failing": sorted(o["repository_id"] for o in repos if o["consecutive_failures"] > 0),
        "stale": sorted(o["repository_id"] for o in repos if o["stale"]),
        "not_found": sorted(o["repository_id"] for o in repos if o["collection_state"] == "not_found"),
        "partial_commits": sorted(o["repository_id"] for o in repos if o["commits_complete"] is False),
    }

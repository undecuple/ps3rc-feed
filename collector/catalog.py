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
        "game_inherited": obs["game"]["basis"] in ("inherited_from_parent", "inherited_unverified_copy"),
        "game_confidence": obs["game"]["confidence"],
        "game_basis": obs["game"]["basis"],
        "matched_alias": obs["game"].get("matched_alias"),
        "category": obs["category"],
        "toolkit": obs["toolkit"]["name"],
        "fork": obs["is_fork"],
        "parent": parent["full_name"] if parent else None,
        "parent_repository_id": obs["parent_repository_id"],
        "root": root["full_name"] if root else None,
        "root_repository_id": obs["network_root_repository_id"],
        "ahead": obs["comparison"]["ahead"],
        "behind": obs["comparison"]["behind"],
        "comparison_state": obs["comparison"]["state"],
        "comparison_stale": obs["comparison_status"]["stale"],
        # The three relationships, kept apart (spec 03).
        "ancestry": {
            "is_fork": obs["is_fork"],
            "parent_repository_id": obs["parent_repository_id"],
            "parent": parent["full_name"] if parent else None,
            "network_root_repository_id": obs["network_root_repository_id"],
            "root": root["full_name"] if root else None,
            "state": obs["ancestry_state"],
            "fork_discovered": obs["fork_discovered_done"],
        },
        "ancestry_history": [dict(h) for h in obs["ancestry_history"]],
        "game_association": {
            "key": obs["game"]["key"],
            "title": text.escape_text(obs["game"]["title"], text.MAX_TITLE),
            "basis": obs["game"]["basis"],
            "inherited_from": obs["game"]["inherited_from"],
            "confidence": obs["game"]["confidence"],
            "matched_alias": obs["game"].get("matched_alias"),
            "inferred": True,
            "verified": False,
        },
        "toolkit_detection": {
            "name": obs["toolkit"]["name"],
            "basis": obs["toolkit"]["basis"],
            "inherited_from": obs["toolkit"]["inherited_from"],
            "confidence": obs["toolkit"]["confidence"],
        },
        "comparison": {
            "base_repository_id": obs["comparison"]["base_repository_id"],
            "base_ref": text.escape_text(obs["comparison"]["base_ref"], 128),
            "head_ref": text.escape_text(obs["comparison"]["head_ref"], 128),
            "base_sha": obs["comparison"]["base_sha"],
            "head_sha": obs["comparison"]["head_sha"],
            "ahead": obs["comparison"]["ahead"],
            "behind": obs["comparison"]["behind"],
            "state": obs["comparison"]["state"],
            "collected_at": obs["comparison"]["collected_at"],
            "future_dated": obs["comparison"]["future_dated"],
            "stale": obs["comparison_status"]["stale"],
            "last_reported_state": obs["comparison_status"]["last_reported_state"],
            "last_reported_at": obs["comparison_status"]["last_reported_at"],
            "history_entries": len(obs["comparison_history"]),
        },
        "discovery_roots": list(obs["discovery_roots"]),
        "discovery_queries": [text.escape_text(q) for q in obs["discovery_queries"]],
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
        # Gone from GitHub: not found for as many runs in a row as it takes to emit collection_stale.
        "removed": obs["collection_state"] == "not_found" and obs["stale_notified"],
        "refreshed_in_last_run": obs["last_attempted_refresh_at"] == state["last_run_at"],
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
    last = col.get("last_run") or {"last_run_at": None, "complete": True, "stopped": None, "not_fetched": 0, "error_count": 0}
    return {
        "last_run_at": state["last_run_at"],
        "baseline_done": state["baseline_done"],
        "baseline_at": state["baseline_at"],
        "repositories": len(state["repositories"]),
        # Incomplete when any stored scan is partial, or when the last run itself did not finish.
        "incomplete": bool(searches or roots) or not last["complete"],
        "last_run": {
            "run_at": last["last_run_at"],
            "complete": last["complete"],
            "stopped": last["stopped"],
            "not_fetched_count": last["not_fetched"],
            "error_count": last["error_count"],
        },
        # Known repositories the last run did not touch: their values age, they are not failing.
        "not_refreshed": sorted(
            o["repository_id"] for o in repos if state["last_run_at"] is not None and o["last_attempted_refresh_at"] != state["last_run_at"]
        ),
        "incomplete_searches": searches,
        "incomplete_roots": roots,
        "failing": sorted(o["repository_id"] for o in repos if o["consecutive_failures"] > 0),
        "stale": sorted(o["repository_id"] for o in repos if o["stale"]),
        "partial": sorted(o["repository_id"] for o in repos if o["partial"]),
        "not_found": sorted(o["repository_id"] for o in repos if o["collection_state"] == "not_found"),
        "partial_commits": sorted(o["repository_id"] for o in repos if o["commits_complete"] is False),
        "rewrite_suspected": sorted(o["repository_id"] for o in repos if o["history_rewrite_suspected"]),
        "partial_roots": roots,
        "root_scans": {
            key: {
                "scan_state": r["scan_state"],
                "resume_cursor": text.escape_text(r["resume_cursor"], 128),
                "found": len(r["found_repository_ids"]),
                "last_complete_at": r["last_complete_at"],
                "last_run_at": r["last_run_at"],
            }
            for key, r in sorted(col["roots"].items())
        },
        "pending_repository_ids": list(col["pending_repository_ids"]),
        "unresolved_ancestry": sorted(
            o["repository_id"] for o in repos if o["ancestry_state"] in ("unresolved", "inconsistent", "depth_exceeded")
        ),
        "rejected_fields": [
            {"path": text.escape_text(r["path"], 128), "reason": r["reason"]} for r in col["rejected_fields"]
        ],
        "comparison_stale": sorted(o["repository_id"] for o in repos if o["comparison_status"]["stale"]),
    }

"""Diff a run against the observation store and emit canonical events.

Rules (spec 02, "Event generation and noise controls", and the test plan):

- The first run ever is a baseline: observations are stored, no event.
- A repository seen for the first time later gets one repository_discovered
  event; its existing commits and releases are stored as its baseline.
- Commits are tracked per SHA. A run's unseen SHAs produce one grouped
  commits_observed event whose id is a digest of the sorted SHAs. Batch
  boundaries and overlap windows therefore cannot re-notify a SHA.
- A suspected history rewrite (previous tip no longer reachable and the
  window reaches back before it, or an explicit hint) records the observed
  SHAs conservatively: the event has no exact count and is marked
  incomplete.
- Incomplete pagination marks the count as a lower bound.
- repository_archived is emitted only on a known false -> true transition.
- A failed fetch keeps the previous known values and marks them stale. The
  failure counter drives one collection_stale event at the configured
  threshold and one collection_recovered event when a stale entry
  refreshes again.
- Every string from the run is stored verbatim as data; renderer-facing
  text is built from fixed templates with escaped values.
"""

from . import model, text
from .store import get_observation, put_observation


class Config:
    def __init__(self, stale_threshold=3):
        if not isinstance(stale_threshold, int) or stale_threshold < 1:
            raise ValueError("stale_threshold must be a positive integer")
        self.stale_threshold = stale_threshold


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _new_observation(repo, run_at, sources):
    category, game, toolkit = text.classify(repo["full_name"], repo["description"], repo["readme_excerpt"])
    return {
        "repository_id": repo["repository_id"],
        "full_name": repo["full_name"],
        "owner": repo["owner"],
        "name_history": [],
        "description": repo["description"],
        "readme_excerpt": repo["readme_excerpt"],
        "is_fork": repo["is_fork"],
        "parent_repository_id": repo["parent_repository_id"],
        "network_root_repository_id": repo["network_root_repository_id"],
        "archived": None,
        "stars": None,
        "default_branch": repo["default_branch"],
        "stale": False,
        "fetch_state": repo["fetch_state"],
        "collection_state": "ok",
        "consecutive_failures": 0,
        "stale_notified": False,
        "first_seen_at": run_at,
        "last_attempted_refresh_at": run_at,
        "last_successful_refresh_at": None,
        "discovery_sources": list(sources),
        "commits_complete": None,
        "tip_sha": None,
        "tip_committed_at": None,
        "history_rewrite_suspected": False,
        "observed_commits": {},
        "releases": {},
        "category": category,
        "game": game,
        "toolkit": toolkit,
        "event_counters": {},
    }


def _bump(obs, kind):
    obs["event_counters"][kind] = obs["event_counters"].get(kind, 0) + 1
    return obs["event_counters"][kind]


def _record_commits(obs, repo, run_at):
    """Store the run's commits per SHA. Returns (new_shas, rewrite_suspected)."""
    commits = repo["commits"]
    if commits is None:
        return [], False
    window = {c["sha"]: c for c in commits}
    new_shas = []
    for sha, commit in window.items():
        if sha not in obs["observed_commits"]:
            obs["observed_commits"][sha] = {
                "committed_at": commit["committed_at"],
                "message": commit["message"],
                "first_observed_at": run_at,
            }
            new_shas.append(sha)
    new_shas.sort()

    rewrite = False
    prev_tip = obs["tip_sha"]
    prev_tip_at = obs["tip_committed_at"]
    if repo.get("rewrite_hint") is not None:
        rewrite = True
    elif prev_tip is not None and prev_tip not in window and prev_tip_at is not None:
        # The window no longer reaches the old tip, yet contains unseen
        # commits dated at or before it: the old tip is probably gone.
        for sha in new_shas:
            if window[sha]["committed_at"] <= prev_tip_at:
                rewrite = True
                break

    if commits:
        tip = max(commits, key=lambda c: (c["committed_at"], c["sha"]))
        obs["tip_sha"] = tip["sha"]
        obs["tip_committed_at"] = tip["committed_at"]
    obs["commits_complete"] = repo["commits_complete"]
    if rewrite:
        obs["history_rewrite_suspected"] = True
    return new_shas, rewrite


def _record_releases(obs, repo, run_at):
    releases = repo["releases"]
    if releases is None:
        return []
    new = []
    for rel in sorted(releases, key=lambda r: r["id"]):
        key = str(rel["id"])
        if key not in obs["releases"]:
            obs["releases"][key] = {
                "tag": rel["tag"],
                "published_at": rel["published_at"],
                "first_observed_at": run_at,
            }
            new.append(rel)
    return new


def _commits_event(obs, run_at, new_shas, rewrite):
    source_at = max(obs["observed_commits"][s]["committed_at"] for s in new_shas)
    complete = bool(obs["commits_complete"]) and not rewrite
    if rewrite:
        summary = "default-branch commits observed after a possible history rewrite; exact delta unknown"
        count = None
        exact = False
    elif complete:
        summary = "%s previously unseen default-branch %s" % (len(new_shas), "commit" if len(new_shas) == 1 else "commits")
        count = len(new_shas)
        exact = True
    else:
        summary = "at least %s previously unseen default-branch %s (collection incomplete)" % (
            len(new_shas),
            "commit" if len(new_shas) == 1 else "commits",
        )
        count = len(new_shas)
        exact = False
    details = {
        "commit_shas": list(new_shas),
        "commit_count": count,
        "count_exact": exact,
        "complete": complete,
        "history_rewrite_suspected": rewrite,
        "commit_urls": [text.commit_url(obs["full_name"], s) for s in new_shas],
    }
    return model.make_event(
        model.COMMITS_OBSERVED,
        obs,
        run_at,
        source_at,
        summary,
        details,
        model.commits_event_id(obs["repository_id"], new_shas),
    )


def _release_event(obs, run_at, rel):
    tag = text.escape_text(rel["tag"], 128)
    details = {
        "release_id": rel["id"],
        "tag": tag,
        "published_at": rel["published_at"],
        "url": text.release_url(obs["full_name"], rel["tag"]),
    }
    return model.make_event(
        model.RELEASE_PUBLISHED,
        obs,
        run_at,
        rel["published_at"],
        "%s published" % tag,
        details,
        model.release_event_id(obs["repository_id"], rel["id"]),
    )


def _apply_success(state, obs, repo, run_at, config, emitted, is_new):
    """Update an observation from a successful fetch; append events."""
    # Recovery is announced only when a collection_stale was announced.
    was_stale = obs["stale_notified"]
    failures_before = obs["consecutive_failures"]
    prev_archived = obs["archived"]
    prev_game_key = obs["game"]["key"]

    if repo["full_name"] != obs["full_name"] or repo["owner"] != obs["owner"]:
        obs["name_history"].append({"full_name": obs["full_name"], "owner": obs["owner"], "until": run_at})
        obs["full_name"] = repo["full_name"]
        obs["owner"] = repo["owner"]
    obs["description"] = repo["description"]
    obs["readme_excerpt"] = repo["readme_excerpt"]
    obs["is_fork"] = repo["is_fork"]
    obs["parent_repository_id"] = repo["parent_repository_id"]
    obs["network_root_repository_id"] = repo["network_root_repository_id"]
    obs["default_branch"] = repo["default_branch"]
    if repo["archived"] is not None:
        obs["archived"] = repo["archived"]
    if repo["stars"] is not None:
        obs["stars"] = repo["stars"]
    obs["stale"] = False
    obs["fetch_state"] = "ok"
    obs["collection_state"] = "ok"
    obs["consecutive_failures"] = 0
    obs["last_successful_refresh_at"] = run_at

    category, game, toolkit = text.classify(obs["full_name"], obs["description"], obs["readme_excerpt"])
    obs["category"] = category
    obs["toolkit"] = toolkit
    prev_title = obs["game"]["title"]
    obs["game"] = game

    new_shas, rewrite = _record_commits(obs, repo, run_at)
    new_releases = _record_releases(obs, repo, run_at)

    if is_new:
        # Baseline for this repository: history stored, not announced.
        return

    if was_stale:
        obs["stale_notified"] = False
        ordinal = _bump(obs, "recovered")
        emitted.append(
            model.make_event(
                model.COLLECTION_RECOVERED,
                obs,
                run_at,
                None,
                "refresh succeeded again",
                {"consecutive_failures_before": failures_before},
                model.ordinal_event_id(obs["repository_id"], "recovered", ordinal),
            )
        )

    if new_shas:
        emitted.append(_commits_event(obs, run_at, new_shas, rewrite))
    for rel in new_releases:
        emitted.append(_release_event(obs, run_at, rel))

    if prev_archived is False and obs["archived"] is True:
        ordinal = _bump(obs, "archived")
        emitted.append(
            model.make_event(
                model.REPOSITORY_ARCHIVED,
                obs,
                run_at,
                None,
                "archived state changed to true",
                {"archived": True, "previous": False},
                model.ordinal_event_id(obs["repository_id"], "archived", ordinal),
            )
        )

    if obs["game"]["key"] != prev_game_key:
        ordinal = _bump(obs, "game_association")
        details = {
            "previous_key": prev_game_key,
            "new_key": obs["game"]["key"],
            "previous_title": text.escape_text(prev_title, text.MAX_TITLE),
            "new_title": text.escape_text(obs["game"]["title"], text.MAX_TITLE),
            "basis": obs["game"]["basis"],
            "confidence": obs["game"]["confidence"],
            "inferred": True,
            "verified": False,
        }
        emitted.append(
            model.make_event(
                model.GAME_ASSOCIATION_CHANGED,
                obs,
                run_at,
                None,
                "inferred game association changed (automated, unverified)",
                details,
                model.ordinal_event_id(obs["repository_id"], "game_association", ordinal),
            )
        )


def _apply_failure(state, obs, repo, run_at, config, emitted, is_new):
    """A fetch that did not succeed: keep known values, count the failure."""
    obs["fetch_state"] = repo["fetch_state"]
    obs["consecutive_failures"] += 1
    if obs["last_successful_refresh_at"] is not None:
        obs["stale"] = True
    if repo["fetch_state"] == "not_found":
        obs["collection_state"] = "not_found"
    else:
        obs["collection_state"] = "failing"
    if is_new:
        return
    if obs["consecutive_failures"] >= config.stale_threshold and not obs["stale_notified"]:
        obs["stale_notified"] = True
        obs["collection_state"] = "stale" if repo["fetch_state"] != "not_found" else "not_found"
        ordinal = _bump(obs, "stale")
        emitted.append(
            model.make_event(
                model.COLLECTION_STALE,
                obs,
                run_at,
                None,
                "refresh failed %s in a row" % _plural(obs["consecutive_failures"], "time"),
                {
                    "consecutive_failures": obs["consecutive_failures"],
                    "threshold": config.stale_threshold,
                    "fetch_state": repo["fetch_state"],
                },
                model.ordinal_event_id(obs["repository_id"], "stale", ordinal),
            )
        )
    elif obs["stale_notified"]:
        obs["collection_state"] = "stale" if repo["fetch_state"] != "not_found" else "not_found"


def _discovery_sources(run, repository_id):
    sources = []
    for search in run["searches"]:
        if repository_id in search["repository_ids"]:
            sources.append("search:%s" % search["query"])
    for root in run["roots"]:
        if root["repository_id"] == repository_id:
            sources.append("root:%d" % root["repository_id"])
    return sources


def _record_collection(state, run):
    col = state["collection"]
    run_at = run["run_at"]
    incomplete_searches = []
    for search in run["searches"]:
        col["searches"][search["query"]] = {
            "complete": search["complete"],
            "repository_ids": sorted(set(search["repository_ids"])),
            "last_run_at": run_at,
        }
        if not search["complete"]:
            incomplete_searches.append(search["query"])
    incomplete_roots = []
    for root in run["roots"]:
        col["roots"][str(root["repository_id"])] = {
            "full_name": root["full_name"],
            "forks_scan_complete": root["forks_scan_complete"],
            # Fork-network listing of the root; recorded only, step 2 uses it.
            "found_repository_ids": sorted(set(root.get("found_repository_ids") or [])),
            "last_run_at": run_at,
        }
        if not root["forks_scan_complete"]:
            incomplete_roots.append(root["repository_id"])
    col["incomplete_searches"] = sorted(incomplete_searches)
    col["incomplete_roots"] = sorted(incomplete_roots)
    col["incomplete"] = bool(incomplete_searches or incomplete_roots)


def apply_run(state, run, config=None):
    """Apply one validated run to the state. Returns the events it emitted.

    The run dict is treated as data only; the caller should have validated
    it with schema.validate_run (the dry-run CLI does). The events are also
    appended to state["events"].
    """
    from . import schema

    schema.validate_run(run)
    if config is None:
        config = Config()
    run_at = run["run_at"]
    baseline = state["baseline_at"] is None
    if baseline:
        state["baseline_at"] = run_at
    emitted = []

    for repo in sorted(run["repositories"], key=lambda r: r["repository_id"]):
        rid = repo["repository_id"]
        obs = get_observation(state, rid)
        is_new = obs is None
        if is_new:
            obs = _new_observation(repo, run_at, _discovery_sources(run, rid))
            put_observation(state, obs)
        obs["last_attempted_refresh_at"] = run_at
        for source in _discovery_sources(run, rid):
            if source not in obs["discovery_sources"]:
                obs["discovery_sources"].append(source)

        if repo["fetch_state"] == "ok":
            _apply_success(state, obs, repo, run_at, config, emitted, is_new)
        else:
            _apply_failure(state, obs, repo, run_at, config, emitted, is_new)

        if is_new and not baseline:
            details = {
                "discovery_sources": [text.escape_text(s) for s in obs["discovery_sources"]],
                "is_fork": obs["is_fork"],
                "parent_repository_id": obs["parent_repository_id"],
                "network_root_repository_id": obs["network_root_repository_id"],
                "fetch_state": obs["fetch_state"],
                "commits_in_baseline": len(obs["observed_commits"]),
                "releases_in_baseline": len(obs["releases"]),
            }
            source = obs["discovery_sources"][0] if obs["discovery_sources"] else "collector"
            emitted.insert(
                0,
                model.make_event(
                    model.REPOSITORY_DISCOVERED,
                    obs,
                    run_at,
                    None,
                    "first seen via %s" % text.escape_text(source, 120),
                    details,
                    model.discovered_event_id(rid),
                ),
            )

    _record_collection(state, run)
    state["last_run_at"] = run_at

    if baseline:
        # First run: observations only, no flood of historical activity.
        emitted = []

    # Deterministic order: by repository, then by event type, then id.
    emitted.sort(key=lambda e: (e["repository_id"], e["type"], e["event_id"]))
    for event in emitted:
        schema.validate_event(event)
    state["events"].extend(emitted)
    return emitted

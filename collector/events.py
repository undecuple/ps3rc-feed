"""Diff a run against the observation store and emit canonical events.

Rules (spec 02, "Event generation and noise controls", spec 03, the test
plan and the review findings):

- Runs apply in order: a run older than the last applied run is rejected.
  A run with the same timestamp is a replay and is idempotent.
- The first run that lists at least one repository is the global baseline:
  observations are stored, no event. Earlier empty runs only record
  collection status.
- A repository seen for the first time after the baseline gets one
  repository_discovered event. Its commit and release baselines are set by
  the first successful fetch that returns them, not by its first sighting.
- fork_discovered is emitted once ever per repository, when a fork
  relationship with a parent is first known; on the baseline run it is
  recorded silently. `related_event_id` links it to repository_discovered
  when both happen in one run.
- Commits are tracked per SHA. A run's unseen SHAs produce one grouped
  event whose id is a digest of the sorted SHAs. "Shared" means the SHA
  was already observed, before this run, in another repository of the same
  fork network (pre-run index, built once per run); a fork whose new SHAs
  are all shared gets fork_activity_observed, otherwise commits_observed.
  Two repositories receiving the same new SHA in one run both get
  commits_observed with `shared_in_run_count`; types never depend on the
  order repositories are processed in.
- History rewrite, archived, stale and recovered rules are unchanged from
  step 1.
- Game association: own evidence or inheritance is recomputed after every
  run. A direct change (a repository's own evidence moved) is one event per
  repository; changes caused only by inheritance are aggregated into one
  event per cause repository per run.
- Comparisons: the last comparable snapshot is kept when a later report is
  unavailable (marked stale); an older snapshot is ignored; a divergence
  event needs two comparable snapshots against the same base with different
  counts, and only for forks. A base change resets the baseline silently
  and is recorded in a bounded history.
- apply_run is atomic: it works on a copy, validates every new event and
  the whole state, and only then replaces the caller's state.
- Every string from the run is stored as bounded data; renderer-facing
  text is built from fixed templates with escaped values.
"""

import copy

from . import forks, games, model, schema, text
from .store import get_observation, put_observation

MAX_AFFECTED_LISTED = 100


class Config:
    """stale_threshold: failures before collection_stale. aliases: a games.Games (default config/games.json)."""

    def __init__(self, stale_threshold=3, aliases=None):
        if not isinstance(stale_threshold, int) or isinstance(stale_threshold, bool) or stale_threshold < 1:
            raise ValueError("stale_threshold must be a positive integer")
        self.stale_threshold = stale_threshold
        self.aliases = aliases if aliases is not None else games.default()


class RunOrderError(ValueError):
    """The run is older than the last run already applied."""


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _new_observation(repo, run_at):
    # Category, game and toolkit are filled by the association post-pass.
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
        "partial": False,
        "fetch_state": repo["fetch_state"],
        "collection_state": "ok",
        "consecutive_failures": 0,
        "stale_notified": False,
        "first_seen_at": run_at,
        "last_attempted_refresh_at": run_at,
        "last_successful_refresh_at": None,
        "discovery_sources": [],
        "commits_baseline_done": False,
        "releases_baseline_done": False,
        "commits_complete": None,
        "tip_sha": None,
        "tip_committed_at": None,
        "history_rewrite_suspected": False,
        "observed_commits": {},
        "releases": {},
        "category": "unclassified",
        "game": forks.unknown_game(),
        "toolkit": forks.unknown_toolkit(),
        "ancestry_state": "unknown",
        "ancestry_history": [],
        "fork_discovered_done": False,
        "first_sight": None,
        "discovery_roots": [],
        "discovery_queries": [],
        "comparison": forks.empty_comparison(),
        "comparison_status": forks.empty_comparison_status(),
        "comparison_history": [],
    }


# --- commits and releases ----------------------------------------------------


def _record_commits(obs, repo, run_at):
    """Store the run's commits per SHA.

    Returns (new_shas, rewrite, uncertain, future_dated): rewrite when the
    previous head is missing from a complete listing (or the fetch layer
    says so), uncertain when it is missing from an incomplete one.
    """
    commits = repo["commits"]
    window = {c["sha"]: c for c in commits}
    complete = repo["commits_complete"] is True
    prev_tip = obs["tip_sha"]

    new_shas = []
    future_dated = False
    for sha, commit in window.items():
        if commit["committed_at"] > run_at:
            future_dated = True
        if sha not in obs["observed_commits"]:
            obs["observed_commits"][sha] = {
                "committed_at": commit["committed_at"],
                "message": commit["message"],
                "first_observed_at": run_at,
            }
            new_shas.append(sha)
    new_shas.sort()

    rewrite = False
    uncertain = False
    if repo.get("rewrite_hint") is not None:
        rewrite = True
    elif prev_tip is not None and prev_tip not in window:
        if complete:
            rewrite = True
        else:
            uncertain = True

    if rewrite:
        obs["history_rewrite_suspected"] = True
    elif complete and prev_tip is not None and prev_tip in window:
        # A complete listing that still reaches the previous head: clean.
        obs["history_rewrite_suspected"] = False

    if commits:
        head = repo.get("head_sha") or commits[-1]["sha"]
        obs["tip_sha"] = head
        obs["tip_committed_at"] = min(window[head]["committed_at"], run_at)
    obs["commits_complete"] = repo["commits_complete"]
    return new_shas, rewrite, uncertain, future_dated


def _record_releases(obs, repo, run_at):
    new = []
    for rel in sorted(repo["releases"], key=lambda r: r["id"]):
        key = str(rel["id"])
        if key not in obs["releases"]:
            obs["releases"][key] = {
                "tag": rel["tag"],
                "published_at": rel["published_at"],
                "first_observed_at": run_at,
            }
            new.append(rel)
    return new


class RunIndex:
    """Pre-run snapshot used for order-independent shared-SHA labelling.

    `networks` maps repository id -> network key as it stood before the run;
    `shas` maps sha -> set of network keys that had observed it before the
    run. Both are built once per run; memory is bounded by the store size.
    """

    def __init__(self, repositories):
        self.networks = {}
        self.shas = {}
        for rid, obs in repositories.items():
            key = forks.network_key(obs, repositories)
            self.networks[int(rid)] = key
            for sha in obs["observed_commits"]:
                self.shas.setdefault(sha, set()).add(key)

    def shared_before_run(self, rid, shas):
        """SHAs already observed before the run in this repository's own network."""
        # A repository first seen this run has no network key here, and no
        # SHA set contains None: nothing can have been shared with it before.
        key = self.networks.get(rid)
        return sorted(s for s in shas if key in self.shas.get(s, ()))


def _commits_event(index, obs, run_at, new_shas, rewrite, uncertain, future_dated):
    """One grouped event for a repository's unseen SHAs.

    Observations stay per repository: a SHA shared with another repository
    in the fork network is still recorded and reported here. When a fork's
    new SHAs were all already observed in its network before this run, the
    event is fork_activity_observed (inherited activity); otherwise
    commits_observed.
    """
    source_at = max(obs["observed_commits"][s]["committed_at"] for s in new_shas)
    if source_at > run_at:
        source_at = run_at
    exact = obs["commits_complete"] is True and not rewrite and not uncertain
    n = len(new_shas)
    shared = index.shared_before_run(obs["repository_id"], new_shas)
    inherited = obs["is_fork"] is True and len(shared) == n
    event_type = model.FORK_ACTIVITY_OBSERVED if inherited else model.COMMITS_OBSERVED
    word = "commit" if n == 1 else "commits"
    if rewrite:
        summary = "default-branch commits observed after a possible history rewrite; exact delta unknown"
        count = None
    elif inherited:
        summary = "%d %s observed on the tracked branch, all already observed elsewhere in the network" % (n, word)
        count = n
    elif exact:
        summary = "%d previously unseen default-branch %s" % (n, word)
        count = n
    else:
        summary = "at least %d previously unseen default-branch %s (collection incomplete)" % (n, word)
        count = n
    if inherited and not exact:
        summary += " (collection incomplete)"
    listed = new_shas[: model.MAX_EVENT_SHAS]
    details = {
        "commit_shas": list(listed),
        "commit_shas_truncated": len(listed) < n,
        "commit_count": count,
        "count_exact": exact,
        "complete": exact,
        "history_rewrite_suspected": rewrite,
        "future_dated": future_dated,
        "is_fork": obs["is_fork"],
        "shared_with_network_count": len(shared),
        "shared_shas": shared[: model.MAX_EVENT_SHAS],
        "shared_in_run_count": 0,
        "commit_urls": [text.commit_url(obs["full_name"], s) for s in listed],
    }
    return model.make_event(
        event_type,
        obs,
        run_at,
        source_at,
        summary,
        details,
        model.commits_event_id(obs["repository_id"], new_shas),
    )


def _count_shared_in_run(emitted, run_new, repositories):
    """Fill shared_in_run_count: new SHAs that another same-network repository also received this run.

    One pass over the run's new SHAs: sha -> {network key: repositories that
    listed it}; a SHA is shared in the run when its own network counts two.
    """
    if len(run_new) < 2:
        return
    networks = {rid: forks.network_key(repositories[str(rid)], repositories) for rid in run_new}
    per_sha = {}
    for rid, shas in run_new.items():
        key = networks[rid]
        for sha in shas:
            counts = per_sha.setdefault(sha, {})
            counts[key] = counts.get(key, 0) + 1
    for event in emitted:
        if event["type"] not in (model.COMMITS_OBSERVED, model.FORK_ACTIVITY_OBSERVED):
            continue
        rid = event["repository_id"]
        if rid not in run_new:
            continue
        key = networks[rid]
        event["details"]["shared_in_run_count"] = sum(1 for sha in run_new[rid] if per_sha[sha].get(key, 0) >= 2)


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
        min(rel["published_at"], run_at),
        "%s published" % tag,
        details,
        model.release_event_id(obs["repository_id"], rel["id"]),
    )


# --- comparisons ------------------------------------------------------------


def _merge_comparison(obs, repo, run_at, emitted):
    """Store the reported comparison; announce a change between comparable snapshots of the same base."""
    reported = repo.get("comparison")
    if reported is None:
        return
    current = forks.normalise_comparison(reported)
    status = obs["comparison_status"]
    status["last_reported_state"] = current["state"]
    status["last_reported_at"] = run_at
    previous = obs["comparison"]
    if current["state"] not in forks.COMPARABLE:
        # Keep the last comparable snapshot, mark it stale.
        status["stale"] = previous["state"] in forks.COMPARABLE
        return
    if previous["state"] in forks.COMPARABLE and previous["collected_at"] > current["collected_at"]:
        # Older than what is stored: ignore.
        return
    status["stale"] = False
    if previous["state"] in forks.COMPARABLE and not forks.same_base(previous, current):
        forks.push_history(obs["comparison_history"], dict(previous, replaced_at=run_at))
    changed = forks.divergence_changed(previous, current)
    obs["comparison"] = current
    if not changed or obs["is_fork"] is not True:
        return
    details = {
        "ahead": current["ahead"],
        "behind": current["behind"],
        "previous_ahead": previous["ahead"],
        "previous_behind": previous["behind"],
        "state": current["state"],
        "base_repository_id": current["base_repository_id"],
        "base_ref": text.escape_text(current["base_ref"], 128),
        "head_ref": text.escape_text(current["head_ref"], 128),
        "base_sha": current["base_sha"],
        "head_sha": current["head_sha"],
        "collected_at": current["collected_at"],
        "future_dated": current["future_dated"],
    }
    emitted.append(
        model.make_event(
            model.FORK_DIVERGENCE_CHANGED,
            obs,
            run_at,
            current["collected_at"],
            "ahead %d / behind %d against the recorded base ref%s"
            % (current["ahead"], current["behind"], "" if current["state"] == "complete" else " (partial comparison)"),
            details,
            model.transition_event_id(obs["repository_id"], "divergence", run_at),
        )
    )


# --- success and failure paths ------------------------------------------------

_PRESERVED_FIELDS = (
    "description",
    "readme_excerpt",
    "archived",
    "stars",
    "default_branch",
)


def _merge_ancestry(obs, repo, run_at):
    """Update is_fork/parent/root from a successful fetch. Returns True if anything was unknown.

    null parent/root ids are a real answer for a non-fork; for a fork, or
    when is_fork itself is null, they mean "not fetched" and the previous
    values are kept. A change is appended to the bounded ancestry history.
    """
    if repo["is_fork"] is None:
        return True
    before = (obs["is_fork"], obs["parent_repository_id"], obs["network_root_repository_id"])
    obs["is_fork"] = repo["is_fork"]
    partial = False
    if repo["is_fork"] is False:
        obs["parent_repository_id"] = repo["parent_repository_id"]
        obs["network_root_repository_id"] = repo["network_root_repository_id"]
    else:
        for field in ("parent_repository_id", "network_root_repository_id"):
            if repo[field] is None:
                partial = partial or obs[field] is not None
            else:
                obs[field] = repo[field]
    after = (obs["is_fork"], obs["parent_repository_id"], obs["network_root_repository_id"])
    if before != after and before[0] is not None:
        forks.push_history(
            obs["ancestry_history"],
            {"is_fork": before[0], "parent_repository_id": before[1], "network_root_repository_id": before[2], "until": run_at},
        )
    return partial


def _apply_success(index, obs, repo, run_at, config, emitted):
    """Update an observation from a fetch with state ok; append events. Returns the new SHAs."""
    was_notified = obs["stale_notified"]
    failures_before = obs["consecutive_failures"]
    prev_archived = obs["archived"]

    if repo["full_name"] != obs["full_name"] or repo["owner"] != obs["owner"]:
        obs["name_history"].append({"full_name": obs["full_name"], "owner": obs["owner"], "until": run_at})
        obs["full_name"] = repo["full_name"]
        obs["owner"] = repo["owner"]

    partial = _merge_ancestry(obs, repo, run_at)
    for field in _PRESERVED_FIELDS:
        if repo[field] is None:
            partial = True
        else:
            obs[field] = repo[field]
    refreshed = repo["commits"] is not None or repo["releases"] is not None
    if repo["commits"] is None or repo["releases"] is None:
        partial = True
    obs["partial"] = partial
    obs["fetch_state"] = "ok"

    if refreshed:
        obs["stale"] = False
        obs["consecutive_failures"] = 0
        obs["last_successful_refresh_at"] = run_at
        obs["collection_state"] = "partial" if partial else "ok"
    elif obs["collection_state"] == "ok":
        obs["collection_state"] = "partial"

    new_shas = []
    rewrite = uncertain = future_dated = False
    if repo["commits"] is not None:
        if obs["commits_baseline_done"]:
            new_shas, rewrite, uncertain, future_dated = _record_commits(obs, repo, run_at)
        else:
            _record_commits(obs, repo, run_at)
            obs["history_rewrite_suspected"] = False
            obs["commits_baseline_done"] = True
    new_releases = []
    if repo["releases"] is not None:
        if obs["releases_baseline_done"]:
            new_releases = _record_releases(obs, repo, run_at)
        else:
            _record_releases(obs, repo, run_at)
            obs["releases_baseline_done"] = True

    if refreshed and was_notified:
        obs["stale_notified"] = False
        emitted.append(
            model.make_event(
                model.COLLECTION_RECOVERED,
                obs,
                run_at,
                None,
                "refresh succeeded again",
                {"consecutive_failures_before": failures_before},
                model.transition_event_id(obs["repository_id"], "recovered", run_at),
            )
        )

    if new_shas:
        emitted.append(_commits_event(index, obs, run_at, new_shas, rewrite, uncertain, future_dated))
    for rel in new_releases:
        emitted.append(_release_event(obs, run_at, rel))
    _merge_comparison(obs, repo, run_at, emitted)

    if prev_archived is False and obs["archived"] is True:
        emitted.append(
            model.make_event(
                model.REPOSITORY_ARCHIVED,
                obs,
                run_at,
                None,
                "archived state changed to true",
                {"archived": True, "previous": False},
                model.transition_event_id(obs["repository_id"], "archived", run_at),
            )
        )
    return new_shas


def _apply_failure(obs, repo, run_at, config, emitted):
    """A fetch that did not succeed: keep known values, count the failure."""
    obs["fetch_state"] = repo["fetch_state"]
    obs["consecutive_failures"] += 1
    known = obs["last_successful_refresh_at"] is not None
    if known:
        obs["stale"] = True
    if repo["fetch_state"] == "not_found":
        obs["collection_state"] = "not_found"
    elif known and obs["stale_notified"]:
        obs["collection_state"] = "stale"
    else:
        obs["collection_state"] = "failing"
    if obs["consecutive_failures"] >= config.stale_threshold and not obs["stale_notified"]:
        obs["stale_notified"] = True
        if known and repo["fetch_state"] != "not_found":
            obs["collection_state"] = "stale"
        emitted.append(
            model.make_event(
                model.COLLECTION_STALE,
                obs,
                run_at,
                None,
                (
                    "no longer found on GitHub for %s in a row (removed or made private; automated, unverified)" % _plural(obs["consecutive_failures"], "check")
                    if repo["fetch_state"] == "not_found"
                    else "refresh failed %s in a row" % _plural(obs["consecutive_failures"], "time")
                ),
                {
                    "consecutive_failures": obs["consecutive_failures"],
                    "threshold": config.stale_threshold,
                    "fetch_state": repo["fetch_state"],
                    "known_values_stale": known,
                },
                model.transition_event_id(obs["repository_id"], "stale", run_at),
            )
        )


# --- discovery and collection status ---------------------------------------------


def _discovery(run, collection, repository_id):
    """(queries, roots, sources) through which the repository is known to have been named.

    A repository is "found through the fork network" of a root when that
    root's scan lists it, in this run or in a stored earlier scan; a root is
    also a source for itself.
    """
    queries = set(s["query"] for s in run["searches"] if repository_id in s["repository_ids"])
    roots = set()
    for r in run["roots"]:
        if r["repository_id"] == repository_id or repository_id in (r.get("found_repository_ids") or []):
            roots.add(r["repository_id"])
    for key, r in collection["roots"].items():
        if int(key) == repository_id or repository_id in r["found_repository_ids"]:
            roots.add(int(key))
    for query, s in collection["searches"].items():
        if repository_id in s["repository_ids"]:
            queries.add(query)
    sources = set("search:%s" % q for q in queries)
    for root in roots:
        sources.add("root:%d" % root if root == repository_id else "fork_network:%d" % root)
    return sorted(queries), sorted(roots), sorted(sources)


def _merge_discovery(obs, run, collection):
    queries, roots, sources = _discovery(run, collection, obs["repository_id"])
    obs["discovery_queries"] = sorted(set(obs["discovery_queries"]) | set(queries))[: schema.MAX_SOURCES]
    obs["discovery_roots"] = sorted(set(obs["discovery_roots"]) | set(roots))[: schema.MAX_SOURCES]
    obs["discovery_sources"] = sorted(set(obs["discovery_sources"]) | set(sources))[: schema.MAX_SOURCES]


def _merge_found(previous, found, complete):
    """Found ids of a root: a complete scan replaces; partial scans union with what was known.

    Order is first-seen; when the bound is exceeded the oldest-seen ids go.
    """
    if complete or previous is None:
        merged = list(dict.fromkeys(found))
    else:
        merged = list(dict.fromkeys(list(previous["found_repository_ids"]) + list(found)))
    return merged[-schema.MAX_IDS :]


def _record_collection(state, run):
    """Search and root-scan status. A partial scan keeps what earlier scans found and its cursor."""
    from . import catalog

    col = state["collection"]
    run_at = run["run_at"]
    for search in run["searches"]:
        col["searches"][search["query"]] = {
            "complete": search["complete"],
            "repository_ids": sorted(set(search["repository_ids"])),
            "last_run_at": run_at,
        }
    for root in run["roots"]:
        key = str(root["repository_id"])
        previous = col["roots"].get(key)
        complete = root["forks_scan_complete"]
        cursor = None
        if not complete:
            cursor = root.get("resume_cursor")
            if cursor is None and previous is not None and previous["scan_state"] == "partial":
                cursor = previous["resume_cursor"]
        col["roots"][key] = {
            "full_name": root["full_name"],
            "forks_scan_complete": complete,
            "found_repository_ids": _merge_found(previous, root.get("found_repository_ids") or [], complete),
            "scan_state": "complete" if complete else "partial",
            "resume_cursor": cursor,
            "last_complete_at": run_at if complete else (previous["last_complete_at"] if previous else None),
            "last_run_at": run_at,
        }
    searches, roots = catalog.incomplete_work(col)
    col["incomplete_searches"] = searches
    col["incomplete_roots"] = roots
    col["incomplete"] = bool(searches or roots)
    named = set()
    for search in col["searches"].values():
        named.update(search["repository_ids"])
    for key, root in col["roots"].items():
        named.add(int(key))
        named.update(root["found_repository_ids"])
    col["pending_repository_ids"] = sorted(i for i in named if str(i) not in state["repositories"])[: schema.MAX_IDS]
    col["rejected_fields"] = [dict(r) for r in run.get("rejected_fields", [])][: schema.MAX_REJECTED]
    # A partial or stopped collection stays visible in the published status.
    report = run.get("collection_report") or {}
    col["last_run"] = {
        "last_run_at": run_at,
        "complete": bool(report.get("complete", True)),
        "stopped": report.get("stopped"),
        "not_fetched": int(report.get("not_fetched", 0)),
        "error_count": int(report.get("error_count", 0)),
    }


# --- discovery and association events --------------------------------------------


def _discovered_event(obs, run_at, related_event_id):
    details = {
        "discovery_sources": [text.escape_text(s) for s in obs["discovery_sources"]],
        "discovery_roots": list(obs["discovery_roots"]),
        "discovery_queries": [text.escape_text(q) for q in obs["discovery_queries"]],
        "is_fork": obs["is_fork"],
        "parent_repository_id": obs["parent_repository_id"],
        "network_root_repository_id": obs["network_root_repository_id"],
        "fetch_state": obs["fetch_state"],
        "commits_in_baseline": len(obs["observed_commits"]),
        "releases_in_baseline": len(obs["releases"]),
        "related_event_id": related_event_id,
    }
    source = obs["discovery_sources"][0] if obs["discovery_sources"] else "collector"
    return model.make_event(
        model.REPOSITORY_DISCOVERED,
        obs,
        run_at,
        None,
        "first seen via %s" % text.escape_text(source, 120),
        details,
        model.discovered_event_id(obs["repository_id"]),
    )


def _fork_discovered_event(repositories, obs, run_at, related_event_id):
    parent = repositories.get(str(obs["parent_repository_id"])) if obs["parent_repository_id"] is not None else None
    root = repositories.get(str(obs["network_root_repository_id"])) if obs["network_root_repository_id"] is not None else None
    if obs["ancestry_state"] == "resolved" and parent is not None:
        summary = "fork of %s first seen" % parent["full_name"]
    elif obs["ancestry_state"] == "resolved":
        summary = "fork first seen; parent repository %d not tracked" % obs["parent_repository_id"]
    else:
        summary = "fork first seen; parent not resolved (%s)" % obs["ancestry_state"]
    details = {
        "parent_repository_id": obs["parent_repository_id"],
        "parent": parent["full_name"] if parent else None,
        "network_root_repository_id": obs["network_root_repository_id"],
        "root": root["full_name"] if root else None,
        "ancestry_state": obs["ancestry_state"],
        "discovery_roots": list(obs["discovery_roots"]),
        "game_basis": obs["game"]["basis"],
        "inherited_from": obs["game"]["inherited_from"],
        "verified": False,
        "related_event_id": related_event_id,
    }
    return model.make_event(
        model.FORK_DISCOVERED,
        obs,
        run_at,
        None,
        summary,
        details,
        model.fork_discovered_event_id(obs["repository_id"]),
    )


def _fork_known(obs):
    return obs["is_fork"] is True and obs["parent_repository_id"] is not None


def _game_change_event(obs, previous, run_at):
    """game_association_changed for a repository whose own evidence moved its key."""
    basis = obs["game"]["basis"]
    if basis in forks.INHERITED_BASES:
        summary = "inferred game now inherited from parent (automated, unverified)"
    elif basis is None:
        summary = "inferred game association removed (automated, unverified)"
    else:
        summary = "inferred game association changed (automated, unverified)"
    details = {
        "previous_key": previous["key"],
        "new_key": obs["game"]["key"],
        "previous_title": text.escape_text(previous["title"], text.MAX_TITLE),
        "new_title": text.escape_text(obs["game"]["title"], text.MAX_TITLE),
        "previous_basis": previous["basis"],
        "basis": basis,
        "inherited_from": obs["game"]["inherited_from"],
        "confidence": obs["game"]["confidence"],
        "matched_alias": obs["game"]["matched_alias"],
        "inferred": True,
        "verified": False,
    }
    return model.make_event(
        model.GAME_ASSOCIATION_CHANGED,
        obs,
        run_at,
        None,
        summary,
        details,
        model.transition_event_id(obs["repository_id"], "game_association", run_at),
    )


def _inherited_change_event(cause, affected, run_at):
    """One aggregated game_association_changed for every repository that followed `cause`."""
    affected = sorted(affected)
    listed = affected[:MAX_AFFECTED_LISTED]
    details = {
        "cause": "inherited",
        "new_key": cause["game"]["key"],
        "new_title": text.escape_text(cause["game"]["title"], text.MAX_TITLE),
        "affected_count": len(affected),
        "affected_repository_ids": listed,
        "affected_truncated": len(listed) < len(affected),
        "inferred": True,
        "verified": False,
    }
    word = "repository" if len(affected) == 1 else "repositories"
    return model.make_event(
        model.GAME_ASSOCIATION_CHANGED,
        cause,
        run_at,
        None,
        "inherited game association changed for %d %s (automated, unverified)" % (len(affected), word),
        details,
        model.transition_event_id(cause["repository_id"], "game_association_inherited", run_at),
    )


def _association_events(repositories, previous_games, previous_text, origin, run_touched, run_at, list_before):
    """Direct changes one per repository; inheritance-caused changes aggregated per cause.

    Re-labelling by the reviewed alias list (config/games.json) is silent:
    a change is announced only when the game key differs and both this run
    and the previous one used the alias list (list_before: the state
    records the list's fingerprint). So the first run after the list was
    introduced announces nothing, and a repository that moves from a README
    heading (or nothing) onto an alias match is not announced either: the
    list was reviewed, the repository did nothing. A different spelling of
    the same key is never a change. A move between two keys, including off
    an alias match, is.
    """
    emitted = []
    by_cause = {}
    if not list_before:
        return emitted
    for rid, previous in previous_games.items():
        obs = repositories[rid]
        game = obs["game"]
        if game["key"] == previous["key"]:
            continue
        if previous.get("matched_alias") is None and game["matched_alias"] is not None:
            continue
        own_text_changed = rid in run_touched and previous_text[rid] != (obs["readme_excerpt"], obs["description"])
        cause = None
        if not own_text_changed:
            if origin.get(rid) == "inherited":
                cause = game["inherited_from"]
            elif previous["basis"] in forks.INHERITED_BASES:
                cause = previous["inherited_from"]
        if cause is not None and str(cause) in repositories and cause != obs["repository_id"]:
            by_cause.setdefault(cause, []).append(obs["repository_id"])
        else:
            emitted.append(_game_change_event(obs, previous, run_at))
    for cause, affected in sorted(by_cause.items()):
        emitted.append(_inherited_change_event(repositories[str(cause)], affected, run_at))
    return emitted


# --- the run ------------------------------------------------------------------


def _apply(state, run, config):
    run_at = run["run_at"]
    emitted = []
    baseline_run = False
    if not state["baseline_done"]:
        if not run["repositories"]:
            _record_collection(state, run)
            state["last_run_at"] = run_at
            state["games_list"] = config.aliases.fingerprint
            return []
        baseline_run = True
        state["baseline_done"] = True
        state["baseline_at"] = run_at

    repositories = state["repositories"]
    index = RunIndex(repositories)
    previous_games = {rid: dict(obs["game"]) for rid, obs in repositories.items()}
    previous_text = {rid: (obs["readme_excerpt"], obs["description"]) for rid, obs in repositories.items()}
    fork_known_before = {rid for rid, obs in repositories.items() if obs["fork_discovered_done"]}
    new_ids = []
    run_touched = set()
    run_new = {}
    for repo in sorted(run["repositories"], key=lambda r: r["repository_id"]):
        rid = repo["repository_id"]
        obs = get_observation(state, rid)
        is_new = obs is None
        if is_new:
            obs = _new_observation(repo, run_at)
            put_observation(state, obs)
            new_ids.append(rid)
        run_touched.add(str(rid))
        obs["last_attempted_refresh_at"] = run_at
        _merge_discovery(obs, run, state["collection"])

        if repo["fetch_state"] == "ok":
            new_shas = _apply_success(index, obs, repo, run_at, config, emitted)
            if new_shas:
                run_new[rid] = set(new_shas)
        else:
            _apply_failure(obs, repo, run_at, config, emitted)

    # Ancestry state, inferred game and toolkit for every observation, with
    # inheritance from resolved tracked ancestors (bounded walk, loops detected).
    origin = forks.resolve_associations(repositories, config.aliases)
    list_before = state.get("games_list") is not None
    state["games_list"] = config.aliases.fingerprint

    new_set = set(new_ids)
    # A new repository's own history is its baseline: only announce it.
    emitted = [e for e in emitted if e["repository_id"] not in new_set]
    _count_shared_in_run(emitted, run_new, repositories)

    fork_events = []
    for rid, obs in repositories.items():
        if not obs["fork_discovered_done"] and _fork_known(obs):
            obs["fork_discovered_done"] = True
            if not baseline_run:
                fork_events.append(obs)

    if not baseline_run:
        for rid in new_ids:
            obs = repositories[str(rid)]
            related = model.fork_discovered_event_id(rid) if obs in fork_events else None
            emitted.append(_discovered_event(obs, run_at, related))
        for obs in fork_events:
            related = model.discovered_event_id(obs["repository_id"]) if obs["repository_id"] in new_set else None
            emitted.append(_fork_discovered_event(repositories, obs, run_at, related))
        emitted.extend(_association_events(repositories, previous_games, previous_text, origin, run_touched, run_at, list_before))

    _record_collection(state, run)
    state["last_run_at"] = run_at
    emitted.sort(key=lambda e: (e["repository_id"], e["type"], e["event_id"]))
    return emitted


def apply_run(state, run, config=None):
    """Apply one run to the state atomically. Returns the events it emitted.

    The run is validated (and its free text bounded) first. Work happens on
    a copy; every new event and the resulting state are validated before
    the caller's state is replaced. On any error the caller's state is
    untouched. The events are also appended to state["events"].
    """
    run = schema.validate_run(run)
    if config is None:
        config = Config()
    run_at = run["run_at"]
    if state["last_run_at"] is not None and run_at < state["last_run_at"]:
        raise RunOrderError("run at %s is older than the last applied run at %s" % (run_at, state["last_run_at"]))

    work = copy.deepcopy(state)
    emitted = _apply(work, run, config)

    known_ids = set(e["event_id"] for e in work["events"])
    for event in emitted:
        schema.validate_event(event)
        if event["event_id"] in known_ids:
            raise schema.SchemaError("event %s already exists in the store" % event["event_id"])
        known_ids.add(event["event_id"])
    work["events"].extend(emitted)
    schema.validate_state(work)

    state.clear()
    state.update(work)
    return emitted

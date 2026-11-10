"""Diff a run against the observation store and emit canonical events.

Rules (spec 02, "Event generation and noise controls", the test plan and
the review findings):

- Runs apply in order: a run older than the last applied run is rejected.
  A run with the same timestamp is a replay and is idempotent.
- The first run that lists at least one repository is the global baseline:
  observations are stored, no event. Earlier empty runs only record
  collection status.
- A repository seen for the first time after the baseline gets one
  repository_discovered event. Its commit and release baselines are set by
  the first successful fetch that returns them, not by its first sighting,
  so a repository first seen while failing does not flood later.
- Commits are tracked per SHA. A run's unseen SHAs produce one grouped
  commits_observed event whose id is a digest of the sorted SHAs. The
  branch head is the last listed commit (or `head_sha`), not the latest
  date; commit dates after the run time are clamped and flagged.
- History rewrite: the previous head missing from a complete listing means
  it is no longer reachable, so the SHAs are recorded conservatively (no
  exact count). Missing from an incomplete listing means the count is a
  lower bound. A later complete listing that contains the previous head
  clears the suspicion.
- repository_archived is emitted only on a known false -> true transition.
- A successful fetch that returns null for a field keeps the previous
  value and marks the entry partial. A success that refreshes neither
  commits nor releases does not count as a refresh.
- A failed fetch keeps the previous known values and marks them stale
  (only if there was a successful refresh to be stale from). The failure
  counter drives one collection_stale event at the configured threshold
  and one collection_recovered event when such an entry refreshes again.
- apply_run is atomic: it works on a copy, validates every new event and
  the whole state, and only then replaces the caller's state.
- Every string from the run is stored as bounded data; renderer-facing
  text is built from fixed templates with escaped values.
"""

import copy

from . import model, schema, text
from .store import get_observation, put_observation


class Config:
    def __init__(self, stale_threshold=3):
        if not isinstance(stale_threshold, int) or isinstance(stale_threshold, bool) or stale_threshold < 1:
            raise ValueError("stale_threshold must be a positive integer")
        self.stale_threshold = stale_threshold


class RunOrderError(ValueError):
    """The run is older than the last run already applied."""


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _commit_word(n):
    return "commit" if n == 1 else "commits"


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
        "partial": False,
        "fetch_state": repo["fetch_state"],
        "collection_state": "ok",
        "consecutive_failures": 0,
        "stale_notified": False,
        "first_seen_at": run_at,
        "last_attempted_refresh_at": run_at,
        "last_successful_refresh_at": None,
        "discovery_sources": sorted(sources),
        "commits_baseline_done": False,
        "releases_baseline_done": False,
        "commits_complete": None,
        "tip_sha": None,
        "tip_committed_at": None,
        "history_rewrite_suspected": False,
        "observed_commits": {},
        "releases": {},
        "category": category,
        "game": game,
        "toolkit": toolkit,
    }


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


def _commits_event(obs, run_at, new_shas, rewrite, uncertain, future_dated):
    source_at = max(obs["observed_commits"][s]["committed_at"] for s in new_shas)
    if source_at > run_at:
        source_at = run_at
    exact = obs["commits_complete"] is True and not rewrite and not uncertain
    n = len(new_shas)
    if rewrite:
        summary = "default-branch commits observed after a possible history rewrite; exact delta unknown"
        count = None
    elif exact:
        summary = "%d previously unseen default-branch %s" % (n, _commit_word(n))
        count = n
    else:
        summary = "at least %d previously unseen default-branch %s (collection incomplete)" % (n, _commit_word(n))
        count = n
    listed = new_shas[: model.MAX_EVENT_SHAS]
    details = {
        "commit_shas": list(listed),
        "commit_shas_truncated": len(listed) < n,
        "commit_count": count,
        "count_exact": exact,
        "complete": exact,
        "history_rewrite_suspected": rewrite,
        "future_dated": future_dated,
        "commit_urls": [text.commit_url(obs["full_name"], s) for s in listed],
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
        min(rel["published_at"], run_at),
        "%s published" % tag,
        details,
        model.release_event_id(obs["repository_id"], rel["id"]),
    )


_PRESERVED_FIELDS = (
    "description",
    "readme_excerpt",
    "archived",
    "stars",
    "default_branch",
)


def _merge_ancestry(obs, repo):
    """Update is_fork/parent/root from a successful fetch. Returns True if anything was unknown.

    null parent/root ids are a real answer for a non-fork; for a fork, or
    when is_fork itself is null, they mean "not fetched" and the previous
    values are kept.
    """
    if repo["is_fork"] is None:
        return True
    obs["is_fork"] = repo["is_fork"]
    if repo["is_fork"] is False:
        obs["parent_repository_id"] = repo["parent_repository_id"]
        obs["network_root_repository_id"] = repo["network_root_repository_id"]
        return False
    partial = False
    for field in ("parent_repository_id", "network_root_repository_id"):
        if repo[field] is None:
            partial = partial or obs[field] is not None
        else:
            obs[field] = repo[field]
    return partial


def _apply_success(obs, repo, run_at, config, emitted):
    """Update an observation from a fetch with state ok; append events."""
    was_notified = obs["stale_notified"]
    failures_before = obs["consecutive_failures"]
    prev_archived = obs["archived"]
    prev_game_key = obs["game"]["key"]
    prev_title = obs["game"]["title"]

    if repo["full_name"] != obs["full_name"] or repo["owner"] != obs["owner"]:
        obs["name_history"].append({"full_name": obs["full_name"], "owner": obs["owner"], "until": run_at})
        obs["full_name"] = repo["full_name"]
        obs["owner"] = repo["owner"]

    partial = _merge_ancestry(obs, repo)
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

    category, game, toolkit = text.classify(obs["full_name"], obs["description"], obs["readme_excerpt"])
    obs["category"] = category
    obs["toolkit"] = toolkit
    obs["game"] = game

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
        emitted.append(_commits_event(obs, run_at, new_shas, rewrite, uncertain, future_dated))
    for rel in new_releases:
        emitted.append(_release_event(obs, run_at, rel))

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

    if obs["game"]["key"] != prev_game_key:
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
                model.transition_event_id(obs["repository_id"], "game_association", run_at),
            )
        )


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
                "refresh failed %s in a row" % _plural(obs["consecutive_failures"], "time"),
                {
                    "consecutive_failures": obs["consecutive_failures"],
                    "threshold": config.stale_threshold,
                    "fetch_state": repo["fetch_state"],
                    "known_values_stale": known,
                },
                model.transition_event_id(obs["repository_id"], "stale", run_at),
            )
        )


def _discovery_sources(run, repository_id):
    sources = []
    for search in run["searches"]:
        if repository_id in search["repository_ids"]:
            sources.append("search:%s" % search["query"])
    for root in run["roots"]:
        if root["repository_id"] == repository_id:
            sources.append("root:%d" % root["repository_id"])
    return sorted(sources)


def _record_collection(state, run):
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
        col["roots"][str(root["repository_id"])] = {
            "full_name": root["full_name"],
            "forks_scan_complete": root["forks_scan_complete"],
            # Fork-network listing of the root; recorded only, step 2 uses it.
            "found_repository_ids": sorted(set(root.get("found_repository_ids") or [])),
            "last_run_at": run_at,
        }
    searches, roots = catalog.incomplete_work(col)
    col["incomplete_searches"] = searches
    col["incomplete_roots"] = roots
    col["incomplete"] = bool(searches or roots)


def _discovered_event(obs, run_at):
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
    return model.make_event(
        model.REPOSITORY_DISCOVERED,
        obs,
        run_at,
        None,
        "first seen via %s" % text.escape_text(source, 120),
        details,
        model.discovered_event_id(obs["repository_id"]),
    )


def _apply(state, run, config):
    run_at = run["run_at"]
    emitted = []
    baseline_run = False
    if not state["baseline_done"]:
        if not run["repositories"]:
            _record_collection(state, run)
            state["last_run_at"] = run_at
            return []
        baseline_run = True
        state["baseline_done"] = True
        state["baseline_at"] = run_at

    for repo in sorted(run["repositories"], key=lambda r: r["repository_id"]):
        rid = repo["repository_id"]
        obs = get_observation(state, rid)
        is_new = obs is None
        if is_new:
            obs = _new_observation(repo, run_at, _discovery_sources(run, rid))
            put_observation(state, obs)
        obs["last_attempted_refresh_at"] = run_at
        merged = set(obs["discovery_sources"]) | set(_discovery_sources(run, rid))
        obs["discovery_sources"] = sorted(merged)[: schema.MAX_SOURCES]

        if repo["fetch_state"] == "ok":
            _apply_success(obs, repo, run_at, config, emitted)
        else:
            _apply_failure(obs, repo, run_at, config, emitted)

        if is_new:
            # The repository's own history is its baseline; only announce it.
            emitted[:] = [e for e in emitted if e["repository_id"] != rid]
            if not baseline_run:
                emitted.append(_discovered_event(obs, run_at))

    _record_collection(state, run)
    state["last_run_at"] = run_at
    # On the baseline run every repository is new, so the loop above has
    # already dropped all of their events and skipped the discovery ones.
    emitted.sort(key=lambda e: (e["repository_id"], e["type"], e["event_id"]))
    return emitted


def apply_run(state, run, config=None):
    """Apply one run to the state atomically. Returns the events it emitted.

    The run is validated (and its free text bounded) first. Work happens on
    a copy; every new event and the resulting state are validated before
    the caller's state is replaced. On any error the caller's state is
    untouched. The events are also appended to state["events"].
    """
    schema.validate_run(run)
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

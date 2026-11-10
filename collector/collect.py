"""Drive the GitHub client and adapt API responses into the run format.

Order of work in `collect`:

1. Searches: GET /search/repositories per configured query (per_page 100,
   page cap). `complete` is false when the API says incomplete_results,
   when the page cap or a stop ended the walk, when an item had to be
   dropped, or when total_count exceeds the distinct ids listed (this
   covers the API's 1000-result ceiling).
2. Roots: GET /repos/{owner}/{repo}/forks per configured root (per_page
   100, page cap). A capped or interrupted walk sets forks_scan_complete
   false and a resume_cursor "page=N".
3. Candidates: the union of search ids, fork ids and root ids, ascending,
   bounded by max_repositories. For each: GET /repositories/{id} (by id,
   so renames are followed), then GET .../commits?sha=<default branch>&
   since=<run_at minus the window>&per_page=100 (page cap; a cap sets
   commits_complete false), GET .../releases?per_page=30 (drafts,
   unpublished and id-less entries skipped), GET .../readme with the raw
   media type (cut to readme_bytes). Statuses: 404/410 -> not_found, 451
   and plain 403 -> blocked, other non-200 -> failed; a malformed body, a
   redirect, an oversize body, bad JSON or a transport failure on the
   metadata request -> failed. The same on a sub-request leaves that field
   null (the repository is then partial) and the run continues. Every such
   failure is counted in a bounded report.
4. A rate limit, the request budget or repeated transport failures stop
   the run: what was fetched is kept, the repository in progress is marked
   rate_limited (or failed) with null metrics, the rest is absent (the
   collector lists them as pending) and named in the report. Nothing is
   ever recorded as zero because it was not fetched.
5. The run carries a bounded `collection_report` (stop reason, counts) so
   a partial run is visible in the file, and is validated with the strict
   loader before it is returned.

Comparison endpoints are not requested: every comparison is reported
unavailable.

Repository text is copied as data; the loader bounds it and the renderer
escapes it. URLs from responses (html_url and friends) are ignored.
"""

import os
import re
from datetime import datetime, timedelta, timezone

from . import github, schema

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "sources.example.json")

DEFAULT_COMMIT_WINDOW_DAYS = 30
DEFAULT_MAX_REQUESTS = 500
MAX_REQUESTS_LIMIT = 5000
DEFAULT_MAX_REPOSITORIES = 500
DEFAULT_README_BYTES = schema.MAX_README
RELEASES_PER_PAGE = 30
PER_PAGE = 100
MAX_REPORT_ERRORS = 100

_SPACE_RE = re.compile(r"\s+")

ROOT_CONFIG = schema.Obj({"repository_id": schema.PositiveId(), "full_name": schema.FULL_NAME})
PAGE_CAPS = schema.Obj(
    {"search": schema.Int(1, 10), "forks": schema.Int(1, 100), "commits": schema.Int(1, 50)},
    optional=("search", "forks", "commits"),
)
CONFIG = schema.Obj(
    {
        "queries": schema.List(schema.QUERY, 50),
        "roots": schema.List(ROOT_CONFIG, 200),
        "commit_window_days": schema.Int(1, 365),
        "max_requests": schema.Int(1, MAX_REQUESTS_LIMIT),
        "max_repositories": schema.Int(1, 5000),
        "readme_bytes": schema.Int(256, schema.MAX_README),
        "page_caps": PAGE_CAPS,
        "_comment": schema.Str(max_len=1024),
    },
    optional=("commit_window_days", "max_requests", "max_repositories", "readme_bytes", "page_caps", "_comment"),
)


class ShapeError(github.ResponseError):
    """A response item does not have the fields the endpoint promises."""


# --- configuration ------------------------------------------------------------


def validate_config(config):
    """Check a config dict. Error messages name paths and counts, never values or keys from the file."""
    if not isinstance(config, dict):
        raise schema.SchemaError("config: expected an object")
    unknown = set(config) - set(CONFIG.fields)
    if unknown:
        raise schema.SchemaError("config: %d unknown field(s)" % len(unknown))
    caps = config.get("page_caps")
    if isinstance(caps, dict):
        unknown = set(caps) - set(PAGE_CAPS.fields)
        if unknown:
            raise schema.SchemaError("config.page_caps: %d unknown field(s)" % len(unknown))
    CONFIG.check(config, "config")
    if not config["queries"] and not config["roots"]:
        raise schema.SchemaError("config: at least one query or one root is required")
    for i, query in enumerate(config["queries"]):
        if "://" in query or schema._SCHEME_RE.match(query) is not None:
            raise schema.SchemaError("config.queries[%d]: a search query must not contain a URL" % i)
    names = [r["full_name"].lower() for r in config["roots"]]
    if len(set(names)) != len(names):
        raise schema.SchemaError("config.roots: duplicate full_name")
    config.setdefault("commit_window_days", DEFAULT_COMMIT_WINDOW_DAYS)
    config.setdefault("max_requests", DEFAULT_MAX_REQUESTS)
    config.setdefault("max_repositories", DEFAULT_MAX_REPOSITORIES)
    config.setdefault("readme_bytes", DEFAULT_README_BYTES)
    merged = dict(github.DEFAULT_PAGE_CAPS)
    merged.update(config.get("page_caps") or {})
    config["page_caps"] = merged
    return config


def load_config(path):
    return validate_config(schema.read_json(path))


def _normalise_name(value):
    return _SPACE_RE.sub(" ", value.strip()).lower()


def example_names(path=EXAMPLE_CONFIG_PATH):
    """Normalised root names and queries of the committed example config."""
    example = validate_config(schema.read_json(path))
    names = set(_normalise_name(r["full_name"]) for r in example["roots"])
    names.update(_normalise_name(q) for q in example["queries"])
    return names


def config_uses_example_names(config, path=EXAMPLE_CONFIG_PATH):
    """Names in `config` that also appear in the example file (placeholders, never real)."""
    placeholders = example_names(path)
    found = [r["full_name"] for r in config["roots"] if _normalise_name(r["full_name"]) in placeholders]
    found += [q for q in config["queries"] if _normalise_name(q) in placeholders]
    return found


def plan_requests(config):
    """The requests a run starts with; later ones depend on what these return."""
    plan = []
    for query in config["queries"]:
        plan.append({"method": "GET", "path": "/search/repositories", "query": github.canonical_query({"q": query, "per_page": PER_PAGE, "page": 1})})
    for root in config["roots"]:
        plan.append({"method": "GET", "path": github.path_for_repo(root["full_name"], "/forks"), "query": github.canonical_query({"per_page": PER_PAGE, "page": 1})})
    for root in config["roots"]:
        plan.append({"method": "GET", "path": "/repositories/%d" % root["repository_id"], "query": ""})
    return plan


# --- response shapes ----------------------------------------------------------


def _field(obj, name, kinds, path, nullable=False):
    """A typed field. A missing or null required field is a ShapeError; nullable allows null or absence."""
    if not isinstance(obj, dict):
        raise ShapeError("response for %s is not an object" % path)
    if name not in obj or obj[name] is None:
        if nullable:
            return None
        raise ShapeError("response for %s lacks %s" % (path, name))
    value = obj[name]
    if isinstance(value, bool) and bool not in kinds:
        raise ShapeError("response for %s has a boolean %s" % (path, name))
    if not isinstance(value, kinds):
        raise ShapeError("response for %s has an unexpected type for %s" % (path, name))
    return value


def _id(obj, name, path, nullable=False):
    value = _field(obj, name, (int,), path, nullable)
    if value is not None and (value < 1 or value > schema.MAX_SAFE_INT):
        raise ShapeError("response for %s has an out-of-range %s" % (path, name))
    return value


def _full_name(obj, path):
    value = _field(obj, "full_name", (str,), path)
    try:
        schema.FULL_NAME.check(value, path)
    except schema.SchemaError:
        raise ShapeError("response for %s has an invalid full_name" % path) from None
    return value


def _listed_repository(item, path):
    """(id, full_name) of a repository item in a search or forks listing."""
    return _id(item, "id", path), _full_name(item, path)


def _timestamp(value, path):
    try:
        return schema.TIMESTAMP.check(value, path)
    except schema.SchemaError:
        raise ShapeError("response for %s has an invalid timestamp" % path) from None


def adapt_repository(data, rid, path):
    """Metadata fields of GET /repositories/{id} in run-format shape, plus a list of softened fields."""
    if _id(data, "id", path) != rid:
        raise ShapeError("response for %s names another repository" % path)
    full_name = _full_name(data, path)
    owner = _field(data, "owner", (dict,), path, nullable=True)
    login = _field(owner, "login", (str,), path, nullable=True) if owner is not None else None
    if login is not None and login != full_name.split("/")[0]:
        raise ShapeError("response for %s has an owner that does not match full_name" % path)
    parent = _field(data, "parent", (dict,), path, nullable=True)
    source = _field(data, "source", (dict,), path, nullable=True)
    softened = []
    stars = _field(data, "stargazers_count", (int,), path, nullable=True)
    if stars is not None and (stars < 0 or stars > schema.MAX_SAFE_INT):
        stars = None
        softened.append("stargazers_count")
    return {
        "full_name": full_name,
        "owner": full_name.split("/")[0],
        "description": _field(data, "description", (str,), path, nullable=True),
        "is_fork": _field(data, "fork", (bool,), path, nullable=True),
        "parent_repository_id": _id(parent, "id", path, nullable=True) if parent is not None else None,
        "network_root_repository_id": _id(source, "id", path, nullable=True) if source is not None else None,
        "archived": _field(data, "archived", (bool,), path, nullable=True),
        "stars": stars,
        "default_branch": _field(data, "default_branch", (str,), path, nullable=True),
    }, softened


def adapt_commits(items, path):
    """(commits oldest first, dropped count). Malformed or duplicate items are dropped."""
    commits = []
    dropped = 0
    for item in items:
        try:
            sha = _field(item, "sha", (str,), path)
            schema.SHA.check(sha, path)
            commit = _field(item, "commit", (dict,), path)
            committer = _field(commit, "committer", (dict,), path, nullable=True)
            date = _field(committer, "date", (str,), path, nullable=True) if committer is not None else None
            if date is None:
                author = _field(commit, "author", (dict,), path, nullable=True)
                date = _field(author, "date", (str,), path, nullable=True) if author is not None else None
            if date is None:
                raise ShapeError("response for %s has a commit without a date" % path)
            message = _field(commit, "message", (str,), path, nullable=True) or ""
            commits.append({"sha": sha, "committed_at": _timestamp(date, path), "message": message})
        except (ShapeError, schema.SchemaError):
            dropped += 1
    commits.reverse()
    seen = set()
    unique = []
    for commit in commits:
        if commit["sha"] in seen:
            dropped += 1
            continue
        seen.add(commit["sha"])
        unique.append(commit)
    return unique, dropped


def adapt_releases(items, path):
    """(releases, dropped count). Drafts and unpublished entries are skipped silently; malformed ones counted."""
    releases = []
    seen = set()
    dropped = 0
    for item in items:
        try:
            if _field(item, "draft", (bool,), path, nullable=True) is True:
                continue
            published = _field(item, "published_at", (str,), path, nullable=True)
            if published is None:
                continue
            rid = _id(item, "id", path)
            tag = _field(item, "tag_name", (str,), path, nullable=True) or ""
            if rid in seen:
                dropped += 1
                continue
            seen.add(rid)
            releases.append({"id": rid, "tag": tag, "published_at": _timestamp(published, path)})
        except (ShapeError, schema.SchemaError):
            dropped += 1
    return releases, dropped


def fetch_state_for(status):
    if status in (404, 410):
        return "not_found"
    if status in (403, 451):
        return "blocked"
    return "failed"


def _since(run_at, days):
    moment = datetime.strptime(run_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_repository(rid, full_name, fetch_state):
    return {
        "repository_id": rid,
        "full_name": full_name,
        "owner": full_name.split("/")[0],
        "description": None,
        "readme_excerpt": None,
        "is_fork": None,
        "parent_repository_id": None,
        "network_root_repository_id": None,
        "archived": None,
        "stars": None,
        "default_branch": None,
        "fetch_state": fetch_state,
        "commits_complete": None,
        "commits": None,
        "releases": None,
    }


# --- the collection -----------------------------------------------------------


class _Stop(Exception):
    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


_STOPS = (github.RateLimited, github.BudgetExhausted, github.NetworkFailed)
_FAILURES = (github.ResponseError, github.TransportFailed, github.TransportRefused)
_STOP_REASONS = ("rate_limited", "budget", "network")


def _failure_kind(exc):
    if isinstance(exc, github.TransportFailed):
        return "transport"
    if isinstance(exc, ShapeError):
        return "shape"
    return "response"


def collect(client, config, run_at):
    """Return (run, report). The run passes schema.validate_run; the report says what did not happen."""
    config = validate_config(dict(config))
    schema.TIMESTAMP.check(run_at, "run_at")
    caps = config["page_caps"]
    run = {"run_at": run_at, "roots": [], "searches": [], "repositories": []}
    report = {
        "stopped": None,
        "errors": [],
        "error_count": 0,
        "dropped_items": 0,
        "not_fetched_repository_ids": [],
        "skipped_unnamed_ids": [],
        "candidates_truncated": False,
    }
    names = {}
    candidates = set()

    def note_error(path, reason):
        report["error_count"] += 1
        if len(report["errors"]) < MAX_REPORT_ERRORS:
            report["errors"].append({"path": path, "reason": reason})

    def attempt(path, action, *args):
        """One request. Returns (value, None) or (None, failure kind); raises _Stop on a stop."""
        try:
            return action(*args), None
        except _STOPS as exc:
            raise _Stop(github._stop_reason(exc))
        except _FAILURES as exc:
            kind = _failure_kind(exc)
            note_error(path, kind)
            return None, kind

    def listed_ids(items, path):
        ids = []
        for item in items:
            try:
                rid, full_name = _listed_repository(item, path)
            except github.ResponseError:
                report["dropped_items"] += 1
                continue
            ids.append(rid)
            names.setdefault(rid, full_name)
        return ids

    def after_pages(path, pages):
        """A stop seen mid-walk ends the run; any other interruption is an error note."""
        if pages.stopped in _STOP_REASONS:
            raise _Stop(pages.stopped)
        if pages.stopped is not None:
            note_error(path, pages.stopped)

    try:
        for query in config["queries"]:
            entry = {"query": query, "complete": False, "repository_ids": []}
            run["searches"].append(entry)
            pages, failed = attempt("/search/repositories", client.paginate, "/search/repositories", {"q": query, "per_page": PER_PAGE}, caps["search"], "object", "items")
            if failed or pages.items is None:
                if not failed:
                    note_error("/search/repositories", pages.stopped or "status")
                continue
            before = report["dropped_items"]
            ids = listed_ids(pages.items, "/search/repositories")
            entry["repository_ids"] = sorted(set(ids))
            candidates.update(ids)
            total = pages.extra.get("total_count")
            incomplete = pages.extra.get("incomplete_results")
            clean = report["dropped_items"] == before
            # total_count above the distinct ids listed covers the API's 1000-result ceiling too.
            capped_total = isinstance(total, int) and not isinstance(total, bool) and total > len(entry["repository_ids"])
            entry["complete"] = bool(pages.complete and clean and incomplete is not True and not capped_total)
            after_pages("/search/repositories", pages)

        for root in config["roots"]:
            rid, full_name = root["repository_id"], root["full_name"]
            names.setdefault(rid, full_name)
            candidates.add(rid)
            entry = {"repository_id": rid, "full_name": full_name, "forks_scan_complete": False, "found_repository_ids": []}
            run["roots"].append(entry)
            path = github.path_for_repo(full_name, "/forks")
            pages, failed = attempt(path, client.paginate, path, {"per_page": PER_PAGE}, caps["forks"])
            if failed or pages.items is None:
                if not failed:
                    note_error(path, pages.stopped or "status")
                continue
            before = report["dropped_items"]
            found = listed_ids(pages.items, path)
            entry["found_repository_ids"] = list(dict.fromkeys(found))
            entry["forks_scan_complete"] = bool(pages.complete and report["dropped_items"] == before)
            if not pages.complete and pages.next_cursor is not None:
                entry["resume_cursor"] = pages.next_cursor
            candidates.update(found)
            after_pages(path, pages)

        ordered = sorted(candidates)
        if len(ordered) > config["max_repositories"]:
            report["candidates_truncated"] = True
            report["not_fetched_repository_ids"].extend(ordered[config["max_repositories"] :])
            ordered = ordered[: config["max_repositories"]]
        since = _since(run_at, config["commit_window_days"])
        for rid in ordered:
            full_name = names.get(rid)
            path = "/repositories/%d" % rid
            try:
                result, failed = attempt(path, client.get_json, path, None, "object")
            except _Stop as stop:
                if full_name is not None:
                    state = "rate_limited" if stop.reason == "rate_limited" else "failed"
                    run["repositories"].append(_empty_repository(rid, full_name, state))
                raise
            if failed or result.status != 200:
                if full_name is None:
                    report["skipped_unnamed_ids"].append(rid)
                    continue
                run["repositories"].append(_empty_repository(rid, full_name, "failed" if failed else fetch_state_for(result.status)))
                continue
            try:
                meta, softened = adapt_repository(result.data, rid, path)
            except github.ResponseError:
                note_error(path, "shape")
                if full_name is None:
                    report["skipped_unnamed_ids"].append(rid)
                    continue
                run["repositories"].append(_empty_repository(rid, full_name, "failed"))
                continue
            for field in softened:
                note_error(path, "range:" + field)
            repo = _empty_repository(rid, meta["full_name"], "ok")
            repo.update(meta)
            run["repositories"].append(repo)
            branch = meta["default_branch"]
            if branch is not None:
                cpath = github.path_for_repo(meta["full_name"], "/commits")
                pages, failed = attempt(cpath, client.paginate, cpath, {"sha": branch, "since": since, "per_page": PER_PAGE}, caps["commits"])
                if not failed and pages.items is not None:
                    commits, dropped = adapt_commits(pages.items, cpath)
                    report["dropped_items"] += dropped
                    repo["commits"] = commits
                    repo["commits_complete"] = bool(pages.complete and dropped == 0)
                    after_pages(cpath, pages)
                elif not failed:
                    note_error(cpath, pages.stopped or "status")
            rpath = github.path_for_repo(meta["full_name"], "/releases")
            result, failed = attempt(rpath, client.get_json, rpath, {"per_page": RELEASES_PER_PAGE}, "array")
            if not failed and result.status == 200:
                repo["releases"], dropped = adapt_releases(result.data, rpath)
                report["dropped_items"] += dropped
            elif not failed:
                note_error(rpath, "status")
            dpath = github.path_for_repo(meta["full_name"], "/readme")
            result, failed = attempt(dpath, client.get_text, dpath, None, config["readme_bytes"])
            if not failed and result.status == 200:
                repo["readme_excerpt"] = result.data
            elif not failed and result.status != 404:
                note_error(dpath, "status")
    except _Stop as stop:
        report["stopped"] = stop.reason

    listed = set(r["repository_id"] for r in run["repositories"])
    report["not_fetched_repository_ids"] = sorted(set(report["not_fetched_repository_ids"]) | (set(candidates) - listed))
    run["repositories"].sort(key=lambda r: r["repository_id"])
    complete = (
        report["stopped"] is None
        and report["error_count"] == 0
        and report["dropped_items"] == 0
        and not report["not_fetched_repository_ids"]
        and not report["skipped_unnamed_ids"]
        and all(s["complete"] for s in run["searches"])
        and all(r["forks_scan_complete"] for r in run["roots"])
        and all(r["fetch_state"] == "ok" and r["commits_complete"] is not False for r in run["repositories"])
    )
    run["collection_report"] = {
        "complete": complete,
        "stopped": report["stopped"],
        "error_count": report["error_count"],
        "dropped_items": report["dropped_items"],
        "not_fetched": len(report["not_fetched_repository_ids"]),
        "requests_made": client.requests_made,
    }
    schema.validate_run(run)
    report["complete"] = complete
    report["requests"] = list(client.log)
    report["client"] = client.summary()
    return run, report

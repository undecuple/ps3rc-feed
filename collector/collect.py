"""Drive the GitHub client and adapt API responses into the run format.

Order of work in `collect`:

1. Searches: GET /search/repositories per configured query (per_page 100,
   page cap). `complete` is false when the API says incomplete_results,
   when the page cap or a stop ended the walk, when an item had to be
   dropped, when the listing exceeded the run format's cap, or when
   total_count exceeds the distinct ids listed (this covers the API's
   1000-result ceiling). Search requests draw from GitHub's separate
   search rate-limit bucket (10 a minute anonymous, 30 authenticated):
   when that bucket is exhausted the remaining searches are marked
   complete=false with a resume cursor and the run goes on with the other
   endpoints; the report says search_rate_limited.
2. Roots: GET /repos/{owner}/{repo}/forks per configured root (per_page
   100, page cap). A capped or interrupted walk sets forks_scan_complete
   false and a resume_cursor "page=N".
3. Candidates: the union of search ids, fork ids and root ids, ascending,
   bounded by max_repositories. For each: GET /repositories/{id} (by id,
   so renames are followed), then GET .../commits?sha=<default branch>&
   since=<run_at minus the window>&per_page=100 (page cap; a cap sets
   commits_complete false; a 409, GitHub's answer for an empty
   repository, is an empty complete listing), GET .../releases?per_page=30
   (drafts, unpublished and id-less entries skipped), GET .../readme with
   the raw media type (cut to readme_bytes). Statuses: 404/410 ->
   not_found, 451 and plain 403 -> blocked, other non-200 -> failed; a
   malformed body, a redirect, an oversize body, bad JSON or a transport
   failure on the metadata request -> failed. The same on a sub-request
   leaves that field null (the repository is then partial) and the run
   continues. Metadata that lacks fork, archived, stargazers_count or
   default_branch is partial too. Every such failure is counted in a
   bounded report. Pagination follows GitHub's next links, which spell
   /repos/{owner}/{repo}/... as /repositories/{id}/...; the id must be the
   one the metadata request returned.
4. A core rate limit, a secondary limit, the request budget or repeated
   transport failures stop the run: what was fetched is kept; a repository
   whose request was actually made and refused is marked rate_limited (or
   failed); a repository that was never requested is simply absent from
   the run (its previous observation, if any, stays as it was) and is
   counted in collection_report.not_fetched. Nothing is ever recorded as
   zero because it was not fetched.
5. The run carries a bounded `collection_report` (complete, stop reason,
   counts) so a partial run is visible in the file, and is validated with
   the strict loader before it is returned; the loader cannot be made to
   raise by response content (every listing is capped and every field
   bounded first); if it ever did, the result is an InternalError with a
   fixed message.

Comparison endpoints are not requested: every comparison is reported
unavailable.

Repository text is copied as data; the loader bounds it and the renderer
escapes it. URLs from responses (html_url and friends) are ignored.
"""

import re
from datetime import datetime, timedelta, timezone

from . import github, schema
from .paths import EXAMPLE_CONFIG_PATH, REPO_ROOT  # noqa: F401  (REPO_ROOT re-exported for callers)

DEFAULT_COMMIT_WINDOW_DAYS = 30
DEFAULT_MAX_REQUESTS = 500
MAX_REQUESTS_LIMIT = 5000
DEFAULT_MAX_REPOSITORIES = 500
DEFAULT_README_BYTES = schema.MAX_README
RELEASES_PER_PAGE = 30
PER_PAGE = 100
MAX_REPORT_ERRORS = 100
# Items kept per listing: the run format's own caps, so validate_run cannot be exceeded by a listing.
LIST_CAPS = {"search": schema.MAX_IDS, "forks": schema.MAX_IDS, "commits": schema.MAX_COMMITS, "releases": schema.MAX_RELEASES}
METADATA_EXPECTED = ("fork", "archived", "stargazers_count", "default_branch")
EMPTY_REPOSITORY_STATUS = 409

_SPACE_RE = re.compile(r"\s+")
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9_\-]+")

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

CONFIG_FILE_ERROR = "config: file is not strict JSON (syntax error, duplicate key, fractional or non-finite number)"


class ShapeError(github.ResponseError):
    """A response item does not have the fields the endpoint promises."""


class InternalError(github.GitHubError):
    """The adapter produced a run the strict loader refused. Fixed message, no details."""

    def __init__(self):
        github.GitHubError.__init__(self, "internal error: the collector produced an invalid run; nothing was written")


# --- configuration ------------------------------------------------------------


def _count_unknown(obj, fields, path):
    if isinstance(obj, dict):
        unknown = set(obj) - set(fields)
        if unknown:
            raise schema.SchemaError("%s: %d unknown field(s)" % (path, len(unknown)))


def validate_config(config):
    """Check a config dict. Error messages name paths and counts, never values or keys from the file."""
    if not isinstance(config, dict):
        raise schema.SchemaError("config: expected an object")
    _count_unknown(config, CONFIG.fields, "config")
    _count_unknown(config.get("page_caps"), PAGE_CAPS.fields, "config.page_caps")
    roots = config.get("roots")
    if isinstance(roots, list):
        for i, root in enumerate(roots):
            _count_unknown(root, ROOT_CONFIG.fields, "config.roots[%d]" % i)
    CONFIG.check(config, "config")
    if not config["queries"] and not config["roots"]:
        raise schema.SchemaError("config: at least one query or one root is required")
    for i, query in enumerate(config["queries"]):
        if "://" in query or schema._SCHEME_RE.match(query) is not None:
            raise schema.SchemaError("config.queries[%d]: a search query must not contain a URL" % i)
    queries = [_normalise_name(q) for q in config["queries"]]
    if len(set(queries)) != len(queries):
        raise schema.SchemaError("config.queries: duplicate query")
    names = [_normalise_name(r["full_name"]) for r in config["roots"]]
    if len(set(names)) != len(names):
        raise schema.SchemaError("config.roots: duplicate full_name")
    ids = [r["repository_id"] for r in config["roots"]]
    if len(set(ids)) != len(ids):
        raise schema.SchemaError("config.roots: duplicate repository_id")
    config.setdefault("commit_window_days", DEFAULT_COMMIT_WINDOW_DAYS)
    config.setdefault("max_requests", DEFAULT_MAX_REQUESTS)
    config.setdefault("max_repositories", DEFAULT_MAX_REPOSITORIES)
    config.setdefault("readme_bytes", DEFAULT_README_BYTES)
    merged = dict(github.DEFAULT_PAGE_CAPS)
    merged.update(config.get("page_caps") or {})
    config["page_caps"] = merged
    return config


def load_config(path):
    try:
        data = schema.read_json(path)
    except schema.SchemaError:
        raise schema.SchemaError(CONFIG_FILE_ERROR) from None
    return validate_config(data)


def _normalise_name(value):
    return _SPACE_RE.sub(" ", value.strip()).lower()


def _tokens(value):
    """Lower-cased word tokens of a name or query; search qualifiers (words with ':') are not tokens."""
    tokens = set()
    for word in _normalise_name(value).split(" "):
        if ":" in word:
            continue
        tokens.update(t for t in _TOKEN_SPLIT_RE.split(word) if t)
    return tokens


def example_placeholders(path=EXAMPLE_CONFIG_PATH):
    """(placeholder tokens, root ids) of the committed example config.

    Tokens are the owner and name parts of every example root and every
    word of every example query, so a placeholder is recognised in any
    position of a query or a root name.
    """
    example = validate_config(schema.read_json(path))
    tokens = set()
    for root in example["roots"]:
        tokens.update(_tokens(root["full_name"]))
    for query in example["queries"]:
        tokens.update(_tokens(query))
    ids = set(r["repository_id"] for r in example["roots"])
    return tokens, ids


def example_names(path=EXAMPLE_CONFIG_PATH):
    return example_placeholders(path)[0]


def config_uses_example_names(config, path=EXAMPLE_CONFIG_PATH):
    """Entries of `config` that use a placeholder: an example token in a query or root name, or an example root id."""
    tokens, ids = example_placeholders(path)
    found = []
    for root in config["roots"]:
        if root["repository_id"] in ids or _tokens(root["full_name"]) & tokens:
            found.append(root["full_name"])
    for query in config["queries"]:
        if _tokens(query) & tokens:
            found.append(query)
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
    """Metadata fields of GET /repositories/{id} in run-format shape, plus notes about softened or missing fields."""
    if _id(data, "id", path) != rid:
        raise ShapeError("response for %s names another repository" % path)
    full_name = _full_name(data, path)
    owner = _field(data, "owner", (dict,), path, nullable=True)
    login = _field(owner, "login", (str,), path, nullable=True) if owner is not None else None
    if login is not None and login != full_name.split("/")[0]:
        raise ShapeError("response for %s has an owner that does not match full_name" % path)
    parent = _field(data, "parent", (dict,), path, nullable=True)
    source = _field(data, "source", (dict,), path, nullable=True)
    notes = []
    for name in METADATA_EXPECTED:
        if data.get(name) is None:
            notes.append("missing:" + name)
    stars = _field(data, "stargazers_count", (int,), path, nullable=True)
    if stars is not None and (stars < 0 or stars > schema.MAX_SAFE_INT):
        stars = None
        notes.append("range:stargazers_count")
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
    }, notes


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
    def __init__(self, reason, requested):
        Exception.__init__(self, reason)
        self.reason = reason
        self.requested = requested


_STOPS = (github.RateLimited, github.BudgetExhausted, github.NetworkFailed)
_FAILURES = (github.ResponseError, github.TransportFailed, github.TransportRefused)
# Reasons that end the whole run; the search bucket running out does not.
_RUN_STOPS = ("rate_limited", "budget", "network")
SEARCH_STOP = "search_rate_limited"


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
        "search_stopped": False,
        "errors": [],
        "error_count": 0,
        "dropped_items": 0,
        "not_fetched_repository_ids": [],
        "candidates_truncated": False,
    }
    names = {}
    candidates = set()
    root_ids = set(r["repository_id"] for r in config["roots"])

    def note_error(path, reason):
        report["error_count"] += 1
        if len(report["errors"]) < MAX_REPORT_ERRORS:
            report["errors"].append({"path": path, "reason": reason})

    def attempt(path, action, *args, **kwargs):
        """One request. Returns (value, None) or (None, failure kind); raises _Stop on a run stop."""
        before = client.requests_made
        try:
            return action(*args, **kwargs), None
        except _STOPS as exc:
            raise _Stop(github._stop_reason(exc), client.requests_made > before)
        except _FAILURES as exc:
            kind = _failure_kind(exc)
            note_error(path, kind)
            return None, kind

    def capped(items, kind):
        """Items within the run format's cap; the overflow counts as dropped."""
        cap = LIST_CAPS[kind]
        if len(items) > cap:
            report["dropped_items"] += len(items) - cap
            return items[:cap], True
        return items, False

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
        """A run stop seen mid-walk ends the run; a search-bucket stop is noted; other interruptions are errors."""
        if pages.stopped in _RUN_STOPS:
            raise _Stop(pages.stopped, True)
        if pages.stopped == SEARCH_STOP:
            report["search_stopped"] = True
            return
        if pages.stopped is not None:
            note_error(path, pages.stopped)

    try:
        for query in config["queries"]:
            entry = {"query": query, "complete": False, "repository_ids": []}
            run["searches"].append(entry)
            if report["search_stopped"]:
                # The search bucket is exhausted: do not ask, record where to resume.
                entry["resume_cursor"] = "page=1"
                continue
            try:
                pages, failed = attempt("/search/repositories", client.paginate, "/search/repositories", {"q": query, "per_page": PER_PAGE}, caps["search"], "object", "items")
            except _Stop as stop:
                if stop.reason != SEARCH_STOP:
                    raise
                report["search_stopped"] = True
                entry["resume_cursor"] = "page=1"
                continue
            if failed or pages.items is None:
                if not failed:
                    after_pages("/search/repositories", pages)
                    if pages.stopped == SEARCH_STOP:
                        entry["resume_cursor"] = "page=1"
                    elif pages.stopped is None:
                        note_error("/search/repositories", "status")
                continue
            before = report["dropped_items"]
            items, overflow = capped(pages.items, "search")
            ids = listed_ids(items, "/search/repositories")
            entry["repository_ids"] = sorted(set(ids))[: schema.MAX_IDS]
            candidates.update(entry["repository_ids"])
            total = pages.extra.get("total_count")
            incomplete = pages.extra.get("incomplete_results")
            clean = report["dropped_items"] == before
            # total_count above the distinct ids listed covers the API's 1000-result ceiling too.
            capped_total = isinstance(total, int) and not isinstance(total, bool) and total > len(entry["repository_ids"])
            entry["complete"] = bool(pages.complete and clean and not overflow and incomplete is not True and not capped_total)
            if not pages.complete and pages.next_cursor is not None:
                entry["resume_cursor"] = pages.next_cursor
            after_pages("/search/repositories", pages)

        for root in config["roots"]:
            rid, full_name = root["repository_id"], root["full_name"]
            names.setdefault(rid, full_name)
            candidates.add(rid)
            entry = {"repository_id": rid, "full_name": full_name, "forks_scan_complete": False, "found_repository_ids": []}
            run["roots"].append(entry)
            path = github.path_for_repo(full_name, "/forks")
            pages, failed = attempt(path, client.paginate, path, {"per_page": PER_PAGE}, caps["forks"], repository_id=rid)
            if failed or pages.items is None:
                if not failed:
                    after_pages(path, pages)
                    if pages.stopped is None:
                        note_error(path, "status")
                continue
            before = report["dropped_items"]
            items, overflow = capped(pages.items, "forks")
            found = listed_ids(items, path)
            entry["found_repository_ids"] = list(dict.fromkeys(found))[: schema.MAX_IDS]
            entry["forks_scan_complete"] = bool(pages.complete and report["dropped_items"] == before and not overflow)
            if not entry["forks_scan_complete"] and pages.next_cursor is not None:
                entry["resume_cursor"] = pages.next_cursor
            candidates.update(entry["found_repository_ids"])
            after_pages(path, pages)

        ordered = sorted(candidates)
        if len(ordered) > config["max_repositories"]:
            report["candidates_truncated"] = True
            report["not_fetched_repository_ids"].extend(ordered[config["max_repositories"] :])
            ordered = ordered[: config["max_repositories"]]
        since = _since(run_at, config["commit_window_days"])
        for rid in ordered:
            full_name = names[rid]
            path = "/repositories/%d" % rid
            try:
                result, failed = attempt(path, client.get_json, path, None, "object")
            except _Stop as stop:
                if stop.requested:
                    # The request was made and refused: that is a real failure state.
                    state = "rate_limited" if stop.reason in ("rate_limited", SEARCH_STOP) else "failed"
                    run["repositories"].append(_empty_repository(rid, full_name, state))
                raise
            if failed or result.status != 200:
                run["repositories"].append(_empty_repository(rid, full_name, "failed" if failed else fetch_state_for(result.status)))
                continue
            try:
                meta, notes = adapt_repository(result.data, rid, path)
            except github.ResponseError:
                note_error(path, "shape")
                run["repositories"].append(_empty_repository(rid, full_name, "failed"))
                continue
            for note in notes:
                note_error(path, note)
            repo = _empty_repository(rid, meta["full_name"], "ok")
            repo.update(meta)
            run["repositories"].append(repo)
            branch = meta["default_branch"]
            if branch is not None:
                cpath = github.path_for_repo(meta["full_name"], "/commits")
                pages, failed = attempt(cpath, client.paginate, cpath, {"sha": branch, "since": since, "per_page": PER_PAGE}, caps["commits"], repository_id=rid)
                if not failed and pages.items is not None:
                    items, overflow = capped(pages.items, "commits")
                    commits, dropped = adapt_commits(items, cpath)
                    report["dropped_items"] += dropped
                    repo["commits"] = commits
                    repo["commits_complete"] = bool(pages.complete and dropped == 0 and not overflow)
                    after_pages(cpath, pages)
                elif not failed and pages.status == EMPTY_REPOSITORY_STATUS:
                    # GitHub answers 409 for a repository without commits: an empty, complete listing.
                    repo["commits"] = []
                    repo["commits_complete"] = True
                elif not failed:
                    after_pages(cpath, pages)
                    if pages.stopped is None:
                        note_error(cpath, "status")
            rpath = github.path_for_repo(meta["full_name"], "/releases")
            result, failed = attempt(rpath, client.get_json, rpath, {"per_page": RELEASES_PER_PAGE}, "array")
            if not failed and result.status == 200:
                items, overflow = capped(result.data, "releases")
                repo["releases"], dropped = adapt_releases(items, rpath)
                report["dropped_items"] += dropped
                if overflow:
                    note_error(rpath, "overflow")
            elif not failed:
                note_error(rpath, "status")
            dpath = github.path_for_repo(meta["full_name"], "/readme")
            result, failed = attempt(dpath, client.get_text, dpath, None, config["readme_bytes"])
            if not failed and result.status == 200:
                repo["readme_excerpt"] = result.data
            elif not failed and result.status != 404:
                note_error(dpath, "status")
    except _Stop as stop:
        # The search phase handles its own bucket; a search limit raised anywhere else ends the run.
        report["stopped"] = "rate_limited" if stop.reason == SEARCH_STOP else stop.reason

    if report["stopped"] is None and report["search_stopped"]:
        report["stopped"] = SEARCH_STOP
    listed = set(r["repository_id"] for r in run["repositories"])
    unfetched = set(report["not_fetched_repository_ids"]) | ((set(candidates) | root_ids) - listed)
    report["not_fetched_repository_ids"] = sorted(unfetched)[: schema.MAX_IDS]
    run["repositories"].sort(key=lambda r: r["repository_id"])
    complete = (
        report["stopped"] is None
        and report["error_count"] == 0
        and report["dropped_items"] == 0
        and not report["not_fetched_repository_ids"]
        and all(s["complete"] for s in run["searches"])
        and all(r["forks_scan_complete"] for r in run["roots"])
        and all(r["fetch_state"] == "ok" and r["commits_complete"] is not False for r in run["repositories"])
    )
    run["collection_report"] = {
        "complete": complete,
        "stopped": report["stopped"],
        "error_count": min(report["error_count"], schema.MAX_SAFE_INT),
        "dropped_items": min(report["dropped_items"], schema.MAX_SAFE_INT),
        "not_fetched": len(report["not_fetched_repository_ids"]),
        "requests_made": client.requests_made,
    }
    try:
        run = schema.validate_run(run)
    except schema.SchemaError:
        raise InternalError() from None
    report["complete"] = complete
    report["requests"] = list(client.log)
    report["client"] = client.summary()
    return run, report

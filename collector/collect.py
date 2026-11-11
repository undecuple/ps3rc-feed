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
   A code query ({"kind": "code", "q": ...}) is GET /search/code instead
   (per_page 100, page cap `page_caps.code`, 1 by default, at most 3). It
   draws from GitHub's code_search bucket (about 10 a minute), handled
   like the search bucket but separately (code_search_rate_limited), so
   either running out leaves the other kind of search running. A
   secondary limit on code search (403/429 with retry-after, or a 429
   while the bucket still has requests left) is waited out and retried
   up to twice (github.Client: each wait at most 30 seconds, all waits of
   a run at most 60 seconds, each retry counted in the budget); when it
   persists, that code search is complete=false with a resume cursor, later code
   queries are not asked (resume cursor "page=1"), the report counts them
   in code_search_limited, and the repository searches, fork walks and
   fetches go on with `stopped` left null. Each item
   is a file; its `repository` gives the id (no usable id: the item is
   dropped) and the name and description `require_any` is matched
   against. A repository listed for several files is one id, and an id
   from any search or fork walk is one candidate. An item whose
   repository has no usable full_name keeps its id only: it is fetched
   when another listing or the config names it, and is otherwise reported
   not fetched. total_count counts files, so it is compared with the
   items listed. The search entry carries "kind": "code": discovery
   provenance (the repository holds a matching file), nothing more.
   Code search needs a token: without one a code query is not asked, its
   entry says skipped "no_token" and the run is incomplete
   (collection_report.skipped_searches).
2. Roots: GET /repos/{owner}/{repo}/forks per configured root (per_page
   100, page cap). A capped or interrupted walk sets forks_scan_complete
   false and a resume_cursor "page=N".
   A query may carry `require_any` terms: a search item is then kept only
   when its full_name or its description (from the search item itself, so
   no extra request) contains one of the terms, case-insensitively as a
   plain substring. Ids left out are counted in the search entry's
   `filtered_out` and the report's `filtered_out`, and are not recorded.
3. Candidates: the union of search ids, fork ids, root ids and the config's
   `include` ids, minus the config's `deny` list (repositories or owners,
   by the name the listing gave; counted in the report's `denied`, removed
   from the run's listings; roots and included ids are never denied),
   in fetch order (see fetch_order), bounded by max_repositories. For each: GET /repositories/{id} (by id,
   so renames are followed), then GET .../commits?sha=<default branch>&
   since=<run_at minus the window>&per_page=100 (page cap; a cap sets
   commits_complete false; a 409 is GitHub's answer for an empty
   repository but can also mean the repository is temporarily
   unavailable, so it is recorded as commits [] with commits_complete
   false and no error: zero commits is never taken as certain from it.
   Such repositories are counted in collection_report.commits_unavailable
   and, unlike every other incomplete listing, do not make the run
   incomplete, so a truly empty repository cannot keep every run
   incomplete), GET .../releases?per_page=30
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
import unicodedata
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
# A commits listing answered with this status is recorded as [] with commits_complete false and counted in
# collection_report.commits_unavailable; it does not make the run incomplete (see step 3).
EMPTY_REPOSITORY_STATUS = 409

_SPACE_RE = re.compile(r"\s+")
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9_\-]+")

MAX_TERMS = 50
MAX_TERM = 64
MAX_DENY_REPOSITORIES = 500
MAX_DENY_OWNERS = 200
MAX_INCLUDE = 200


class Term(schema.Type):
    """A require_any term: 1-64 characters, no control or format characters, no whitespace but
    single inner spaces, no leading or trailing space. Matched as a plain case-insensitive substring."""

    def check(self, value, path):
        schema.Str(max_len=MAX_TERM).check(value, path)
        if (
            not value
            or value != value.strip()
            or "  " in value
            or any(unicodedata.category(c)[0] in ("C", "Z") and c != " " for c in value)
        ):
            raise schema.SchemaError("%s: a term must be 1-%d printable characters, single inner spaces only" % (path, MAX_TERM))
        return value


FILTERED_QUERY = schema.Obj({"q": schema.QUERY, "require_any": schema.List(Term(), MAX_TERMS)})
# A code search: {"kind": "code", "q": query}, optionally with require_any (matched like a repository query's).
CODE_QUERY = schema.Obj(
    {"kind": schema.Enum(("code",)), "q": schema.QUERY, "require_any": schema.List(Term(), MAX_TERMS)},
    optional=("require_any",),
)


def _query_shape(entry):
    """The schema of a query object: a code query when it has a `kind`, else a filtered repository query."""
    return CODE_QUERY if "kind" in entry else FILTERED_QUERY


class QueryEntry(schema.Type):
    """A search query: a plain string, {"q": query, "require_any": [term, ...]}, or {"kind": "code", "q": query}."""

    def check(self, value, path):
        if isinstance(value, str):
            return schema.QUERY.check(value, path)
        if isinstance(value, dict):
            return _query_shape(value).check(value, path)
        raise schema.SchemaError("%s: expected a query string or a query object, got %s" % (path, schema._kind(value)))


ROOT_CONFIG = schema.Obj({"repository_id": schema.PositiveId(), "full_name": schema.FULL_NAME})
PAGE_CAPS = schema.Obj(
    {"search": schema.Int(1, 10), "code": schema.Int(1, 3), "forks": schema.Int(1, 100), "commits": schema.Int(1, 50)},
    optional=("search", "code", "forks", "commits"),
)
DENY = schema.Obj(
    {
        "repositories": schema.List(schema.FULL_NAME, MAX_DENY_REPOSITORIES),
        "owners": schema.List(schema.OWNER, MAX_DENY_OWNERS),
    }
)
CONFIG = schema.Obj(
    {
        "queries": schema.List(QueryEntry(), 50),
        "roots": schema.List(ROOT_CONFIG, 200),
        "include": schema.List(ROOT_CONFIG, MAX_INCLUDE),
        "deny": DENY,
        "commit_window_days": schema.Int(1, 365),
        "max_requests": schema.Int(1, MAX_REQUESTS_LIMIT),
        "max_repositories": schema.Int(1, 5000),
        "readme_bytes": schema.Int(256, schema.MAX_README),
        "page_caps": PAGE_CAPS,
        "_comment": schema.Str(max_len=1024),
    },
    optional=("include", "deny", "commit_window_days", "max_requests", "max_repositories", "readme_bytes", "page_caps", "_comment"),
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
    _count_unknown(config.get("deny"), DENY.fields, "config.deny")
    for key in ("roots", "include"):
        entries = config.get(key)
        if isinstance(entries, list):
            for i, entry in enumerate(entries):
                _count_unknown(entry, ROOT_CONFIG.fields, "config.%s[%d]" % (key, i))
    queries = config.get("queries")
    if isinstance(queries, list):
        for i, query in enumerate(queries):
            if isinstance(query, dict):
                _count_unknown(query, _query_shape(query).fields, "config.queries[%d]" % i)
    CONFIG.check(config, "config")
    if not config["queries"] and not config["roots"]:
        raise schema.SchemaError("config: at least one query or one root is required")
    for i, entry in enumerate(config["queries"]):
        query = query_text(entry)
        if "://" in query or schema._SCHEME_RE.match(query) is not None:
            raise schema.SchemaError("config.queries[%d]: a search query must not contain a URL" % i)
        if query_terms(entry) is not None:
            terms = [t.casefold() for t in query_terms(entry)]
            if not terms:
                raise schema.SchemaError("config.queries[%d].require_any: at least one term is required" % i)
            if len(set(terms)) != len(terms):
                raise schema.SchemaError("config.queries[%d].require_any: duplicate term" % i)
    queries = [_normalise_name(query_text(q)) for q in config["queries"]]
    if len(set(queries)) != len(queries):
        raise schema.SchemaError("config.queries: duplicate query")
    include = config.setdefault("include", [])
    deny = config.setdefault("deny", {"repositories": [], "owners": []})
    for key, entries in (("roots", config["roots"]), ("include", include)):
        names = [_normalise_name(r["full_name"]) for r in entries]
        if len(set(names)) != len(names):
            raise schema.SchemaError("config.%s: duplicate full_name" % key)
        ids = [r["repository_id"] for r in entries]
        if len(set(ids)) != len(ids):
            raise schema.SchemaError("config.%s: duplicate repository_id" % key)
    root_ids = set(r["repository_id"] for r in config["roots"])
    root_names = set(r["full_name"].lower() for r in config["roots"])
    if any(r["repository_id"] in root_ids or r["full_name"].lower() in root_names for r in include):
        raise schema.SchemaError("config.include: a root is already a candidate; do not include it")
    for key in ("repositories", "owners"):
        values = [v.lower() for v in deny[key]]
        if len(set(values)) != len(values):
            raise schema.SchemaError("config.deny.%s: duplicate entry" % key)
    for key, entries in (("roots", config["roots"]), ("include", include)):
        if any(is_denied(r["full_name"], deny) for r in entries):
            raise schema.SchemaError("config.deny: names an entry of config.%s" % key)
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


def query_text(entry):
    """The search string of a query entry (a plain string or a query object)."""
    return entry["q"] if isinstance(entry, dict) else entry


def query_terms(entry):
    """The require_any terms of a query entry, or None for an unfiltered query."""
    return entry.get("require_any") if isinstance(entry, dict) else None


def query_kind(entry):
    """The kind of a query entry: "code" (GET /search/code) or "repositories" (GET /search/repositories)."""
    return "code" if isinstance(entry, dict) and entry.get("kind") == "code" else "repositories"


SEARCH_PATHS = {"repositories": "/search/repositories", "code": github.CODE_SEARCH_PATH}
SEARCH_CAPS = {"repositories": "search", "code": "code"}


def is_relevant(full_name, description, terms):
    """True when the name or the description contains one of `terms` (case-insensitive substring).

    The name and the description are matched separately, so a term never
    spans the two. The description is cut to the run format's bound first,
    so the decision can be checked again against the stored description.
    """
    name = full_name.casefold()
    text = description[: schema.MAX_DESCRIPTION].casefold() if isinstance(description, str) else ""
    for term in terms:
        term = term.casefold()
        if term in name or term in text:
            return True
    return False


def is_denied(full_name, deny):
    """True when `full_name` or its owner is on the config's deny lists (case-insensitive)."""
    name = full_name.lower()
    if name in set(r.lower() for r in deny["repositories"]):
        return True
    return name.split("/")[0] in set(o.lower() for o in deny["owners"])


# Qualifiers whose value names something (a file, a path, a repository or an account): their values are
# tokens, so a placeholder in `filename:example-...` is found. Other qualifiers (in:name, fork:true) are not.
NAMING_QUALIFIERS = ("filename", "path", "repo", "user", "org")


def _tokens(value):
    """Lower-cased word tokens of a name or query; search qualifiers (words with ':') are not tokens,
    except the values of NAMING_QUALIFIERS."""
    tokens = set()
    for word in _normalise_name(value).split(" "):
        if ":" in word:
            key, word = word.split(":", 1)
            if key not in NAMING_QUALIFIERS:
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
    for root in example["roots"] + example["include"]:
        tokens.update(_tokens(root["full_name"]))
    for query in example["queries"]:
        tokens.update(_tokens(query_text(query)))
        for term in query_terms(query) or ():
            tokens.update(_tokens(term))
    for name in example["deny"]["repositories"] + example["deny"]["owners"]:
        tokens.update(_tokens(name))
    ids = set(r["repository_id"] for r in example["roots"] + example["include"])
    return tokens, ids


def example_names(path=EXAMPLE_CONFIG_PATH):
    return example_placeholders(path)[0]


def config_uses_example_names(config, path=EXAMPLE_CONFIG_PATH):
    """Entries of `config` that use a placeholder: an example token in a query, a require_any term, a root,
    include or deny name, or an example root or include id."""
    tokens, ids = example_placeholders(path)
    found = []
    for root in config["roots"] + config.get("include", []):
        if root["repository_id"] in ids or _tokens(root["full_name"]) & tokens:
            found.append(root["full_name"])
    for query in config["queries"]:
        words = _tokens(query_text(query))
        for term in query_terms(query) or ():
            words |= _tokens(term)
        if words & tokens:
            found.append(query_text(query))
    deny = config.get("deny") or {"repositories": [], "owners": []}
    for name in deny["repositories"] + deny["owners"]:
        if _tokens(name) & tokens:
            found.append(name)
    return found


FETCH_ORDER = ("roots_and_include_by_id", "new_newest_id_first", "known_oldest_attempt_first")


def fetch_order(candidates, priority_ids, last_attempted=None):
    """The order candidates are fetched in; max_repositories cuts this list.

    1. Roots and `include` entries (`priority_ids`), ascending id: the config
       names them, so they always come first.
    2. Candidates never attempted before (absent from `last_attempted`),
       newest (highest) id first, so a new repository is never the one cut.
    3. Known candidates, oldest last attempt first, ties by ascending id: a
       repository left out of one run is among the first of the next, so the
       cut rotates and every candidate is reached within a few runs.

    `last_attempted` maps repository id to the timestamp of its last
    attempted refresh (`last_attempted_from_state`); None means no previous
    state, so every candidate is new and the order is newest id first.
    The order depends only on the arguments: no clock, no hash order.
    """
    known = last_attempted or {}
    first = sorted(rid for rid in candidates if rid in priority_ids)
    rest = [rid for rid in candidates if rid not in priority_ids]
    new = sorted((rid for rid in rest if rid not in known), reverse=True)
    old = sorted((rid for rid in rest if rid in known), key=lambda rid: (known[rid], rid))
    return first + new + old


def last_attempted_from_state(state):
    """{repository id: last_attempted_refresh_at} of a validated store state (store.load_state).

    Every observation in the store comes from a run that listed the
    repository (fetched, failed or rate limited), so this is the set of
    repositories attempted before and when each was last attempted.
    """
    return {int(key): obs["last_attempted_refresh_at"] for key, obs in state["repositories"].items()}


def plan_requests(config, authenticated=True):
    """The requests a run starts with; later ones depend on what these return.

    Without a token (`authenticated` false) code queries are left out: code
    search refuses unauthenticated requests, and collect() skips them too.
    """
    plan = []
    for query in config["queries"]:
        kind = query_kind(query)
        if kind == "code" and not authenticated:
            continue
        plan.append({"method": "GET", "path": SEARCH_PATHS[kind], "query": github.canonical_query({"q": query_text(query), "per_page": PER_PAGE, "page": 1})})
    for root in config["roots"]:
        plan.append({"method": "GET", "path": github.path_for_repo(root["full_name"], "/forks"), "query": github.canonical_query({"per_page": PER_PAGE, "page": 1})})
    for root in config["roots"] + config.get("include", []):
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


_STOPS = (github.RateLimited, github.BudgetExhausted, github.NetworkFailed, github.Unauthorized)
_FAILURES = (github.ResponseError, github.TransportFailed, github.TransportRefused)
# Reasons that end the whole run; a search bucket running out does not.
_RUN_STOPS = ("rate_limited", "budget", "network", "unauthorized")
SEARCH_STOP = "search_rate_limited"
CODE_SEARCH_STOP = "code_search_rate_limited"
SEARCH_STOPS = (SEARCH_STOP, CODE_SEARCH_STOP)
# A code search secondary limit: ends code search for this run, nothing else.
CODE_SECONDARY = github.CODE_SEARCH_SECONDARY
# The bucket stop that ends the searches of a kind.
KIND_STOPS = {"repositories": SEARCH_STOP, "code": CODE_SEARCH_STOP}


def _failure_kind(exc):
    if isinstance(exc, github.TransportFailed):
        return "transport"
    if isinstance(exc, ShapeError):
        return "shape"
    return "response"


def collect(client, config, run_at, last_attempted=None):
    """Return (run, report). The run passes schema.validate_run; the report says what did not happen.

    `last_attempted` ({repository id: timestamp}, see last_attempted_from_state)
    sets the fetch order of the candidates (fetch_order); None orders them
    newest id first after the roots and included repositories.
    """
    config = validate_config(dict(config))
    schema.TIMESTAMP.check(run_at, "run_at")
    caps = config["page_caps"]
    run = {"run_at": run_at, "roots": [], "searches": [], "repositories": []}
    report = {
        "stopped": None,
        # Search bucket stops seen (search_rate_limited, code_search_rate_limited).
        "search_stops": set(),
        "skipped_searches": 0,
        # Code searches cut short or not asked because of a code search secondary limit.
        "code_search_limited": 0,
        "errors": [],
        "error_count": 0,
        "dropped_items": 0,
        "not_fetched_repository_ids": [],
        "candidates_truncated": False,
        "filtered_out": 0,
        "denied": 0,
    }
    # Repositories whose commits listing answered 409: their commits_complete false does not make the run incomplete.
    commits_unavailable = set()
    names = {}
    candidates = set()
    # Code search ids whose item gave no usable full_name: fetched only when another listing names them.
    unnamed = set()
    root_ids = set(r["repository_id"] for r in config["roots"])
    authenticated = getattr(client, "token", None) is not None

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

    def listed_ids(items, path, terms=None):
        """(ids kept, distinct ids listed). With `terms`, an item whose name and description
        contain none of them is not kept (and its name is not recorded)."""
        ids = []
        listed = set()
        for item in items:
            try:
                rid, full_name = _listed_repository(item, path)
            except github.ResponseError:
                report["dropped_items"] += 1
                continue
            listed.add(rid)
            if terms is not None and not is_relevant(full_name, item.get("description"), terms):
                continue
            ids.append(rid)
            names.setdefault(rid, full_name)
        return ids, listed

    def code_ids(items, path, terms=None):
        """(ids kept, distinct ids listed) of code search items, by each item's `repository`.

        An item without a repository object or a valid repository id is
        dropped. An item whose repository has an unusable full_name keeps its
        id only (it is not named, so it is fetched only when another listing
        names it). With `terms`, the repository's name and description are
        matched as for a repository search.
        """
        ids = []
        listed = set()
        for item in items:
            repo = item.get("repository") if isinstance(item, dict) else None
            try:
                rid = _id(repo, "id", path)
            except github.ResponseError:
                report["dropped_items"] += 1
                continue
            try:
                full_name = _full_name(repo, path)
            except github.ResponseError:
                full_name = None
            listed.add(rid)
            if terms is not None and not is_relevant(full_name or "", repo.get("description"), terms):
                continue
            ids.append(rid)
            if full_name is None:
                unnamed.add(rid)
            else:
                names.setdefault(rid, full_name)
        return ids, listed

    def after_pages(path, pages):
        """A run stop seen mid-walk ends the run; a search-bucket stop is noted; other interruptions are errors."""
        if pages.stopped == CODE_SECONDARY and path == github.CODE_SEARCH_PATH:
            report["code_search_limited"] += 1
            return
        if pages.stopped in _RUN_STOPS:
            raise _Stop(pages.stopped, True)
        if pages.stopped in SEARCH_STOPS:
            report["search_stops"].add(pages.stopped)
            return
        if pages.stopped is not None:
            note_error(path, pages.stopped)

    def apply_deny():
        """Remove denied candidates (by the name the listing gave) before anything is fetched.

        Roots and included repositories are never denied. A denied id is
        also taken out of the search and fork listings in the run, so it is
        not named anywhere and is not left pending.
        """
        denied = set(rid for rid in candidates if rid not in root_ids and rid not in include_ids and is_denied(names[rid], config["deny"]))
        if denied:
            candidates.difference_update(denied)
            report["denied"] += len(denied)
            for entry in run["searches"]:
                entry["repository_ids"] = [i for i in entry["repository_ids"] if i not in denied]
            for entry in run["roots"]:
                entry["found_repository_ids"] = [i for i in entry["found_repository_ids"] if i not in denied]

    include_ids = set()
    for extra in config["include"]:
        names.setdefault(extra["repository_id"], extra["full_name"])
        include_ids.add(extra["repository_id"])

    try:
        for query_entry in config["queries"]:
            query, terms, kind = query_text(query_entry), query_terms(query_entry), query_kind(query_entry)
            path, bucket_stop = SEARCH_PATHS[kind], KIND_STOPS[kind]
            entry = {"query": query, "complete": False, "repository_ids": []}
            if kind == "code":
                entry["kind"] = "code"
            if terms is not None:
                entry["filtered_out"] = 0
            run["searches"].append(entry)
            if kind == "code" and not authenticated:
                # Code search refuses requests without a token: not asked, and the run is incomplete.
                entry["skipped"] = "no_token"
                entry["resume_cursor"] = "page=1"
                report["skipped_searches"] += 1
                continue
            if bucket_stop in report["search_stops"]:
                # This kind's bucket is exhausted: do not ask, record where to resume.
                entry["resume_cursor"] = "page=1"
                continue
            try:
                pages, failed = attempt(path, client.paginate, path, {"q": query, "per_page": PER_PAGE}, caps[SEARCH_CAPS[kind]], "object", "items")
            except _Stop as stop:
                if stop.reason == CODE_SECONDARY and kind == "code":
                    # Waited and retried in the client, or not retryable, or refused before the
                    # transport because code search already met the limit: this search only is incomplete.
                    report["code_search_limited"] += 1
                    entry["resume_cursor"] = "page=1"
                    continue
                if stop.reason not in SEARCH_STOPS:
                    raise
                report["search_stops"].add(stop.reason)
                entry["resume_cursor"] = "page=1"
                continue
            if failed or pages.items is None:
                if not failed:
                    after_pages(path, pages)
                    if pages.stopped in SEARCH_STOPS + (CODE_SECONDARY,):
                        entry["resume_cursor"] = "page=1"
                    elif pages.stopped is None:
                        note_error(path, "status")
                continue
            before = report["dropped_items"]
            items, overflow = capped(pages.items, "search")
            if kind == "code":
                ids, listed = code_ids(items, path, terms)
            else:
                ids, listed = listed_ids(items, path, terms)
            entry["repository_ids"] = sorted(set(ids))[: schema.MAX_IDS]
            if terms is not None:
                entry["filtered_out"] = len(listed) - len(set(ids))
                report["filtered_out"] += entry["filtered_out"]
            candidates.update(i for i in entry["repository_ids"] if i not in unnamed)
            total = pages.extra.get("total_count")
            incomplete = pages.extra.get("incomplete_results")
            clean = report["dropped_items"] == before
            # total_count above the distinct ids listed covers the API's 1000-result ceiling too.
            # Ids the filter left out were listed: they count here, so a filter never makes a search look cut short.
            # A code search counts files, several of which may be in one repository: compare with the items listed.
            seen = len(items) if kind == "code" else len(listed)
            capped_total = isinstance(total, int) and not isinstance(total, bool) and total > seen
            entry["complete"] = bool(pages.complete and clean and not overflow and incomplete is not True and not capped_total)
            if not pages.complete and pages.next_cursor is not None:
                entry["resume_cursor"] = pages.next_cursor
            after_pages(path, pages)

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
            found, _ = listed_ids(items, path)
            entry["found_repository_ids"] = list(dict.fromkeys(found))[: schema.MAX_IDS]
            entry["forks_scan_complete"] = bool(pages.complete and report["dropped_items"] == before and not overflow)
            if not entry["forks_scan_complete"] and pages.next_cursor is not None:
                entry["resume_cursor"] = pages.next_cursor
            candidates.update(entry["found_repository_ids"])
            after_pages(path, pages)

        # A code search id without a name is fetched when another listing (or the config) named it.
        candidates.update(rid for rid in unnamed if rid in names)
        apply_deny()
        candidates.update(include_ids)
        ordered = fetch_order(candidates, root_ids | include_ids, last_attempted)
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
                    state = "rate_limited" if stop.reason in ("rate_limited",) + SEARCH_STOPS else "failed"
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
                    # GitHub answers 409 for a repository without commits, but a 409 can also mean the
                    # repository is temporarily unavailable: an empty listing that is not known to be complete.
                    repo["commits"] = []
                    repo["commits_complete"] = False
                    commits_unavailable.add(rid)
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
        # The search phase handles its own buckets; a search limit raised anywhere else ends the run.
        report["stopped"] = "rate_limited" if stop.reason in SEARCH_STOPS + (CODE_SECONDARY,) else stop.reason

    if report["stopped"] is None and report["search_stops"]:
        # Both buckets may have run out: the repository search bucket is named first.
        report["stopped"] = SEARCH_STOP if SEARCH_STOP in report["search_stops"] else CODE_SEARCH_STOP
    report["search_stopped"] = bool(report["search_stops"])
    report["search_stops"] = sorted(report["search_stops"])
    # Code search ids nobody named are recorded in their search but never fetched.
    report["not_fetched_repository_ids"].extend(sorted(rid for rid in unnamed if rid not in names))
    # A stop before the candidates were chosen: still keep denied ids out, and count included ones as not fetched.
    apply_deny()
    listed = set(r["repository_id"] for r in run["repositories"])
    unfetched = set(report["not_fetched_repository_ids"]) | ((set(candidates) | root_ids | include_ids) - listed)
    report["not_fetched_repository_ids"] = sorted(unfetched)[: schema.MAX_IDS]
    run["repositories"].sort(key=lambda r: r["repository_id"])
    complete = (
        report["stopped"] is None
        and report["error_count"] == 0
        and report["dropped_items"] == 0
        and report["skipped_searches"] == 0
        and not report["not_fetched_repository_ids"]
        and all(s["complete"] for s in run["searches"])
        and all(r["forks_scan_complete"] for r in run["roots"])
        and all(
            r["fetch_state"] == "ok" and (r["commits_complete"] is not False or r["repository_id"] in commits_unavailable)
            for r in run["repositories"]
        )
    )
    run["collection_report"] = {
        "complete": complete,
        "stopped": report["stopped"],
        "error_count": min(report["error_count"], schema.MAX_SAFE_INT),
        "dropped_items": min(report["dropped_items"], schema.MAX_SAFE_INT),
        "not_fetched": len(report["not_fetched_repository_ids"]),
        "requests_made": client.requests_made,
        "filtered_out": min(report["filtered_out"], schema.MAX_SAFE_INT),
        "denied": report["denied"],
        "commits_unavailable": len(commits_unavailable),
    }
    if report["skipped_searches"]:
        run["collection_report"]["skipped_searches"] = report["skipped_searches"]
    try:
        run = schema.validate_run(run)
    except schema.SchemaError:
        raise InternalError() from None
    report["complete"] = complete
    report["requests"] = list(client.log)
    report["client"] = client.summary()
    return run, report

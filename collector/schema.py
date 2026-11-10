"""Strict JSON schemas for run files, persisted state and events.

Every object is checked against a fixed field list: a missing required
field, a value of the wrong type or an unknown field raises SchemaError
naming the path. Patterns use fullmatch, timestamps are parsed as real
dates, integers are bounded to the 2^53 range JSON consumers agree on, and
free text is cut to a fixed length at load time. JSON files are read with
duplicate keys, NaN/Infinity and floats rejected.

Renderer-facing fields of events (title, summary, game_title, every
string in details) must already be escaped: raw markup characters, raw
Discord mentions, control characters and non-GitHub URLs are rejected.
Repository text in observations is data and is only bounded.
"""

import json
import re
import unicodedata
from datetime import datetime

MAX_SAFE_INT = 2**53

MAX_DESCRIPTION = 2048
MAX_README = 16384
MAX_MESSAGE = 2048
MAX_TAG = 256
MAX_QUERY = 256
MAX_FULL_NAME = 200
MAX_OWNER = 100
MAX_RENDERED = 512
MAX_DETAILS_BYTES = 65536
MAX_DETAILS_ITEMS = 256
MAX_DETAILS_DEPTH = 3
MAX_COMMITS = 10000
MAX_RELEASES = 1000
MAX_REPOSITORIES = 10000
MAX_IDS = 10000
MAX_SOURCES = 64

TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
NAME_PART_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")
EVENT_ID_RE = re.compile(r"repo-[0-9]+:[a-z_]+(:[0-9a-f]+)?\Z")
SOURCE_URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
KEY_RE = re.compile(r"[a-z_]+\Z")
LABEL_RE = re.compile(r"[a-z0-9_-]+\Z")
DIGITS_RE = re.compile(r"[0-9]+\Z")
_SCHEME_RE = re.compile(r"(javascript|data|vbscript|file|ftp|http):", re.IGNORECASE)
_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)

FETCH_STATES = ("ok", "rate_limited", "not_found")
COLLECTION_STATES = ("ok", "partial", "failing", "stale", "not_found")


class SchemaError(ValueError):
    """Raised when a document does not match its strict schema."""


def _kind(value):
    if value is None:
        return "null"
    return type(value).__name__


class Type:
    def check(self, value, path):
        raise NotImplementedError


class Int(Type):
    """Integer within [lo, hi]; booleans are not integers here."""

    def __init__(self, lo=-MAX_SAFE_INT, hi=MAX_SAFE_INT):
        self.lo = lo
        self.hi = hi

    def check(self, value, path):
        if not isinstance(value, int) or isinstance(value, bool):
            raise SchemaError("%s: expected integer, got %s" % (path, _kind(value)))
        if value < self.lo or value > self.hi:
            raise SchemaError("%s: integer out of range" % path)
        return value


def PositiveId():
    return Int(1, MAX_SAFE_INT)


def NonNegInt():
    return Int(0, MAX_SAFE_INT)


class Bool(Type):
    def check(self, value, path):
        if not isinstance(value, bool):
            raise SchemaError("%s: expected boolean, got %s" % (path, _kind(value)))
        return value


class Str(Type):
    """String, optionally bounded and matched (fullmatch) against a pattern."""

    def __init__(self, pattern=None, max_len=None):
        self.pattern = pattern
        self.max_len = max_len

    def check(self, value, path):
        if not isinstance(value, str):
            raise SchemaError("%s: expected string, got %s" % (path, _kind(value)))
        if self.max_len is not None and len(value) > self.max_len:
            raise SchemaError("%s: string longer than %d" % (path, self.max_len))
        if self.pattern is not None and self.pattern.fullmatch(value) is None:
            raise SchemaError("%s: string does not match the expected form" % path)
        return value


class Text(Type):
    """Free text from an untrusted source: bounded by cutting, never by rejecting."""

    def __init__(self, max_len):
        self.max_len = max_len

    def check(self, value, path):
        if not isinstance(value, str):
            raise SchemaError("%s: expected string, got %s" % (path, _kind(value)))
        return value[: self.max_len]


class Timestamp(Type):
    def check(self, value, path):
        if not isinstance(value, str) or TIMESTAMP_RE.fullmatch(value) is None:
            raise SchemaError("%s: expected a UTC timestamp YYYY-MM-DDTHH:MM:SSZ" % path)
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise SchemaError("%s: not a real calendar date/time" % path) from None
        return value


class NamePart(Type):
    """One component of owner/name: allowed characters, never '.' or '..'."""

    def __init__(self, max_len):
        self.max_len = max_len

    def check(self, value, path):
        if not isinstance(value, str):
            raise SchemaError("%s: expected string, got %s" % (path, _kind(value)))
        if len(value) > self.max_len or NAME_PART_RE.fullmatch(value) is None or value in (".", ".."):
            raise SchemaError("%s: not a valid GitHub name component" % path)
        return value


class FullName(Type):
    def check(self, value, path):
        if not isinstance(value, str):
            raise SchemaError("%s: expected string, got %s" % (path, _kind(value)))
        parts = value.split("/")
        if len(value) > MAX_FULL_NAME or len(parts) != 2:
            raise SchemaError("%s: expected owner/name" % path)
        NamePart(MAX_OWNER).check(parts[0], path)
        NamePart(MAX_FULL_NAME).check(parts[1], path)
        return value


class Enum(Type):
    def __init__(self, values):
        self.values = tuple(values)

    def check(self, value, path):
        if not isinstance(value, str) or value not in self.values:
            raise SchemaError("%s: unexpected value" % path)
        return value


class Nullable(Type):
    def __init__(self, inner):
        self.inner = inner

    def check(self, value, path):
        if value is None:
            return None
        return self.inner.check(value, path)


class List(Type):
    def __init__(self, item, max_items=MAX_IDS):
        self.item = item
        self.max_items = max_items

    def check(self, value, path):
        if not isinstance(value, list):
            raise SchemaError("%s: expected list, got %s" % (path, _kind(value)))
        if len(value) > self.max_items:
            raise SchemaError("%s: more than %d items" % (path, self.max_items))
        for i, item in enumerate(value):
            value[i] = self.item.check(item, "%s[%d]" % (path, i))
        return value


class Obj(Type):
    """A fixed set of fields. `optional` fields may be absent; no others may be present."""

    def __init__(self, fields, optional=()):
        self.fields = fields
        self.optional = set(optional)

    def check(self, value, path):
        if not isinstance(value, dict):
            raise SchemaError("%s: expected object, got %s" % (path, _kind(value)))
        unknown = sorted(set(value) - set(self.fields))
        if unknown:
            raise SchemaError("%s: unknown field(s) %s" % (path, ", ".join(repr(u) for u in unknown)))
        for name, ftype in self.fields.items():
            if name not in value:
                if name in self.optional:
                    continue
                raise SchemaError("%s: missing required field %r" % (path, name))
            value[name] = ftype.check(value[name], "%s.%s" % (path, name))
        return value


class Map(Type):
    """An object with string keys of a given form and uniform values."""

    def __init__(self, value_type, key_pattern=None, max_items=MAX_IDS, max_key_len=MAX_QUERY):
        self.value_type = value_type
        self.key_pattern = key_pattern
        self.max_items = max_items
        self.max_key_len = max_key_len

    def check(self, value, path):
        if not isinstance(value, dict):
            raise SchemaError("%s: expected object, got %s" % (path, _kind(value)))
        if len(value) > self.max_items:
            raise SchemaError("%s: more than %d entries" % (path, self.max_items))
        for key in list(value):
            if len(key) > self.max_key_len:
                raise SchemaError("%s: key longer than %d" % (path, self.max_key_len))
            if self.key_pattern is not None and self.key_pattern.fullmatch(key) is None:
                raise SchemaError("%s: key %r has an unexpected form" % (path, key))
            value[key] = self.value_type.check(value[key], "%s[%r]" % (path, key))
        return value


class Rendered(Type):
    """Renderer-facing text that must already be escaped and bounded."""

    def __init__(self, max_len=MAX_RENDERED):
        self.max_len = max_len

    def check(self, value, path):
        if not isinstance(value, str):
            raise SchemaError("%s: expected string, got %s" % (path, _kind(value)))
        check_rendered_text(value, path, self.max_len)
        return value


def check_rendered_text(value, path, max_len=MAX_RENDERED):
    if len(value) > max_len:
        raise SchemaError("%s: rendered text longer than %d" % (path, max_len))
    for ch in value:
        if ch in "<>\"'":
            raise SchemaError("%s: rendered text contains raw markup character" % path)
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            raise SchemaError("%s: rendered text contains a control or format character" % path)
    if _MENTION_RE.search(value) is not None:
        raise SchemaError("%s: rendered text contains a raw mention" % path)
    if "://" in value and not value.startswith("https://github.com/"):
        raise SchemaError("%s: rendered text contains a non-GitHub URL" % path)
    if _SCHEME_RE.match(value) is not None:
        raise SchemaError("%s: rendered text starts with a URL scheme" % path)


class Details(Type):
    """Event details: a small JSON object of ids, flags, timestamps and rendered strings."""

    def check(self, value, path):
        if not isinstance(value, dict):
            raise SchemaError("%s: expected object, got %s" % (path, _kind(value)))
        self._walk(value, path, 0)
        try:
            size = len(json.dumps(value, ensure_ascii=True))
        except (TypeError, ValueError):
            raise SchemaError("%s: details are not plain JSON" % path) from None
        if size > MAX_DETAILS_BYTES:
            raise SchemaError("%s: details larger than %d bytes" % (path, MAX_DETAILS_BYTES))
        return value

    def _walk(self, value, path, depth):
        if depth > MAX_DETAILS_DEPTH:
            raise SchemaError("%s: details nested too deeply" % path)
        if isinstance(value, dict):
            if len(value) > MAX_DETAILS_ITEMS:
                raise SchemaError("%s: too many entries" % path)
            for key, item in value.items():
                if not isinstance(key, str) or KEY_RE.fullmatch(key) is None:
                    raise SchemaError("%s: key %r has an unexpected form" % (path, key))
                self._walk(item, "%s.%s" % (path, key), depth + 1)
        elif isinstance(value, list):
            if len(value) > MAX_DETAILS_ITEMS:
                raise SchemaError("%s: too many items" % path)
            for i, item in enumerate(value):
                self._walk(item, "%s[%d]" % (path, i), depth + 1)
        elif isinstance(value, str):
            check_rendered_text(value, path)
        elif isinstance(value, bool) or value is None:
            return
        elif isinstance(value, int):
            Int().check(value, path)
        else:
            raise SchemaError("%s: unexpected value type %s" % (path, _kind(value)))


# --- JSON reading ----------------------------------------------------------


def _pairs_hook(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise SchemaError("duplicate key %r in JSON object" % key)
        obj[key] = value
    return obj


def _reject_constant(name):
    raise SchemaError("non-finite number %s is not allowed" % name)


def _reject_float(text):
    raise SchemaError("fractional number %r is not allowed" % text)


def read_json(path):
    """Parse a JSON file strictly: no duplicate keys, no NaN/Infinity, no floats, bounded ints."""
    with open(path, "r", encoding="utf-8") as fh:
        try:
            return json.load(
                fh,
                object_pairs_hook=_pairs_hook,
                parse_constant=_reject_constant,
                parse_float=_reject_float,
            )
        except json.JSONDecodeError as exc:
            raise SchemaError("%s: not valid JSON (%s)" % (path, exc)) from None
        except ValueError as exc:
            # Includes an integer literal above the interpreter's digit limit.
            if isinstance(exc, SchemaError):
                raise SchemaError("%s: %s" % (path, exc)) from None
            raise SchemaError("%s: unreadable number (%s)" % (path, exc)) from None
        except RecursionError:
            raise SchemaError("%s: nested too deeply" % path) from None


# --- run files (what a fetch returned) ---------------------------------

TIMESTAMP = Timestamp()
SHA = Str(pattern=SHA_RE)
FULL_NAME = FullName()
OWNER = NamePart(MAX_OWNER)
# Operator search queries: GitHub search syntax characters only, no URLs.
QUERY_RE = re.compile(r"[A-Za-z0-9 _.:/+\-\"()@#]{1,%d}\Z" % MAX_QUERY)
QUERY = Str(pattern=QUERY_RE, max_len=MAX_QUERY)
# Git reference names, the subset safe to carry around: no ':', '..', '@{', control or space.
REF_RE = re.compile(r"(?!.*\.\.)(?!.*@\{)(?!.*/\.)(?!\.)[A-Za-z0-9][A-Za-z0-9._/+\-]{0,%d}\Z" % (MAX_TAG - 1))
# Opaque scan cursors as the fetch layer hands them back.
CURSOR_RE = re.compile(r"[A-Za-z0-9_=.:/+\-]{1,%d}\Z" % MAX_TAG)
PATH_RE = re.compile(r"[A-Za-z0-9_.\[\]]{1,128}\Z")
REJECTION_REASONS = ("invalid_ref", "invalid_cursor")

COMMIT = Obj({"sha": SHA, "committed_at": TIMESTAMP, "message": Text(MAX_MESSAGE)})
RELEASE = Obj({"id": PositiveId(), "tag": Text(MAX_TAG), "published_at": TIMESTAMP})

REPOSITORY = Obj(
    {
        "repository_id": PositiveId(),
        "full_name": FULL_NAME,
        "owner": OWNER,
        "description": Nullable(Text(MAX_DESCRIPTION)),
        "readme_excerpt": Nullable(Text(MAX_README)),
        "is_fork": Nullable(Bool()),
        "parent_repository_id": Nullable(PositiveId()),
        "network_root_repository_id": Nullable(PositiveId()),
        "archived": Nullable(Bool()),
        "stars": Nullable(NonNegInt()),
        "default_branch": Nullable(Text(MAX_TAG)),
        "fetch_state": Enum(FETCH_STATES),
        "commits_complete": Nullable(Bool()),
        # Commits are the branch window in chronological order: the last
        # element is the branch head unless `head_sha` names it.
        "commits": Nullable(List(COMMIT, MAX_COMMITS)),
        "releases": Nullable(List(RELEASE, MAX_RELEASES)),
        "head_sha": SHA,
        # Optional hints the fetch layer may add. `html_url_claimed` is the
        # URL the source claimed for itself; it is accepted here so the file
        # loads, and dropped by the collector (the source URL is rebuilt
        # from the full name).
        "rewrite_hint": Text(MAX_TAG),
        "html_url_claimed": Text(MAX_DESCRIPTION),
        # Comparison against the parent, as the fetch layer reported it.
        "comparison": None,  # set below once COMPARISON exists
    },
    optional=("head_sha", "rewrite_hint", "html_url_claimed", "comparison"),
)

COMPARISON_STATES = ("complete", "partial", "unavailable", "not_comparable")
COMPARISON_FIELDS = ("base_repository_id", "base_ref", "head_ref", "base_sha", "head_sha", "ahead", "behind", "collected_at")
_COMPARISON_TYPES = {
    "base_repository_id": Nullable(PositiveId()),
    "base_ref": Nullable(Text(MAX_TAG)),
    "head_ref": Nullable(Text(MAX_TAG)),
    "base_sha": Nullable(SHA),
    "head_sha": Nullable(SHA),
    "ahead": Nullable(NonNegInt()),
    "behind": Nullable(NonNegInt()),
    "state": Enum(COMPARISON_STATES),
    "collected_at": Nullable(TIMESTAMP),
    "future_dated": Bool(),
}
# From a run: optional fields are filled with null by the loader.
COMPARISON = Obj(dict(_COMPARISON_TYPES), optional=COMPARISON_FIELDS + ("future_dated",))
# In state: every field present, nothing filled in.
COMPARISON_STORED = Obj(dict(_COMPARISON_TYPES))
REPOSITORY.fields["comparison"] = Nullable(COMPARISON)


def check_comparison(comparison, path):
    """Unknown divergence is null with an unavailable state; a known one has both counts."""
    if comparison["state"] in ("unavailable", "not_comparable"):
        if comparison["ahead"] is not None or comparison["behind"] is not None:
            raise SchemaError("%s: ahead/behind must be null when the comparison is %s" % (path, comparison["state"]))
    else:
        if comparison["ahead"] is None or comparison["behind"] is None:
            raise SchemaError("%s: a %s comparison needs ahead and behind" % (path, comparison["state"]))
        if comparison["collected_at"] is None:
            raise SchemaError("%s: a %s comparison needs collected_at" % (path, comparison["state"]))
    return comparison


def _downgrade_comparison(comparison, path, rejected, field):
    rejected.append({"path": "%s.%s" % (path, field), "reason": "invalid_ref"})
    for key in COMPARISON_FIELDS:
        comparison[key] = None
    comparison["state"] = "unavailable"


def normalise_run_comparison(repo, index, run_at, rejected):
    """Fill, clamp and sanity-check one repository's comparison block from a run.

    A ref outside the git reference character set does not refuse the run:
    the comparison becomes unavailable and the field is listed in
    `rejected_fields`. A collected_at after the run time is clamped and
    flagged `future_dated`.
    """
    comparison = repo.get("comparison")
    if comparison is None:
        return
    path = "repositories[%d].comparison" % index
    for field in COMPARISON_FIELDS:
        comparison.setdefault(field, None)
    comparison.setdefault("future_dated", False)
    for field in ("base_ref", "head_ref"):
        value = comparison[field]
        if value is not None and REF_RE.fullmatch(value) is None:
            _downgrade_comparison(comparison, path, rejected, field)
            return
    if comparison["collected_at"] is not None and comparison["collected_at"] > run_at:
        comparison["collected_at"] = run_at
        comparison["future_dated"] = True
    check_comparison(comparison, "run." + path)

ROOT = Obj(
    {
        "repository_id": PositiveId(),
        "full_name": FULL_NAME,
        "forks_scan_complete": Bool(),
        "found_repository_ids": List(PositiveId(), MAX_IDS),
        # Opaque position to resume an incomplete fork-network scan from.
        "resume_cursor": Nullable(Text(MAX_TAG)),
    },
    optional=("found_repository_ids", "resume_cursor"),
)
SEARCH = Obj({"query": QUERY, "complete": Bool(), "repository_ids": List(PositiveId(), MAX_IDS)})
REJECTED = Obj({"path": Str(pattern=PATH_RE, max_len=128), "reason": Enum(REJECTION_REASONS)})
MAX_REJECTED = 100

RUN = Obj(
    {
        "run_at": TIMESTAMP,
        "fictional": Bool(),
        "roots": List(ROOT, MAX_IDS),
        "searches": List(SEARCH, MAX_IDS),
        "repositories": List(REPOSITORY, MAX_REPOSITORIES),
        # Written by validate_run: fields it downgraded instead of refusing the run.
        "rejected_fields": List(REJECTED, MAX_REJECTED),
    },
    optional=("fictional", "rejected_fields"),
)


def validate_run(run):
    """Check a parsed run document (bounding its free text in place); raise SchemaError.

    Hostile per-repository values that the collector can live without (a
    comparison ref, a scan cursor) are downgraded and listed in
    run["rejected_fields"]; operator configuration (search queries) and
    everything structural raises.
    """
    RUN.check(run, "run")
    # A run validated twice keeps the notes from the first pass.
    rejected = [dict(r) for r in run.get("rejected_fields", [])]
    run_at = run["run_at"]
    for i, search in enumerate(run["searches"]):
        if "://" in search["query"] or _SCHEME_RE.match(search["query"]) is not None:
            raise SchemaError("run.searches[%d].query: a search query must not contain a URL" % i)
    for i, root in enumerate(run["roots"]):
        cursor = root.get("resume_cursor")
        if cursor is not None and CURSOR_RE.fullmatch(cursor) is None:
            root["resume_cursor"] = None
            rejected.append({"path": "roots[%d].resume_cursor" % i, "reason": "invalid_cursor"})
    seen = set()
    for i, repo in enumerate(run["repositories"]):
        rid = repo["repository_id"]
        if rid in seen:
            raise SchemaError("run.repositories[%d]: duplicate repository_id %d" % (i, rid))
        seen.add(rid)
        if repo["owner"] != repo["full_name"].split("/")[0]:
            raise SchemaError("run.repositories[%d]: owner does not match full_name" % i)
        if repo["commits"]:
            shas = [c["sha"] for c in repo["commits"]]
            if len(set(shas)) != len(shas):
                raise SchemaError("run.repositories[%d]: duplicate commit sha" % i)
            head = repo.get("head_sha")
            if head is not None and head not in shas:
                raise SchemaError("run.repositories[%d]: head_sha is not in commits" % i)
        if repo["releases"]:
            ids = [r["id"] for r in repo["releases"]]
            if len(set(ids)) != len(ids):
                raise SchemaError("run.repositories[%d]: duplicate release id" % i)
        normalise_run_comparison(repo, i, run_at, rejected)
    queries = [s["query"] for s in run["searches"]]
    if len(set(queries)) != len(queries):
        raise SchemaError("run.searches: duplicate query")
    root_ids = [r["repository_id"] for r in run["roots"]]
    if len(set(root_ids)) != len(root_ids):
        raise SchemaError("run.roots: duplicate repository_id")
    unique = []
    for entry in rejected:
        if entry not in unique:
            unique.append(entry)
    run["rejected_fields"] = unique[:MAX_REJECTED]
    return run


def load_run_file(path):
    return validate_run(read_json(path))


# --- events --------------------------------------------------------------

EVENT_TYPES = (
    "repository_discovered",
    "commits_observed",
    "release_published",
    "repository_archived",
    "collection_stale",
    "collection_recovered",
    "game_association_changed",
    "fork_discovered",
    "fork_activity_observed",
    "fork_divergence_changed",
)

EVENT = Obj(
    {
        "event_id": Str(pattern=EVENT_ID_RE, max_len=MAX_TAG),
        "type": Enum(EVENT_TYPES),
        "repository_id": PositiveId(),
        "repository": FULL_NAME,
        "owner": OWNER,
        "game_key": Nullable(Str(pattern=re.compile(r"[a-z0-9-]+\Z"), max_len=64)),
        "game_title": Nullable(Rendered(MAX_RENDERED)),
        "game_inferred": Bool(),
        "toolkit": Nullable(Str(pattern=LABEL_RE, max_len=64)),
        "source_at": Nullable(TIMESTAMP),
        "observed_at": TIMESTAMP,
        "title": Rendered(MAX_RENDERED),
        "summary": Rendered(MAX_RENDERED),
        "source_url": Str(pattern=SOURCE_URL_RE, max_len=MAX_FULL_NAME + 32),
        "details": Details(),
    }
)


def validate_event(event):
    EVENT.check(event, "event")
    if event["owner"] != event["repository"].split("/")[0]:
        raise SchemaError("event.owner: does not match repository")
    if event["source_url"] != "https://github.com/" + event["repository"]:
        raise SchemaError("event.source_url: does not match repository")
    if not event["event_id"].startswith("repo-%d:" % event["repository_id"]):
        raise SchemaError("event.event_id: does not name repository_id")
    return event


def load_events(events):
    List(EVENT, MAX_IDS).check(events, "events")
    seen = set()
    for i, event in enumerate(events):
        validate_event(event)
        if event["event_id"] in seen:
            raise SchemaError("events[%d]: duplicate event_id" % i)
        seen.add(event["event_id"])
    return events


# --- persisted state ------------------------------------------------------

OBSERVED_COMMIT = Obj({"committed_at": TIMESTAMP, "message": Text(MAX_MESSAGE), "first_observed_at": TIMESTAMP})
OBSERVED_RELEASE = Obj({"tag": Text(MAX_TAG), "published_at": TIMESTAMP, "first_observed_at": TIMESTAMP})
NAME_HISTORY = Obj({"full_name": FULL_NAME, "owner": OWNER, "until": TIMESTAMP})
GAME = Obj(
    {
        "key": Nullable(Str(pattern=re.compile(r"[a-z0-9-]+\Z"), max_len=64)),
        "title": Nullable(Text(MAX_TAG)),
        "basis": Nullable(Enum(("readme_title", "inherited_from_parent", "inherited_unverified_copy"))),
        "confidence": Enum(("strong", "weak", "unknown")),
        "inferred": Bool(),
        "verified": Bool(),
        "inherited_from": Nullable(PositiveId()),
    }
)
INHERITED_BASES = ("inherited_from_parent", "inherited_unverified_copy")
TOOLKIT = Obj(
    {
        "name": Nullable(Str(pattern=LABEL_RE, max_len=64)),
        "confidence": Enum(("strong", "weak", "unknown")),
        "basis": Nullable(Enum(("own_text", "inherited_from_parent"))),
        "inherited_from": Nullable(PositiveId()),
    }
)
ANCESTRY_STATE = Enum(("none", "resolved", "unresolved", "unknown", "inconsistent", "depth_exceeded"))
ANCESTRY_HISTORY = Obj(
    {
        "is_fork": Nullable(Bool()),
        "parent_repository_id": Nullable(PositiveId()),
        "network_root_repository_id": Nullable(PositiveId()),
        "until": TIMESTAMP,
    }
)
FIRST_SIGHT = Obj(
    {
        "heading": Nullable(Text(MAX_TAG)),
        "ancestor_heading": Nullable(Text(MAX_TAG)),
        "ancestor_repository_id": Nullable(PositiveId()),
    }
)
COMPARISON_STATUS = Obj(
    {
        "last_reported_state": Nullable(Enum(COMPARISON_STATES)),
        "last_reported_at": Nullable(TIMESTAMP),
        "stale": Bool(),
    }
)
COMPARISON_HISTORY = Obj(dict(_COMPARISON_TYPES, replaced_at=TIMESTAMP))
MAX_HISTORY = 20

OBSERVATION = Obj(
    {
        "repository_id": PositiveId(),
        "full_name": FULL_NAME,
        "owner": OWNER,
        "name_history": List(NAME_HISTORY, MAX_IDS),
        "description": Nullable(Text(MAX_DESCRIPTION)),
        "readme_excerpt": Nullable(Text(MAX_README)),
        "is_fork": Nullable(Bool()),
        "parent_repository_id": Nullable(PositiveId()),
        "network_root_repository_id": Nullable(PositiveId()),
        "archived": Nullable(Bool()),
        "stars": Nullable(NonNegInt()),
        "default_branch": Nullable(Text(MAX_TAG)),
        "stale": Bool(),
        "partial": Bool(),
        "fetch_state": Enum(FETCH_STATES),
        "collection_state": Enum(COLLECTION_STATES),
        "consecutive_failures": NonNegInt(),
        "stale_notified": Bool(),
        "first_seen_at": TIMESTAMP,
        "last_attempted_refresh_at": TIMESTAMP,
        "last_successful_refresh_at": Nullable(TIMESTAMP),
        "discovery_sources": List(Str(max_len=MAX_QUERY + 16), MAX_SOURCES),
        "commits_baseline_done": Bool(),
        "releases_baseline_done": Bool(),
        "commits_complete": Nullable(Bool()),
        "tip_sha": Nullable(SHA),
        "tip_committed_at": Nullable(TIMESTAMP),
        "history_rewrite_suspected": Bool(),
        "observed_commits": Map(OBSERVED_COMMIT, key_pattern=SHA_RE, max_items=MAX_COMMITS * 10, max_key_len=40),
        "releases": Map(OBSERVED_RELEASE, key_pattern=DIGITS_RE, max_items=MAX_RELEASES * 10, max_key_len=20),
        "category": Enum(("framework", "game", "unclassified")),
        "game": GAME,
        "toolkit": TOOLKIT,
        "ancestry_state": ANCESTRY_STATE,
        "ancestry_history": List(ANCESTRY_HISTORY, MAX_HISTORY),
        "fork_discovered_done": Bool(),
        "first_sight": Nullable(FIRST_SIGHT),
        "discovery_roots": List(PositiveId(), MAX_SOURCES),
        "discovery_queries": List(QUERY, MAX_SOURCES),
        "comparison": COMPARISON_STORED,
        "comparison_status": COMPARISON_STATUS,
        "comparison_history": List(COMPARISON_HISTORY, MAX_HISTORY),
    }
)

SEARCH_STATUS = Obj({"complete": Bool(), "repository_ids": List(PositiveId(), MAX_IDS), "last_run_at": TIMESTAMP})
ROOT_STATUS = Obj(
    {
        "full_name": FULL_NAME,
        "forks_scan_complete": Bool(),
        "found_repository_ids": List(PositiveId(), MAX_IDS),
        "scan_state": Enum(("complete", "partial")),
        "resume_cursor": Nullable(Text(MAX_TAG)),
        "last_complete_at": Nullable(TIMESTAMP),
        "last_run_at": TIMESTAMP,
    }
)

COLLECTION = Obj(
    {
        "searches": Map(SEARCH_STATUS, max_items=MAX_IDS),
        "roots": Map(ROOT_STATUS, key_pattern=DIGITS_RE, max_items=MAX_IDS, max_key_len=20),
        "incomplete": Bool(),
        "incomplete_searches": List(QUERY, MAX_IDS),
        "incomplete_roots": List(PositiveId(), MAX_IDS),
        # Ids named by a search or a fork-network scan that have no observation yet.
        "pending_repository_ids": List(PositiveId(), MAX_IDS),
        # Fields the last run's loader downgraded instead of refusing the run.
        "rejected_fields": List(REJECTED, MAX_REJECTED),
    }
)

STATE = Obj(
    {
        "schema_version": PositiveId(),
        "baseline_done": Bool(),
        "baseline_at": Nullable(TIMESTAMP),
        "last_run_at": Nullable(TIMESTAMP),
        "repositories": Map(OBSERVATION, key_pattern=DIGITS_RE, max_items=MAX_REPOSITORIES * 10, max_key_len=20),
        "events": List(EVENT, MAX_IDS * 100),
        "collection": COLLECTION,
    }
)


def validate_state(state):
    STATE.check(state, "state")
    for key, obs in state["repositories"].items():
        if str(obs["repository_id"]) != key:
            raise SchemaError("state.repositories[%r]: key does not match repository_id" % key)
        if obs["owner"] != obs["full_name"].split("/")[0]:
            raise SchemaError("state.repositories[%r]: owner does not match full_name" % key)
        if obs["tip_sha"] is not None and obs["tip_sha"] not in obs["observed_commits"]:
            raise SchemaError("state.repositories[%r]: tip_sha is not an observed commit" % key)
        check_comparison(obs["comparison"], "state.repositories[%r].comparison" % key)
        if (obs["game"]["basis"] in INHERITED_BASES) != (obs["game"]["inherited_from"] is not None):
            raise SchemaError("state.repositories[%r].game: inherited_from and basis disagree" % key)
        if obs["game"]["verified"] is not False or obs["game"]["inferred"] is not True:
            raise SchemaError("state.repositories[%r].game: associations are always inferred and unverified" % key)
        if len(set(obs["discovery_roots"])) != len(obs["discovery_roots"]):
            raise SchemaError("state.repositories[%r].discovery_roots: duplicate ids" % key)
        if obs["is_fork"] is not True and obs["ancestry_state"] in ("resolved", "unresolved", "depth_exceeded"):
            raise SchemaError("state.repositories[%r].ancestry_state: %s on a non-fork" % (key, obs["ancestry_state"]))
        if obs["is_fork"] is None and obs["ancestry_state"] != "unknown":
            raise SchemaError("state.repositories[%r].ancestry_state: expected unknown" % key)
    for key, root in state["collection"]["roots"].items():
        if (root["scan_state"] == "complete") != root["forks_scan_complete"]:
            raise SchemaError("state.collection.roots[%r]: scan_state contradicts forks_scan_complete" % key)
        if len(set(root["found_repository_ids"])) != len(root["found_repository_ids"]):
            raise SchemaError("state.collection.roots[%r]: duplicate found ids" % key)
    seen = set()
    for i, event in enumerate(state["events"]):
        validate_event(event)
        if event["event_id"] in seen:
            raise SchemaError("state.events[%d]: duplicate event_id %r" % (i, event["event_id"]))
        seen.add(event["event_id"])
    if state["baseline_done"] != (state["baseline_at"] is not None):
        raise SchemaError("state.baseline_done: inconsistent with baseline_at")
    return state

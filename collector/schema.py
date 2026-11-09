"""Strict JSON schemas for run files, persisted state and events.

Every object is checked against a fixed field list: a missing required
field, a value of the wrong type or an unknown field raises SchemaError
naming the path. Repository text is validated for type and shape only; it
is kept as data and never interpreted here.
"""

import json
import re

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# GitHub owner and repository names: letters, digits, '-', '_', '.'.
FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
EVENT_ID_RE = re.compile(r"^repo-[0-9]+:[a-z_]+(:[0-9a-f]+)?$")

FETCH_STATES = ("ok", "rate_limited", "not_found")


class SchemaError(ValueError):
    """Raised when a document does not match its strict schema."""


class Type:
    def check(self, value, path):
        raise NotImplementedError


class Any(Type):
    def check(self, value, path):
        return value


class Int(Type):
    def check(self, value, path):
        if not isinstance(value, int) or isinstance(value, bool):
            raise SchemaError("%s: expected integer, got %s" % (path, _kind(value)))
        return value


class Bool(Type):
    def check(self, value, path):
        if not isinstance(value, bool):
            raise SchemaError("%s: expected boolean, got %s" % (path, _kind(value)))
        return value


class Str(Type):
    def __init__(self, pattern=None, max_len=None):
        self.pattern = pattern
        self.max_len = max_len

    def check(self, value, path):
        if not isinstance(value, str):
            raise SchemaError("%s: expected string, got %s" % (path, _kind(value)))
        if self.max_len is not None and len(value) > self.max_len:
            raise SchemaError("%s: string longer than %d" % (path, self.max_len))
        if self.pattern is not None and not self.pattern.match(value):
            raise SchemaError("%s: string does not match the expected form" % path)
        return value


class Enum(Type):
    def __init__(self, values):
        self.values = tuple(values)

    def check(self, value, path):
        if value not in self.values:
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
    def __init__(self, item):
        self.item = item

    def check(self, value, path):
        if not isinstance(value, list):
            raise SchemaError("%s: expected list, got %s" % (path, _kind(value)))
        for i, item in enumerate(value):
            self.item.check(item, "%s[%d]" % (path, i))
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
            ftype.check(value[name], "%s.%s" % (path, name))
        return value


class Map(Type):
    """An object with arbitrary string keys and uniform values."""

    def __init__(self, value_type, key_pattern=None):
        self.value_type = value_type
        self.key_pattern = key_pattern

    def check(self, value, path):
        if not isinstance(value, dict):
            raise SchemaError("%s: expected object, got %s" % (path, _kind(value)))
        for key, item in value.items():
            if self.key_pattern is not None and not self.key_pattern.match(key):
                raise SchemaError("%s: key %r has an unexpected form" % (path, key))
            self.value_type.check(item, "%s[%r]" % (path, key))
        return value


def _kind(value):
    if value is None:
        return "null"
    return type(value).__name__


# --- run files (what a fetch returned) ---------------------------------

TIMESTAMP = Str(pattern=TIMESTAMP_RE)
SHA = Str(pattern=SHA_RE)
FULL_NAME = Str(pattern=FULL_NAME_RE, max_len=200)
OWNER = Str(pattern=OWNER_RE, max_len=100)
QUERY = Str(max_len=256)

COMMIT = Obj({"sha": SHA, "committed_at": TIMESTAMP, "message": Str()})
RELEASE = Obj({"id": Int(), "tag": Str(max_len=256), "published_at": TIMESTAMP})

REPOSITORY = Obj(
    {
        "repository_id": Int(),
        "full_name": FULL_NAME,
        "owner": OWNER,
        "description": Nullable(Str()),
        "readme_excerpt": Nullable(Str()),
        "is_fork": Nullable(Bool()),
        "parent_repository_id": Nullable(Int()),
        "network_root_repository_id": Nullable(Int()),
        "archived": Nullable(Bool()),
        "stars": Nullable(Int()),
        "default_branch": Nullable(Str(max_len=256)),
        "fetch_state": Enum(FETCH_STATES),
        "commits_complete": Nullable(Bool()),
        "commits": Nullable(List(COMMIT)),
        "releases": Nullable(List(RELEASE)),
        # Optional hints the fetch layer may add. `html_url_claimed` is the
        # URL the source claimed for itself; it is accepted here so the file
        # loads, and dropped by the collector (the source URL is rebuilt
        # from the full name).
        "rewrite_hint": Str(max_len=256),
        "html_url_claimed": Str(),
    },
    optional=("rewrite_hint", "html_url_claimed"),
)

ROOT = Obj(
    {
        "repository_id": Int(),
        "full_name": FULL_NAME,
        "forks_scan_complete": Bool(),
        "found_repository_ids": List(Int()),
    },
    optional=("found_repository_ids",),
)
SEARCH = Obj({"query": QUERY, "complete": Bool(), "repository_ids": List(Int())})

RUN = Obj(
    {
        "run_at": TIMESTAMP,
        "fictional": Bool(),
        "roots": List(ROOT),
        "searches": List(SEARCH),
        "repositories": List(REPOSITORY),
    },
    optional=("fictional",),
)


def validate_run(run):
    """Check a parsed run document; return it unchanged or raise SchemaError."""
    RUN.check(run, "run")
    seen = set()
    for i, repo in enumerate(run["repositories"]):
        rid = repo["repository_id"]
        if rid in seen:
            raise SchemaError("run.repositories[%d]: duplicate repository_id %d" % (i, rid))
        seen.add(rid)
    return run


def load_run_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SchemaError("%s: not valid JSON (%s)" % (path, exc)) from None
    return validate_run(data)


# --- events --------------------------------------------------------------

EVENT_TYPES = (
    "repository_discovered",
    "commits_observed",
    "release_published",
    "repository_archived",
    "collection_stale",
    "collection_recovered",
    "game_association_changed",
)

EVENT = Obj(
    {
        "event_id": Str(pattern=EVENT_ID_RE),
        "type": Enum(EVENT_TYPES),
        "repository_id": Int(),
        "repository": FULL_NAME,
        "owner": OWNER,
        "game_key": Nullable(Str(max_len=64)),
        "game_title": Nullable(Str()),
        "game_inferred": Bool(),
        "toolkit": Nullable(Str(max_len=64)),
        "source_at": Nullable(TIMESTAMP),
        "observed_at": TIMESTAMP,
        "title": Str(),
        "summary": Str(),
        "source_url": Str(pattern=re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")),
        "details": Map(Any()),
    }
)


def validate_event(event):
    EVENT.check(event, "event")
    return event


def load_events(events):
    List(EVENT).check(events, "events")
    return events


# --- persisted state ------------------------------------------------------

OBSERVED_COMMIT = Obj({"committed_at": TIMESTAMP, "message": Str(), "first_observed_at": TIMESTAMP})
OBSERVED_RELEASE = Obj({"tag": Str(max_len=256), "published_at": TIMESTAMP, "first_observed_at": TIMESTAMP})
NAME_HISTORY = Obj({"full_name": FULL_NAME, "owner": OWNER, "until": TIMESTAMP})
GAME = Obj(
    {
        "key": Nullable(Str(max_len=64)),
        "title": Nullable(Str()),
        "basis": Nullable(Str(max_len=64)),
        "confidence": Enum(("strong", "weak", "unknown")),
        "inferred": Bool(),
        "verified": Bool(),
    }
)
TOOLKIT = Obj({"name": Nullable(Str(max_len=64)), "confidence": Enum(("strong", "weak", "unknown"))})
COUNTERS = Map(Int(), key_pattern=re.compile(r"^[a-z_]+$"))

OBSERVATION = Obj(
    {
        "repository_id": Int(),
        "full_name": FULL_NAME,
        "owner": OWNER,
        "name_history": List(NAME_HISTORY),
        "description": Nullable(Str()),
        "readme_excerpt": Nullable(Str()),
        "is_fork": Nullable(Bool()),
        "parent_repository_id": Nullable(Int()),
        "network_root_repository_id": Nullable(Int()),
        "archived": Nullable(Bool()),
        "stars": Nullable(Int()),
        "default_branch": Nullable(Str(max_len=256)),
        "stale": Bool(),
        "fetch_state": Enum(FETCH_STATES),
        "collection_state": Enum(("ok", "failing", "stale", "not_found")),
        "consecutive_failures": Int(),
        "stale_notified": Bool(),
        "first_seen_at": TIMESTAMP,
        "last_attempted_refresh_at": TIMESTAMP,
        "last_successful_refresh_at": Nullable(TIMESTAMP),
        "discovery_sources": List(Str(max_len=300)),
        "commits_complete": Nullable(Bool()),
        "tip_sha": Nullable(SHA),
        "tip_committed_at": Nullable(TIMESTAMP),
        "history_rewrite_suspected": Bool(),
        "observed_commits": Map(OBSERVED_COMMIT, key_pattern=SHA_RE),
        "releases": Map(OBSERVED_RELEASE, key_pattern=re.compile(r"^[0-9]+$")),
        "category": Enum(("framework", "game", "unclassified")),
        "game": GAME,
        "toolkit": TOOLKIT,
        "event_counters": COUNTERS,
    }
)

SEARCH_STATUS = Obj({"complete": Bool(), "repository_ids": List(Int()), "last_run_at": TIMESTAMP})
ROOT_STATUS = Obj(
    {
        "full_name": FULL_NAME,
        "forks_scan_complete": Bool(),
        "found_repository_ids": List(Int()),
        "last_run_at": TIMESTAMP,
    }
)

COLLECTION = Obj(
    {
        "searches": Map(SEARCH_STATUS),
        "roots": Map(ROOT_STATUS, key_pattern=re.compile(r"^[0-9]+$")),
        "incomplete": Bool(),
        "incomplete_searches": List(QUERY),
        "incomplete_roots": List(Int()),
    }
)

STATE = Obj(
    {
        "schema_version": Int(),
        "baseline_at": Nullable(TIMESTAMP),
        "last_run_at": Nullable(TIMESTAMP),
        "repositories": Map(OBSERVATION, key_pattern=re.compile(r"^[0-9]+$")),
        "events": List(EVENT),
        "collection": COLLECTION,
    }
)


def validate_state(state):
    STATE.check(state, "state")
    for key, obs in state["repositories"].items():
        if str(obs["repository_id"]) != key:
            raise SchemaError("state.repositories[%r]: key does not match repository_id" % key)
    return state

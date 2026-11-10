"""Feed history: an append-only archive of the feeds the scheduled refresh publishes.

feed.json keeps only the newest export.MAX_FEED_EVENTS events and the
current repository values. This module keeps the rest, in a directory that
the refresh workflow commits to the feed-history branch (docs/history.md):

  README.md                 fixed text, written by this module
  index.json                format_version, last_run_at, runs, months, and
                            per file: bytes, lines, sha256, compressed
  events/YYYY-MM.jsonl      one feed event per line, in the month of its
                            observed_at; sorted by (source_at or observed_at,
                            id); an event id is stored once, ever
  snapshots/YYYY-MM.jsonl   per run, in the month of the run: one header
                            line {run_at, complete, stopped, not_fetched,
                            repos}, then one line per repository whose
                            tracked values changed since its previous line:
                            {id, repo, stars, commits_30d, category,
                            archived, fork, last_commit_at}. A repository
                            seen for the first time, or again after it left
                            the feed, gets a full line; one that left the
                            feed gets {id, repo, removed: true}.
  *.jsonl.gz                a month compacted with gzip (same lines)

The index is derived from the files alone (runs and last_run_at come from
the snapshot headers), and loading checks that it matches them byte for
byte, so a hand edit or a half-finished write is found before anything new
is written. Every line is canonical JSON (sorted keys, no spaces, ASCII),
gzip output has a fixed header (mtime 0), and nothing reads the clock: the
same history and feed always give the same bytes.

What reaches the history: snapshot lines carry only the fields above
(validated ids, owner/name, integers, booleans, an enum and a timestamp),
so repository descriptions and other free text never do. Event summaries
are plain text built from fixed templates, but a release tag inside one is
repository text, so email addresses, token-like strings and non-GitHub URLs
in a summary are replaced by REMOVED, and every file is scanned for them
again before it is written.
"""

import gzip
import hashlib
import json
import re

from . import schema

FORMAT_VERSION = 1
MAX_FILE_BYTES = 50 * 1024 * 1024
BUDGET_BYTES = 500 * 1024 * 1024
MAX_FEED_BYTES = 16 * 1024 * 1024
MAX_COMPACT_MONTHS = 1200

INDEX_NAME = "index.json"
README_NAME = "README.md"
KINDS = ("events", "snapshots")
REMOVED = "[removed]"

FILE_RE = re.compile(r"(events|snapshots)/([0-9]{4}-(?:0[1-9]|1[0-2]))\.jsonl(\.gz)?\Z")
MONTH_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")
REPO_ID_RE = re.compile(r"r[1-9][0-9]{0,15}\Z")
SOURCE_LABEL_RE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
CATEGORIES = ("framework", "game", "tool", "unclassified")
SNAPSHOT_FIELDS = ("id", "repo", "stars", "commits_30d", "category", "archived", "fork", "last_commit_at")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_TOKEN_RE = re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{8,}")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
_GITHUB = "https://github.com/"

README_TEXT = """# feed-history

Written by the refresh workflow of ps3rc-feed. It keeps every event the feed has shown and the changes of the
tracked repository values, one file per month. The format and the size limits are described in docs/history.md
on the main branch. Do not edit this branch by hand; history is only appended.
"""


class Refused(ValueError):
    """The request is understood but not applied (an older run, a file over the size limit)."""


# --- schemas -----------------------------------------------------------------------------------------------

_TS = schema.TIMESTAMP
_REPO_ID = schema.Str(pattern=REPO_ID_RE, max_len=17)
_PLAIN = schema.Str(max_len=schema.MAX_DESCRIPTION)
_LABEL = schema.Str(pattern=schema.LABEL_RE, max_len=64)
_COUNT = schema.NonNegInt()

FEED_REPO = schema.Obj(
    {
        "id": _REPO_ID,
        "repo": schema.FULL_NAME,
        "owner": schema.OWNER,
        "url": schema.Str(pattern=schema.SOURCE_URL_RE, max_len=schema.MAX_FULL_NAME + 32),
        "description": schema.Nullable(_PLAIN),
        "game": schema.Nullable(_PLAIN),
        "game_inferred": schema.Bool(),
        "game_inherited": schema.Bool(),
        "category": schema.Nullable(schema.Enum(CATEGORIES)),
        "toolkit": schema.Nullable(_LABEL),
        "fork": schema.Nullable(schema.Bool()),
        "parent": schema.Nullable(schema.FULL_NAME),
        "root": schema.Nullable(schema.FULL_NAME),
        "ahead": schema.Nullable(_COUNT),
        "behind": schema.Nullable(_COUNT),
        "comparison_state": schema.Nullable(_LABEL),
        "stars": schema.Nullable(_COUNT),
        "commits_30d": schema.Nullable(_COUNT),
        "first_seen_at": schema.Nullable(_TS),
        "last_commit_at": schema.Nullable(_TS),
        "archived": schema.Nullable(schema.Bool()),
        "collection_failing": schema.Bool(),
    }
)

_EVENT_FIELDS = {
    "id": schema.Str(pattern=schema.EVENT_ID_RE, max_len=schema.MAX_TAG),
    "type": schema.Enum(schema.EVENT_TYPES),
    "repo_id": _REPO_ID,
    "source_at": schema.Nullable(_TS),
    "observed_at": _TS,
    "summary": schema.Nullable(_PLAIN),
}
FEED_EVENT = schema.Obj(dict(_EVENT_FIELDS))

FEED_COLLECTION = schema.Obj(
    {
        "complete": schema.Bool(),
        "stopped": schema.Nullable(schema.Enum(schema.STOP_REASONS)),
        "not_fetched": _COUNT,
        "error_count": _COUNT,
        "last_run_at": _TS,
    }
)

FEED = schema.Obj(
    {
        "generated_at": _TS,
        "collection": FEED_COLLECTION,
        "repos": schema.List(FEED_REPO, schema.MAX_IDS),
        "events": schema.List(FEED_EVENT, schema.MAX_IDS),
        "stale_after_minutes": schema.Int(1, 100000),
    },
    optional=("collection", "stale_after_minutes"),
)

EVENT_LINE = schema.Obj(dict(_EVENT_FIELDS, source=schema.Str(pattern=SOURCE_LABEL_RE, max_len=32)), optional=("source",))
SNAPSHOT_HEADER = schema.Obj(
    {
        "run_at": _TS,
        "complete": schema.Bool(),
        "stopped": schema.Nullable(schema.Enum(schema.STOP_REASONS)),
        "not_fetched": _COUNT,
        "repos": _COUNT,
    }
)
SNAPSHOT_REPO = schema.Obj(
    {
        "id": _REPO_ID,
        "repo": schema.FULL_NAME,
        "stars": schema.Nullable(_COUNT),
        "commits_30d": schema.Nullable(_COUNT),
        "category": schema.Nullable(schema.Enum(CATEGORIES)),
        "archived": schema.Nullable(schema.Bool()),
        "fork": schema.Nullable(schema.Bool()),
        "last_commit_at": schema.Nullable(_TS),
    }
)
class _TrueOnly(schema.Type):
    def check(self, value, path):
        if value is not True:
            raise schema.SchemaError("%s: expected true" % path)
        return value


SNAPSHOT_REMOVED = schema.Obj({"id": _REPO_ID, "repo": schema.FULL_NAME, "removed": _TrueOnly()})

INDEX_FILE = schema.Obj(
    {
        "bytes": _COUNT,
        "lines": _COUNT,
        "sha256": schema.Str(pattern=SHA256_RE, max_len=64),
        "compressed": schema.Bool(),
    }
)
INDEX = schema.Obj(
    {
        "format_version": schema.Int(1, 1000),
        "last_run_at": schema.Nullable(_TS),
        "runs": _COUNT,
        "months": schema.List(schema.Str(pattern=MONTH_RE, max_len=7), schema.MAX_IDS),
        "files": schema.Map(INDEX_FILE, key_pattern=FILE_RE, max_items=schema.MAX_IDS, max_key_len=40),
        "total_bytes": _COUNT,
    }
)


def _id_number(record_id):
    return int(record_id[1:])


def month_of(timestamp):
    return timestamp[:7]


def months_between(earlier, later):
    """Whole months from one YYYY-MM to another (later - earlier)."""
    return (int(later[:4]) * 12 + int(later[5:7])) - (int(earlier[:4]) * 12 + int(earlier[5:7]))


def file_name(kind, month, compressed):
    return "%s/%s.jsonl%s" % (kind, month, ".gz" if compressed else "")


def line_bytes(obj):
    """One canonical JSON line: sorted keys, no spaces, ASCII, newline-terminated."""
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def document_bytes(document):
    """index.json: the same canonical form as the feed and manifest (cli.json_bytes)."""
    return (json.dumps(document, indent=1, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def gzip_bytes(data):
    """gzip with a fixed header (no name, mtime 0): the same input gives the same bytes."""
    return gzip.compress(data, compresslevel=9, mtime=0)


def clean_summary(value):
    """Replace email addresses, token-like strings and non-GitHub URLs in a summary by REMOVED."""
    if value is None:
        return None
    value = _TOKEN_RE.sub(REMOVED, value)
    value = _EMAIL_RE.sub(REMOVED, value)
    return _URL_RE.sub(lambda m: m.group(0) if m.group(0).startswith(_GITHUB) else REMOVED, value)


def unsafe_text(data):
    """A reason when the bytes hold an email address, a token-like string or a non-GitHub URL, else None."""
    content = data.decode("utf-8", errors="replace")
    if _EMAIL_RE.search(content):
        return "an email address"
    if _TOKEN_RE.search(content):
        return "a token-like string"
    for match in _URL_RE.finditer(content):
        if not match.group(0).startswith(_GITHUB):
            return "a non-GitHub URL"
    return None


# --- feeds -------------------------------------------------------------------------------------------------


def validate_feed(feed, require_collection=True):
    """Check a feed.json document the way export writes it; raise schema.SchemaError naming the path.

    A feed for a run needs the collection object export adds; an older feed
    imported by hand (require_collection False) may lack it.
    """
    FEED.check(feed, "feed")
    if require_collection and "collection" not in feed:
        raise schema.SchemaError("feed: has no collection object; only a feed written by export can be applied as a run")
    if "collection" in feed and feed["collection"]["last_run_at"] != feed["generated_at"]:
        raise schema.SchemaError("feed.collection.last_run_at: differs from generated_at")
    repo_ids = set()
    for i, repo in enumerate(feed["repos"]):
        path = "feed.repos[%d]" % i
        if repo["id"] in repo_ids:
            raise schema.SchemaError("%s.id: duplicate repository id" % path)
        repo_ids.add(repo["id"])
        if repo["owner"] != repo["repo"].split("/")[0]:
            raise schema.SchemaError("%s.owner: does not match repo" % path)
        if repo["url"] != _GITHUB + repo["repo"]:
            raise schema.SchemaError("%s.url: does not match repo" % path)
    event_ids = set()
    for i, event in enumerate(feed["events"]):
        path = "feed.events[%d]" % i
        if event["id"] in event_ids:
            raise schema.SchemaError("%s.id: duplicate event id" % path)
        event_ids.add(event["id"])
        if not event["id"].startswith("repo-%d:" % _id_number(event["repo_id"])):
            raise schema.SchemaError("%s.id: does not name repo_id" % path)
        if event["repo_id"] not in repo_ids:
            raise schema.SchemaError("%s.repo_id: names no repository of the feed" % path)
        if event["observed_at"] > feed["generated_at"]:
            raise schema.SchemaError("%s.observed_at: later than generated_at" % path)
    return feed


def event_line(event, source=None):
    line = {
        "id": event["id"],
        "type": event["type"],
        "repo_id": event["repo_id"],
        "source_at": event["source_at"],
        "observed_at": event["observed_at"],
        "summary": clean_summary(event["summary"]),
    }
    if source is not None:
        line["source"] = source
    return line


def snapshot_record(repo):
    return {field: repo[field] for field in SNAPSHOT_FIELDS}


def _event_order(line):
    return (line["source_at"] or line["observed_at"], line["id"])


def _is_removed(line):
    return line.get("removed") is True


# --- the history ---------------------------------------------------------------------------------------------


class History:
    """The month files of a history directory, as parsed lines, plus their on-disk bytes when loaded."""

    def __init__(self):
        # (kind, month) -> {"compressed": bool, "lines": [dict], "dirty": bool}
        self.months = {}
        self.original = {}

    # loading

    @classmethod
    def load(cls, index_bytes, files):
        """Parse and check an existing history: `files` maps month file names to their on-disk bytes.

        Raises schema.SchemaError when a file does not parse, a line is not
        canonical or out of order, an event id repeats, snapshot runs are not
        in order, or index.json differs from what the files give.
        """
        history = cls()
        if index_bytes is None:
            if files:
                raise schema.SchemaError("the history has month files but no index.json")
            return history
        try:
            index = schema.loads(index_bytes.decode("utf-8"), INDEX_NAME)
        except UnicodeDecodeError:
            raise schema.SchemaError("index.json: not UTF-8") from None
        version = index.get("format_version") if isinstance(index, dict) else None
        if version != FORMAT_VERSION:
            raise schema.SchemaError("index.json: format_version %r is not supported (this collector writes %d)" % (version, FORMAT_VERSION))
        INDEX.check(index, INDEX_NAME)
        listed = set(index["files"])
        present = set(files)
        if listed != present:
            raise schema.SchemaError(
                "index.json lists %d file(s) the directory lacks and the directory holds %d file(s) index.json does not list"
                % (len(listed - present), len(present - listed))
            )
        for name in sorted(files):
            kind, month, compressed = _parse_name(name)
            if (kind, month) in history.months:
                raise schema.SchemaError("%s: the month is stored both plain and compressed" % name)
            raw = files[name]
            data = raw
            if compressed:
                try:
                    data = gzip.decompress(raw)
                except (OSError, EOFError, ValueError):
                    raise schema.SchemaError("%s: not a valid gzip file" % name) from None
            history.months[(kind, month)] = {"compressed": compressed, "lines": _parse_lines(kind, month, data, name), "dirty": False}
            history.original[name] = raw
        history._check_across_files()
        if history.index_bytes(history.original) != index_bytes:
            raise schema.SchemaError("index.json does not match the month files (sizes, line counts or digests differ)")
        return history

    def _check_across_files(self):
        seen = set()
        last_run = None
        for (kind, month) in sorted(self.months):
            name = file_name(kind, month, self.months[(kind, month)]["compressed"])
            for line in self.months[(kind, month)]["lines"]:
                if kind == "events":
                    if line["id"] in seen:
                        raise schema.SchemaError("%s: event %s is stored twice" % (name, line["id"]))
                    seen.add(line["id"])
                elif "run_at" in line:
                    if last_run is not None and line["run_at"] <= last_run:
                        raise schema.SchemaError("%s: snapshot runs are not in time order" % name)
                    last_run = line["run_at"]

    # derived values

    def runs(self):
        return [line["run_at"] for key in sorted(self.months) if key[0] == "snapshots" for line in self.months[key]["lines"] if "run_at" in line]

    def last_run_at(self):
        runs = self.runs()
        return runs[-1] if runs else None

    def event_ids(self):
        return set(line["id"] for key in self.months if key[0] == "events" for line in self.months[key]["lines"])

    def last_snapshot(self):
        """{repository id: its newest snapshot line}, replayed from the first run on."""
        last = {}
        for key in sorted(self.months):
            if key[0] == "snapshots":
                for line in self.months[key]["lines"]:
                    if "run_at" not in line:
                        last[line["id"]] = line
        return last

    # changes

    def _month(self, kind, month):
        if (kind, month) not in self.months:
            self.months[(kind, month)] = {"compressed": False, "lines": [], "dirty": True}
        entry = self.months[(kind, month)]
        entry["dirty"] = True
        return entry

    def _add_events(self, events, source=None):
        known = self.event_ids()
        added = 0
        for event in events:
            if event["id"] in known:
                continue
            known.add(event["id"])
            line = event_line(event, source)
            self._month("events", month_of(line["observed_at"]))["lines"].append(line)
            added += 1
        for (kind, _), entry in self.months.items():
            if kind == "events" and entry["dirty"]:
                entry["lines"].sort(key=_event_order)
        return added

    def apply_run(self, feed):
        """Add a run's new events and its snapshot. Returns None when this run is already the newest one stored.

        Raises Refused for a run older than the newest one stored.
        """
        run_at = feed["generated_at"]
        last = self.last_run_at()
        if last is not None and run_at == last:
            return None
        if last is not None and run_at < last:
            raise Refused("refused: the feed's generated_at %s is older than the history's last run (%s); runs are only applied forwards" % (run_at, last))
        added = self._add_events(feed["events"])
        collection = feed["collection"]
        lines = [
            {
                "run_at": run_at,
                "complete": collection["complete"],
                "stopped": collection["stopped"],
                "not_fetched": collection["not_fetched"],
                "repos": len(feed["repos"]),
            }
        ]
        records = {repo["id"]: snapshot_record(repo) for repo in feed["repos"]}
        previous = self.last_snapshot()
        present = set(rid for rid, line in previous.items() if not _is_removed(line))
        for rid in sorted(set(records) | present, key=_id_number):
            if rid in records:
                if previous.get(rid) != records[rid]:
                    lines.append(records[rid])
            else:
                lines.append({"id": rid, "repo": previous[rid]["repo"], "removed": True})
        self._month("snapshots", month_of(run_at))["lines"].extend(lines)
        return {"run_at": run_at, "events_added": added, "snapshot_lines": len(lines) - 1}

    def import_events(self, feed, label):
        """Add an older feed's events marked with "source": label; no snapshot. Returns (added, already stored)."""
        added = self._add_events(feed["events"], source=label)
        return added, len(feed["events"]) - added

    def compact(self, older_than):
        """Mark plain months more than `older_than` months before the last run's month for gzip; returns their names."""
        last = self.last_run_at()
        if last is None:
            return []
        names = []
        for (kind, month), entry in sorted(self.months.items()):
            if not entry["compressed"] and months_between(month, month_of(last)) > older_than:
                names.append(file_name(kind, month, False))
                entry["compressed"] = True
                entry["dirty"] = True
        return names

    # output

    def render(self):
        """{file name: on-disk bytes} for every month; unchanged months keep their original bytes."""
        files = {}
        for (kind, month), entry in sorted(self.months.items()):
            name = file_name(kind, month, entry["compressed"])
            if not entry["dirty"] and name in self.original:
                files[name] = self.original[name]
                continue
            data = b"".join(line_bytes(line) for line in entry["lines"])
            files[name] = gzip_bytes(data) if entry["compressed"] else data
        return files

    def index_bytes(self, files):
        entries = {}
        for name, raw in sorted(files.items()):
            kind, month, compressed = _parse_name(name)
            entries[name] = {
                "bytes": len(raw),
                "lines": len(self.months[(kind, month)]["lines"]),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "compressed": compressed,
            }
        index = {
            "format_version": FORMAT_VERSION,
            "last_run_at": self.last_run_at(),
            "runs": len(self.runs()),
            "months": sorted(set(month for (_, month) in self.months)),
            "files": entries,
            "total_bytes": sum(len(raw) for raw in files.values()),
        }
        return document_bytes(index)


def _parse_name(name):
    match = FILE_RE.fullmatch(name)
    if match is None:
        raise schema.SchemaError("%r is not a month file name" % name)
    return match.group(1), match.group(2), match.group(3) is not None


def _parse_lines(kind, month, data, name):
    """The lines of one month file, each strictly parsed, validated and in canonical form."""
    if not data or not data.endswith(b"\n"):
        raise schema.SchemaError("%s: empty or not newline-terminated" % name)
    try:
        content = data.decode("ascii")
    except UnicodeDecodeError:
        raise schema.SchemaError("%s: not ASCII JSON lines" % name) from None
    lines = []
    previous = None
    for number, text_line in enumerate(content.split("\n")[:-1], 1):
        where = "%s line %d" % (name, number)
        obj = schema.loads(text_line, where)
        if kind == "events":
            EVENT_LINE.check(obj, where)
            if month_of(obj["observed_at"]) != month:
                raise schema.SchemaError("%s: observed_at is not in %s" % (where, month))
            if previous is not None and _event_order(obj) <= _event_order(previous):
                raise schema.SchemaError("%s: events are not in order" % where)
        else:
            if isinstance(obj, dict) and "run_at" in obj:
                SNAPSHOT_HEADER.check(obj, where)
                if month_of(obj["run_at"]) != month:
                    raise schema.SchemaError("%s: run_at is not in %s" % (where, month))
            elif previous is None:
                raise schema.SchemaError("%s: a snapshot file starts with a run header" % where)
            else:
                (SNAPSHOT_REMOVED if isinstance(obj, dict) and "removed" in obj else SNAPSHOT_REPO).check(obj, where)
                if "run_at" not in previous and _id_number(obj["id"]) <= _id_number(previous["id"]):
                    raise schema.SchemaError("%s: repositories of a run are not in id order" % where)
        if line_bytes(obj) != (text_line + "\n").encode("ascii"):
            raise schema.SchemaError("%s: not in canonical form" % where)
        lines.append(obj)
        previous = obj
    return lines

"""Site feed export: the observation store as the static page's feed.json and manifest.json.

Run files go through the same path as the dry run (schema.validate_run,
events.apply_run into a store.empty_state() or a state saved by an earlier
export, catalog.build_catalog); this module only reshapes the resulting
state into the records site/app.js and site/terminal.js read:

- feed: {generated_at, collection, repos[], events[]}, plus
  stale_after_minutes when the export is given one. Every record has a
  string `id` ("r<repository id>" for a repository, the canonical event id
  for an event); an event's `repo_id` is the id of its repository record.
- collection describes the newest applied run, copied from the state's
  collection.last_run: {complete, stopped, not_fetched, error_count,
  last_run_at}. complete is false and stopped names the reason ("budget",
  "rate_limited", ...) when that run did not finish; a run file without a
  collection_report counts as complete with no errors (events.apply_run).
- stale_after_minutes, when present, is how old generated_at may get before
  the page calls the feed stale (site/app.js reads data.stale_after_minutes
  and falls back to its own default when the key is absent).
- Text fields are plain text. The store and the catalog hold HTML-escaped
  text (text.escape_text); the page inserts text with textContent, so the
  escaping is undone here with html.unescape, which is an exact inverse of
  html.escape (bound_escaped never cuts inside an entity). Control
  characters stay stripped and mention forms stay neutralised.
- The only links are repository URLs rebuilt from the validated full name
  (https://github.com/owner/name). Events carry no URL of their own.
- Unknown values are null, never 0 or "", and every key is always present.
- Nothing is flagged sample.
- generated_at is the newest applied run's run_at, never the wall clock.
- The newest MAX_FEED_EVENTS events are kept (the page keeps the same
  number); repositories are capped at MAX_FEED_REPOS, always keeping those
  the kept events refer to.

Deterministic: same state and run files, same bytes (sorted keys, fixed
orderings); the export command refuses two run files with the same run_at,
so the argument order does not matter, and a state saved after some runs
then given the rest gives the same feed as all the runs from scratch.
"""

import hashlib
import html

from . import catalog, text

MAX_FEED_EVENTS = 500
MAX_FEED_REPOS = 2000
VERSION_HEX = 16

FEED_NAME = "feed.json"
MANIFEST_NAME = "manifest.json"


def repo_record_id(repository_id):
    return "r%d" % repository_id


def plain(value):
    """Undo text.escape_text for a renderer that uses textContent; empty text is unknown."""
    if value is None:
        return None
    value = html.unescape(value)
    return value if value.strip() else None


def _repo_record(entry):
    return {
        "id": repo_record_id(entry["id"]),
        "repo": entry["repo"],
        "owner": entry["owner"],
        "url": text.repository_url(entry["repo"]),
        "description": plain(entry["description"]),
        "game": plain(entry["game"]),
        "game_inferred": entry["game_inferred"],
        "game_inherited": entry["game_inherited"],
        "category": entry["category"],
        "toolkit": entry["toolkit"],
        "fork": entry["fork"],
        "parent": entry["parent"],
        "root": entry["root"],
        "ahead": entry["ahead"],
        "behind": entry["behind"],
        "comparison_state": entry["comparison_state"],
        "stars": entry["stars"],
        "commits_30d": entry["commits_30d"],
        "first_seen_at": entry["first_seen_at"],
        "last_commit_at": entry["last_commit_at"],
        "archived": entry["archived"],
        "collection_failing": entry["collection_failing"],
    }


def _event_record(event):
    return {
        "id": event["event_id"],
        "type": event["type"],
        "repo_id": repo_record_id(event["repository_id"]),
        "source_at": event["source_at"],
        "observed_at": event["observed_at"],
        "summary": plain(event["summary"]),
    }


def _newest_first(event):
    return (event["observed_at"], event["source_at"] or "", event["event_id"])


def _collection_record(state):
    last = state["collection"]["last_run"]
    return {
        "complete": last["complete"],
        "stopped": last["stopped"],
        "not_fetched": last["not_fetched"],
        "error_count": last["error_count"],
        "last_run_at": last["last_run_at"],
    }


def build_feed(state, stale_after_minutes=None):
    """The feed document for a state that has had at least one run applied."""
    if state["last_run_at"] is None:
        raise ValueError("no run has been applied")
    if state["collection"]["last_run"]["last_run_at"] != state["last_run_at"]:
        raise ValueError("collection.last_run does not describe the last applied run")
    events = sorted(state["events"], key=_newest_first, reverse=True)[:MAX_FEED_EVENTS]
    entries = catalog.build_catalog(state)
    referenced = set(e["repository_id"] for e in events)
    keep = [e for e in entries if e["id"] in referenced]
    room = MAX_FEED_REPOS - len(keep)
    keep += [e for e in entries if e["id"] not in referenced][: max(room, 0)]
    keep.sort(key=lambda e: e["id"])
    feed = {
        "generated_at": state["last_run_at"],
        "collection": _collection_record(state),
        "repos": [_repo_record(e) for e in keep],
        "events": [_event_record(e) for e in events],
    }
    if stale_after_minutes is not None:
        feed["stale_after_minutes"] = stale_after_minutes
    return feed


def build_manifest(feed, feed_bytes):
    """The manifest the page polls: its version changes exactly when the feed bytes change."""
    return {
        "feed": FEED_NAME,
        "generated_at": feed["generated_at"],
        "version": hashlib.sha256(feed_bytes).hexdigest()[:VERSION_HEX],
    }

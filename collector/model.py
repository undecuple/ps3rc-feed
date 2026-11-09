"""Canonical event model: types, deterministic ids and event construction.

Event ids are built from the repository id plus immutable source
identifiers (commit SHAs, release ids) or a per-repository transition
ordinal. They never contain repository text.

Field names follow the sample in site-draft/data/feed.json where the two
agree; the differences are listed in .local/STATUS.md.
"""

import hashlib

from . import text

REPOSITORY_DISCOVERED = "repository_discovered"
COMMITS_OBSERVED = "commits_observed"
RELEASE_PUBLISHED = "release_published"
REPOSITORY_ARCHIVED = "repository_archived"
COLLECTION_STALE = "collection_stale"
COLLECTION_RECOVERED = "collection_recovered"
GAME_ASSOCIATION_CHANGED = "game_association_changed"

LABELS = {
    REPOSITORY_DISCOVERED: "FOUND",
    COMMITS_OBSERVED: "COMMIT",
    RELEASE_PUBLISHED: "RELEASE",
    REPOSITORY_ARCHIVED: "ARCHIVED",
    COLLECTION_STALE: "STALE",
    COLLECTION_RECOVERED: "RECOVERED",
    GAME_ASSOCIATION_CHANGED: "RECLASSIFIED",
}

EVENT_FIELDS = (
    "event_id",
    "type",
    "repository_id",
    "repository",
    "owner",
    "game_key",
    "game_title",
    "game_inferred",
    "toolkit",
    "source_at",
    "observed_at",
    "title",
    "summary",
    "source_url",
    "details",
)


def commits_event_id(repository_id, shas):
    digest = hashlib.sha256("\n".join(sorted(shas)).encode("ascii")).hexdigest()
    return "repo-%d:commits:%s" % (repository_id, digest)


def release_event_id(repository_id, release_id):
    return "repo-%d:release:%d" % (repository_id, release_id)


def discovered_event_id(repository_id):
    return "repo-%d:discovered" % repository_id


def ordinal_event_id(repository_id, kind, ordinal):
    """Id for a state transition: the n-th transition of `kind` seen for the repository."""
    return "repo-%d:%s:%d" % (repository_id, kind, ordinal)


def make_event(event_type, obs, observed_at, source_at, summary, details, event_id):
    """Build an event from an observation. Renderer-facing text is escaped."""
    game_title = text.escape_text(obs["game"]["title"], text.MAX_TITLE)
    title = "%s %s %s" % (LABELS[event_type], game_title or "Unclassified", obs["full_name"])
    return {
        "event_id": event_id,
        "type": event_type,
        "repository_id": obs["repository_id"],
        "repository": obs["full_name"],
        "owner": obs["owner"],
        "game_key": obs["game"]["key"],
        "game_title": game_title,
        "game_inferred": True,
        "toolkit": obs["toolkit"]["name"],
        "source_at": source_at,
        "observed_at": observed_at,
        "title": title,
        "summary": summary,
        "source_url": text.repository_url(obs["full_name"]),
        "details": details,
    }

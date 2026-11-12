"""Discord notifications, planning only: which feed events become which messages.

This module reads a parsed feed.json (the export format) and the delivery
state, and returns the messages a webhook would be sent. It opens no
socket, reads no clock and holds no webhook address: sending is a separate
step, so everything here is a pure function of its input and the same feed
and state always give the same plan.

What is sent, newest-first inside each kind, oldest message first:

- repository_discovered and release_published: one message each, up to
  MAX_INDIVIDUAL per plan; the rest are folded into one overflow message
  that only counts them and points at the site.
- commits_observed: one digest message listing up to MAX_DIGEST_REPOSITORIES
  repositories, with the number of further ones.
- Every other event type (forks, fork activity, game association changes,
  stale notices) is never sent; it is only marked as seen.

State is {"format_version": 1, "seen": [...event ids...], "attempts":
{event id: failed sends}}. An event in "seen" is never planned again. The
first plan for a feed with no state is a baseline: no messages, and every
event in the feed becomes seen, so enabling the notifications never posts
the history. apply_delivery marks the events of delivered messages seen,
counts a failure against the events of a message whose send failed (a message
not attempted counts nothing) and gives such an event up (seen, no more sends)
after MAX_ATTEMPTS failures.
"seen" keeps the newest MAX_SEEN ids in feed order, which is enough because
a feed holds at most export.MAX_FEED_EVENTS events and an id that left the
feed cannot come back.

Message text: fixed templates around values that come from repository text
(names, descriptions, release tags). Those values are plain text only:
controls stripped, mention forms neutralised, links removed, Markdown
punctuation backslash-escaped, bounded. Repository links are rebuilt from
the validated full name and written in angle brackets so Discord shows no
preview. Every payload sets allowed_mentions to an empty parse list, so
nothing a repository writes can ping anyone.
"""

import re

from . import text

FORMAT_VERSION = 1
MAX_INDIVIDUAL = 5
MAX_DIGEST_REPOSITORIES = 10
MAX_ATTEMPTS = 3
MAX_SEEN = 2000
MAX_CONTENT = 1800
SITE_URL = "https://undecuple.github.io/ps3rc-feed/"
USERNAME = "ps3rc-feed"
SUPPRESS_EMBEDS = 4

INDIVIDUAL = ("repository_discovered", "release_published")
DIGEST = "commits_observed"

EVENT_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
_LINK_RE = re.compile(r"\S*://\S*|\bwww\.\S+")
_MARKDOWN_RE = re.compile(r"([\\*_~`|\[\]()<])")
_SPACE_RE = re.compile(r"\s+")


class StateError(ValueError):
    """The delivery state is not in the documented shape."""


def empty_state():
    return {"format_version": FORMAT_VERSION, "seen": [], "attempts": {}}


def validate_state(state):
    """The state, checked; raises StateError (fixed wording, no values from the file)."""
    if not isinstance(state, dict) or set(state) != {"format_version", "seen", "attempts"}:
        raise StateError("delivery state must be an object with format_version, seen and attempts")
    if state["format_version"] != FORMAT_VERSION:
        raise StateError("delivery state format_version is not %d" % FORMAT_VERSION)
    seen, attempts = state["seen"], state["attempts"]
    if not isinstance(seen, list) or len(seen) > MAX_SEEN or not all(isinstance(i, str) and EVENT_ID_RE.match(i) for i in seen):
        raise StateError("delivery state seen must be a list of at most %d event ids" % MAX_SEEN)
    if len(set(seen)) != len(seen):
        raise StateError("delivery state seen lists an id twice")
    if not isinstance(attempts, dict) or len(attempts) > MAX_SEEN:
        raise StateError("delivery state attempts must be an object of at most %d entries" % MAX_SEEN)
    for key, value in attempts.items():
        if not (isinstance(key, str) and EVENT_ID_RE.match(key)) or type(value) is not int or not 1 <= value < MAX_ATTEMPTS:
            raise StateError("delivery state attempts must map event ids to counts from 1 to %d" % (MAX_ATTEMPTS - 1))
    return state


def clean(value, max_len):
    """Repository text made safe for a Discord message: one line of plain text."""
    if not isinstance(value, str):
        return ""
    value = text.neutralise_mentions(text.strip_controls(value))
    value = _LINK_RE.sub("[link removed]", value)
    value = _SPACE_RE.sub(" ", value).strip()
    value = text.bound(value, max_len)
    return _MARKDOWN_RE.sub(r"\\\1", value)


def _repo_index(feed):
    repos = feed.get("repos") if isinstance(feed, dict) else None
    index = {}
    for rec in repos if isinstance(repos, list) else []:
        if isinstance(rec, dict) and isinstance(rec.get("id"), str):
            index[rec["id"]] = rec
    return index


def _events(feed):
    events = feed.get("events") if isinstance(feed, dict) else None
    out = []
    for e in events if isinstance(events, list) else []:
        if isinstance(e, dict) and isinstance(e.get("id"), str) and EVENT_ID_RE.match(e["id"]) and isinstance(e.get("type"), str):
            out.append(e)
    return out


def _observed(event):
    value = event.get("observed_at")
    return value if isinstance(value, str) else ""


def _name(rec):
    name = rec.get("repo") if rec else None
    return clean(name, 100) if isinstance(name, str) and name else "unknown repository"


def _link(rec):
    url = rec.get("url") if rec else None
    return "<%s>" % url if isinstance(url, str) and URL_RE.match(url) and not rec.get("sample") else None


def _game(rec):
    if rec and rec.get("game_confidence") in ("strong", "medium") and isinstance(rec.get("game"), str) and rec["game"]:
        return clean(rec["game"], 80)
    return "game unknown"


def _payload(content):
    return {
        "username": USERNAME,
        "content": text.bound(content, MAX_CONTENT),
        "allowed_mentions": {"parse": []},
        "flags": SUPPRESS_EMBEDS,
    }


def _lines(head, *rest):
    return "\n".join([head] + [line for line in rest if line])


def _individual(event, rec):
    name, game, link = _name(rec), _game(rec), _link(rec)
    if event["type"] == "repository_discovered":
        kind = "fork" if rec and rec.get("fork") is True else "repository"
        head = "[NEW] %s — %s" % (game, name)
        body = "New %s found by an automated search. Game association is inferred and may be wrong." % kind
    else:
        head = "[RELEASE] %s — %s" % (game, name)
        body = clean(event.get("summary"), 120) or "Release published."
    return _lines(head, body, "Source: %s" % link if link else None)


def _digest(events, repos):
    counts = {}
    order = []
    for e in events:
        rid = e.get("repo_id")
        if rid not in counts:
            counts[rid] = 0
            order.append(rid)
        counts[rid] += 1
    shown = order[:MAX_DIGEST_REPOSITORIES]
    lines = ["[COMMITS] Activity on %d %s (automated, unverified)" % (len(order), "repository" if len(order) == 1 else "repositories")]
    for rid in shown:
        lines.append("- %s: %s" % (_name(repos.get(rid)), _game(repos.get(rid))))
    if len(order) > len(shown):
        lines.append("and %d more" % (len(order) - len(shown)))
    lines.append("Details: <%s>" % SITE_URL)
    return "\n".join(lines)


def _overflow(events):
    new = sum(1 for e in events if e["type"] == "repository_discovered")
    rel = len(events) - new
    parts = []
    if new:
        parts.append("%d more new %s" % (new, "repository" if new == 1 else "repositories"))
    if rel:
        parts.append("%d more %s" % (rel, "release" if rel == 1 else "releases"))
    return "[MORE] %s. Details: <%s>" % (" and ".join(parts), SITE_URL)


def plan(feed, state=None):
    """What to send for this feed and state.

    Returns {"baseline": bool, "messages": [{"kind", "event_ids", "payload"}],
    "state": the state with every event of the feed that is not planned for
    sending marked seen}. With state None this is the baseline: no messages,
    every event seen. Planned events are not in the returned state yet; give
    the plan and the delivery results to apply_delivery.
    """
    events = _events(feed)
    ids = [e["id"] for e in events]
    if state is None:
        base = empty_state()
        base["seen"] = _trim(ids)
        return {"baseline": True, "messages": [], "state": base}
    validate_state(state)
    seen = set(state["seen"])
    fresh = [e for e in events if e["id"] not in seen]
    repos = _repo_index(feed)
    individual = sorted((e for e in fresh if e["type"] in INDIVIDUAL), key=lambda e: (_observed(e), e["id"]))
    digest = sorted((e for e in fresh if e["type"] == DIGEST), key=lambda e: (_observed(e), e["id"]))
    ignored = [e["id"] for e in fresh if e["type"] not in INDIVIDUAL and e["type"] != DIGEST]
    messages = []
    for e in individual[:MAX_INDIVIDUAL]:
        messages.append({"kind": e["type"], "event_ids": [e["id"]], "payload": _payload(_individual(e, repos.get(e.get("repo_id"))))})
    rest = individual[MAX_INDIVIDUAL:]
    if rest:
        messages.append({"kind": "overflow", "event_ids": [e["id"] for e in rest], "payload": _payload(_overflow(rest))})
    if digest:
        messages.append({"kind": DIGEST, "event_ids": [e["id"] for e in digest], "payload": _payload(_digest(digest, repos))})
    attempts = {k: v for k, v in state["attempts"].items() if k in set(ids)}
    new_state = {"format_version": FORMAT_VERSION, "seen": _trim(state["seen"] + ignored), "attempts": attempts}
    return {"baseline": False, "messages": messages, "state": new_state}


def _trim(ids):
    return ids[-MAX_SEEN:]


def apply_delivery(plan_, delivered):
    """The state after sending: delivered is a list with one result per message of the plan, in order.

    True: the events of the message become seen. False: its events get one
    failed attempt and stay unseen so the next plan tries again, until they
    have MAX_ATTEMPTS failures, when they are given up (seen). None: the
    message was not attempted (the run stopped before it); its events stay
    unseen and no attempt is counted. The plan's own state is not changed.
    """
    messages = plan_["messages"]
    if len(delivered) != len(messages):
        raise ValueError("one delivery result per message is required")
    state = plan_["state"]
    seen = list(state["seen"])
    attempts = dict(state["attempts"])
    for message, ok in zip(messages, delivered):
        if ok is None:
            continue
        for event_id in message["event_ids"]:
            if ok:
                attempts.pop(event_id, None)
                seen.append(event_id)
                continue
            failed = attempts.get(event_id, 0) + 1
            if failed >= MAX_ATTEMPTS:
                attempts.pop(event_id, None)
                seen.append(event_id)
            else:
                attempts[event_id] = failed
    seen = _trim(seen)
    keep = set(seen)
    return {"format_version": FORMAT_VERSION, "seen": seen, "attempts": {k: v for k, v in sorted(attempts.items()) if k not in keep}}

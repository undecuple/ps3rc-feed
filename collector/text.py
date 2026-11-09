"""Text handling for renderer-facing output.

Repository text (descriptions, READMEs, commit messages, tags) is untrusted.
It is stored verbatim in the observation store as data and never executed
or interpreted. Anything that goes into a renderer-facing field (summary,
title, game title, catalog description) passes through `escape_text`:
control and bidirectional-format characters are removed, the length is
bounded, Discord mention forms are neutralised and HTML is escaped. The
result is HTML-escaped text; renderers must not escape it a second time
and must not insert it as raw HTML either.

URLs are never taken from repository text. They are rebuilt from the
validated full name and immutable identifiers.
"""

import html
import re
import unicodedata
from urllib.parse import quote

MAX_TEXT = 300
MAX_TITLE = 120
MAX_KEY = 64

GITHUB = "https://github.com/"

_MENTION_RE = re.compile(r"@(?=everyone\b|here\b|&|!|[0-9])")
_WS_RE = re.compile(r"[\r\n\t\f\v]+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SPACES_RE = re.compile(r" {2,}")


def strip_controls(value):
    """Remove control (Cc) and format (Cf) characters; fold line breaks to spaces."""
    value = _WS_RE.sub(" ", value)
    out = []
    for ch in value:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        out.append(ch)
    return _SPACES_RE.sub(" ", "".join(out)).strip()


def bound(value, max_len):
    if len(value) <= max_len:
        return value
    return value[: max_len - 3].rstrip() + "..."


def neutralise_mentions(value):
    """Break Discord mention forms (@everyone, @here, <@id>, <@&role>, <@!id>)."""
    return _MENTION_RE.sub("(at)", value)


def plain_text(value, max_len=MAX_TEXT):
    """Sanitised plain text: controls stripped, mentions neutralised, bounded. Not HTML-escaped."""
    if value is None:
        return None
    return bound(neutralise_mentions(strip_controls(str(value))), max_len)


def escape_text(value, max_len=MAX_TEXT):
    """Renderer-safe text: `plain_text` then HTML-escaped (quotes included)."""
    if value is None:
        return None
    return html.escape(plain_text(value, max_len), quote=True)


def slug(value, max_len=MAX_KEY):
    """Lower-case grouping key from a title; None when nothing is left."""
    if value is None:
        return None
    key = _SLUG_RE.sub("-", strip_controls(value).lower()).strip("-")
    key = key[:max_len].strip("-")
    return key or None


def repository_url(full_name):
    return GITHUB + full_name


def commit_url(full_name, sha):
    return "%s%s/commit/%s" % (GITHUB, full_name, sha)


def release_url(full_name, tag):
    return "%s%s/releases/tag/%s" % (GITHUB, full_name, quote(plain_text(tag, 256), safe=""))


# --- classification -------------------------------------------------------

_HEADING_RE = re.compile(r"^\s{0,3}#{1,2}\s+(.+?)\s*#*\s*$")
_TITLE_PUNCT = " '’:-.,!?&()+"


def plausible_title(value):
    """True when a heading looks like a game title.

    Letters, digits, spaces and light punctuation only, at least one
    letter. Shell text, URLs, markup and code never qualify, so a hostile
    README leaves the repository unclassified instead of naming a "game".
    """
    if not value or len(value) > MAX_TITLE:
        return False
    if not any(ch.isalpha() for ch in value):
        return False
    return all(ch.isalnum() or ch in _TITLE_PUNCT for ch in value)


def readme_title(readme_excerpt):
    """First level-1/2 Markdown heading of the README excerpt, if it is a plausible title."""
    if not readme_excerpt:
        return None
    for line in readme_excerpt.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            title = plain_text(m.group(1), MAX_TITLE)
            return title if plausible_title(title) else None
    return None


def classify(full_name, description, readme_excerpt):
    """Heuristic category, inferred game and toolkit for a repository.

    Returns (category, game, toolkit) where game and toolkit are the dicts
    stored in the observation. Everything here is an automated guess and is
    always marked inferred and unverified.
    """
    name = full_name.split("/", 1)[1].lower()
    haystack = " ".join(x for x in (name, description or "", readme_excerpt or "")).lower()
    toolkit_name = "ps3recomp" if "ps3recomp" in haystack else None
    toolkit = {"name": toolkit_name, "confidence": "weak" if toolkit_name else "unknown"}

    if "ps3recomp" in name:
        game = {"key": None, "title": None, "basis": None, "confidence": "unknown", "inferred": True, "verified": False}
        return "framework", game, toolkit

    title = readme_title(readme_excerpt)
    key = slug(title)
    if key:
        game = {
            "key": key,
            "title": title,
            "basis": "readme_title",
            "confidence": "weak",
            "inferred": True,
            "verified": False,
        }
        return "game", game, toolkit
    game = {"key": None, "title": None, "basis": None, "confidence": "unknown", "inferred": True, "verified": False}
    return "unclassified", game, toolkit

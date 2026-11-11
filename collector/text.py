"""Text handling for renderer-facing output.

Repository text (descriptions, READMEs, commit messages, tags) is untrusted.
It is stored in the observation store as data, bounded at load time, and
never executed or interpreted. Anything that goes into a renderer-facing
field (summary, title, game title, catalog description) passes through
`escape_text`, in this order:

1. NFKC normalisation, then removal of control, format, surrogate, private
   use, unassigned, line/paragraph separator and invisible filler
   characters; line breaks fold to spaces; runs of combining marks are
   capped.
2. Discord mention forms are neutralised (`@everyone...`, `@here...` in any
   case and with any suffix, `<@`, `<#`).
3. HTML escaping with quotes.
4. Length bound applied after escaping, cutting only between entities.

The result is HTML-escaped text; renderers must not escape it a second
time and must not insert it as raw HTML either.

URLs are never taken from repository text. They are rebuilt from the
validated full name and immutable identifiers. Every parse here is linear
in the input length (no backtracking regular expressions over free text).
"""

import html
import re
import unicodedata
from urllib.parse import quote

MAX_TEXT = 300
MAX_TITLE = 120
MAX_KEY = 64
MAX_COMBINING_RUN = 2

GITHUB = "https://github.com/"

# Zero-width and filler characters that are not in a stripped category.
_INVISIBLE = frozenset(
    [
        "ᅟ",  # Hangul choseong filler
        "ᅠ",  # Hangul jungseong filler
        "ㅤ",  # Hangul filler
        "ﾠ",  # halfwidth Hangul filler
        "⠀",  # braille pattern blank
        "­",  # soft hyphen
    ]
)
_STRIP_CATEGORIES = frozenset(["Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"])
_COMBINING = frozenset(["Mn", "Me"])

_MENTION_RE = re.compile(r"@(?=everyone|here)", re.IGNORECASE)
_LT_MENTION_RE = re.compile(r"<(?=[@#])")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def strip_controls(value):
    """Normalise and remove invisible characters; fold line breaks to spaces."""
    value = unicodedata.normalize("NFKC", str(value))
    out = []
    combining = 0
    prev_space = False
    for ch in value:
        if ch in ("\r", "\n", "\t", "\f", "\v"):
            ch = " "
        if ch in _INVISIBLE:
            continue
        cat = unicodedata.category(ch)
        if cat in _STRIP_CATEGORIES:
            continue
        if cat in _COMBINING:
            combining += 1
            if combining > MAX_COMBINING_RUN:
                continue
        else:
            combining = 0
        if ch == " ":
            if prev_space:
                continue
            prev_space = True
        else:
            prev_space = False
        out.append(ch)
    return "".join(out).strip()


def neutralise_mentions(value):
    """Break Discord mention forms: @everyone*, @here* (any case), <@...>, <#...>."""
    value = _MENTION_RE.sub("(at)", value)
    value = _LT_MENTION_RE.sub("<(ref)", value)
    return value


def bound_escaped(value, max_len):
    """Cut HTML-escaped text to max_len without splitting an entity."""
    if len(value) <= max_len:
        return value
    cut = value[: max_len - 3]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:]:
        cut = cut[:amp]
    return cut.rstrip() + "..."


def bound(value, max_len):
    """Cut plain text to max_len."""
    if len(value) <= max_len:
        return value
    return value[: max_len - 3].rstrip() + "..."


def plain_text(value, max_len=MAX_TEXT):
    """Sanitised plain text: controls stripped, mentions neutralised, bounded. Not HTML-escaped."""
    if value is None:
        return None
    return bound(neutralise_mentions(strip_controls(value)), max_len)


def escape_text(value, max_len=MAX_TEXT):
    """Renderer-safe text: strip, neutralise, HTML-escape (quotes too), then bound."""
    if value is None:
        return None
    escaped = html.escape(neutralise_mentions(strip_controls(value)), quote=True)
    return bound_escaped(escaped, max_len)


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
    """Release page URL. The tag is control-stripped and percent-encoded, nothing else.

    A tag that is empty, '.' or '..' after stripping gets the releases list
    instead of a path component.
    """
    clean = strip_controls(tag)[:256]
    if clean in ("", ".", ".."):
        return "%s%s/releases" % (GITHUB, full_name)
    return "%s%s/releases/tag/%s" % (GITHUB, full_name, quote(clean, safe=""))


# --- classification -------------------------------------------------------

_TITLE_PUNCT = frozenset(" '’:-.,!?&()+")
MAX_HEADING_LINE = 1024


def heading_text(line):
    """Text of a level-1 or level-2 ATX heading, or None. Linear scan, no regex."""
    line = line[:MAX_HEADING_LINE]
    i = 0
    while i < 3 and i < len(line) and line[i] == " ":
        i += 1
    j = i
    while j < len(line) and line[j] == "#":
        j += 1
    level = j - i
    if level < 1 or level > 2:
        return None
    if j >= len(line) or line[j] not in (" ", "\t"):
        return None
    rest = line[j:].strip()
    k = len(rest)
    while k > 0 and rest[k - 1] == "#":
        k -= 1
    rest = rest[:k].rstrip()
    return rest or None


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


def has_cjk(value):
    """True when value holds a Han, kana or Hangul character (such a heading is not taken as a title)."""
    return any(any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES) for ch in value)


_CJK_RANGES = ((0x1100, 0x11FF), (0x2E80, 0x9FFF), (0xA960, 0xA97F), (0xAC00, 0xD7FF), (0xF900, 0xFAFF), (0xFF66, 0xFF9F), (0x20000, 0x3FFFF))
_DASHES = ("\u2014", "\u2013")
# Words that say what was done to a game or where it runs. A ", " / ": " / " - " clause made of
# text holding one of them is a description, not part of the title.
DESCRIPTIVE_WORDS = frozenset(
    [
        "port", "ported", "recomp", "recompiled", "recompilation", "recompiler", "decomp", "decompilation",
        "native", "pc", "windows", "linux", "macos", "mac", "ipad", "static", "statically", "unofficial",
        "experimental", "experiment", "wip", "progress", "research", "emulator", "emulation", "playable",
        "x86", "x64", "arm64",
    ]
)


def _words(value):
    return [w for w in _SLUG_RE.split(fold(value).lower()) if w]


# A "for <platform>" tail and dangling connector words are cut from the end ("Killzone for Windows" -> "Killzone").
PLATFORM_WORDS = frozenset(["windows", "pc", "mac", "linux", "macos", "android", "ps3"])
CONNECTOR_WORDS = frozenset(["for", "in", "on", "of", "the", "and", "with", "from", "to", "a", "an"])


def _strip_suffixes(value):
    """Drop trailing parentheticals, RESTATED_SUFFIXES words, "for <platform>" tails and dangling
    CONNECTOR_WORDS ("Foo (PS3) Native PC Port" -> "Foo", "Foo HD for Windows" -> "Foo HD")."""
    for _ in range(MAX_TITLE):
        value = value.rstrip(" :,-")
        if value.endswith(")") and "(" in value:
            value = value[: value.rfind("(")]
            continue
        cut = value.rfind(" ")
        last = value[cut + 1 :].lower()
        if cut != -1 and last in RESTATED_SUFFIXES and not set(_words(value[:cut])) <= RESTATED_SUFFIXES:
            value = value[:cut]
            continue
        before = value[:cut].rstrip()
        if cut != -1 and last in PLATFORM_WORDS and before.lower().endswith(" for"):
            value = before[:-4]
            continue
        if cut != -1 and last in CONNECTOR_WORDS:
            value = before
            continue
        return value
    return value


def clean_title(value):
    """The game-naming part of a heading: before an em/en dash, without restated suffixes,
    and before a ", " / ": " / " - " clause whose rest is descriptive. Linear passes only."""
    for dash in _DASHES:
        if dash in value:
            value = value[: value.find(dash)]
    value = _strip_suffixes(value.strip())
    for sep in (", ", ": ", " - "):
        at = value.find(sep)
        if at > 0 and any(w in DESCRIPTIVE_WORDS for w in _words(value[at + len(sep) :])):
            value = _strip_suffixes(value[:at])
    return value.strip()


def readme_heading(readme_excerpt):
    """First level-1/2 Markdown heading of the README excerpt as plain text, uncleaned and unchecked."""
    if not readme_excerpt:
        return None
    for line in readme_excerpt.splitlines():
        heading = heading_text(line)
        if heading is not None:
            return plain_text(heading, MAX_TITLE)
    return None


def readme_title(readme_excerpt):
    """readme_heading, cleaned (clean_title), if it is then a plausible title without CJK characters."""
    heading = readme_heading(readme_excerpt)
    if heading is None:
        return None
    title = clean_title(heading)
    if title != heading and len(title) < 2:
        return None  # cleaning left a stub ("X for Windows" -> "X")
    return title if plausible_title(title) and not has_cjk(title) else None


# Headings that name a README section, never a game. Compared as slugs, so
# case and punctuation do not matter ("Table of Contents:" -> table-of-contents).
GENERIC_HEADINGS = frozenset(
    [
        "table-of-contents", "contents", "toc", "docs", "documentation", "readme",
        "introduction", "overview", "about", "features", "usage", "installation",
        "getting-started", "quick-start", "quickstart", "license", "changelog",
        "description", "build", "building", "requirements", "project-structure",
        "notes", "links", "credits", "screenshots", "demo", "download", "status",
        "setup", "list", "lists", "tools", "tool", "scripts", "examples", "example", "faq", "help", "wiki",
        "roadmap", "contributing", "support", "disclaimer", "warning", "summary", "home", "index", "todo",
        "o-que-e", "what-is-this", "what-is-it", "que-es", "sobre", "projeto", "proyecto",
    ]
)
# A heading starting with one of these asks a question or opens a section ("What is ...", "O que é ...").
GENERIC_PREFIXES = ("what-is-", "o-que-e-", "que-es-", "how-to-", "about-")
# A heading holding one of these words names a tool or a collection, not a game.
GENERIC_WORDS = frozenset(
    ["emulator", "emulators", "bucket", "trending", "launcher", "launchers", "engine", "framework", "sdk", "toolkit", "toolchain", "dotfiles"]
)
# Trailing words that only say what was done to a game, not which game.
RESTATED_SUFFIXES = frozenset(["recomp", "recompiled", "recompilation", "port", "pc", "native", "ps3", "windows", "linux", "macos"])


def fold(value):
    """NFKD with combining marks dropped ("O que é" -> "O que e"), for comparisons only."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch))


def generic_heading(title):
    """True when title names a README section, a question, a tool or a list rather than a game."""
    key = slug(fold(title)) or ""
    if key in GENERIC_HEADINGS or key.startswith(GENERIC_PREFIXES):
        return True
    return any(word in GENERIC_WORDS for word in key.split("-"))


def _core(key):
    """Slug with trailing RESTATED_SUFFIXES words removed; may be empty."""
    words = key.split("-")
    while words and words[-1] in RESTATED_SUFFIXES:
        words.pop()
    return "-".join(words)


def game_title(repo_name, title, raw=None):
    """title when it can name a game in repository repo_name, else None.

    A title is rejected when any of these hold (all on text.slug forms):
    - it is a GENERIC_HEADINGS entry (a README section name);
    - it or the repository name starts with "awesome" (a curated list);
    - it equals the repository-name slug (the name restated, no evidence);
    - after dropping trailing RESTATED_SUFFIXES words from the title only,
      what is left is non-empty and equals the repository-name slug
      ("Haze PC Port" in haze). A title made only of those words is not
      rejected by this rule.
    The last two rules also apply to raw, the heading before clean_title,
    so "Haze Recomp" in haze-recomp (cleaned to "Haze") is still rejected.
    Only the title is trimmed, so a shorter title stays: "Skyfall" in
    skyfall-recomp is kept. Slugs keep word breaks, so "Firefall 3
    Recompiled" (core firefall-3) in firefall3-port stays a game.
    """
    key = slug(title)
    name_key = slug(repo_name) or ""
    if key is None or generic_heading(title):
        return None
    if key.startswith("awesome") or name_key.startswith("awesome"):
        return None
    for value in (key, slug(raw) if raw is not None else None):
        if value is None:
            continue
        if value == name_key:
            return None
        core = _core(value)
        if core and core == name_key:
            return None
    return title


def classify(full_name, description, readme_excerpt, aliases=None):
    """Heuristic category, inferred game and toolkit for a repository.

    Returns (category, game, toolkit) where game and toolkit are the dicts
    stored in the observation. Everything here is an automated guess and is
    always marked inferred and unverified. The reviewed alias list
    (collector.games, config/games.json) is tried first: repository name,
    description, README title, first hit wins; a hit gives the entry's key
    and title, basis alias_name / alias_description (confidence strong) or
    alias_readme_title (medium), and matched_alias. Otherwise the first
    README heading, when game_title accepts it, is used with basis
    readme_title and confidence low: unreviewed, and hidden by default.
    """
    from . import games

    if aliases is None:
        aliases = games.default()
    name = full_name.split("/", 1)[1]
    haystack = " ".join(x for x in (name.lower(), description or "", readme_excerpt or "")).lower()
    toolkit_name = "ps3recomp" if "ps3recomp" in haystack else None
    toolkit = {"name": toolkit_name, "confidence": "weak" if toolkit_name else "unknown"}

    unknown = {"key": None, "title": None, "basis": None, "confidence": "unknown", "inferred": True, "verified": False, "matched_alias": None}
    if "ps3recomp" in name.lower():
        return "framework", unknown, toolkit

    heading = readme_title(readme_excerpt)
    hit = aliases.match(name, description, heading)
    if hit is not None:
        entry, basis, matched = hit
        game = dict(unknown, key=entry["key"], title=entry["title"], basis=basis, confidence=games.CONFIDENCE[basis], matched_alias=matched)
        return "game", game, toolkit
    title = game_title(name.lower(), heading, readme_heading(readme_excerpt))
    key = slug(title)
    if key:
        return "game", dict(unknown, key=key, title=title, basis="readme_title", confidence="low"), toolkit
    return "unclassified", unknown, toolkit

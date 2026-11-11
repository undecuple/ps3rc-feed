"""Reviewed game aliases (config/games.json) and the token matcher that applies them.

The file is a reviewed list, changed only through a pull request (README,
"Game aliases"). Each entry names one game:

  {"key": "god-of-war-ascension", "title": "God of War: Ascension",
   "aliases": ["gow ascension", "god of war ascension"],
   "name_patterns": ["ascension-r"], "platform": "PS3"}

Matching is case-insensitive and token-bounded. Text is folded (NFKD,
accents dropped, lower case) and split into tokens on every character that
is not a letter or digit and between letters and digits ("dod3" is dod, 3).
For the repository name a second token list also splits camelCase
("GodOfWarAscension"), and either list may match. An alias matches when its
tokens appear as a contiguous run of tokens, so "gow" never matches
"gowild". A name pattern matches the whole repository name, token for token,
where "*" stands for exactly one token.

The fields are tried in order and the first field with a hit wins: the
repository name (patterns first, then aliases), the description, then the
README title. Inside one field the alias with the most tokens wins ("infamous
2" over "infamous"), then the earlier entry in the file.

Every parse is linear in the input (no backtracking expressions over
repository text). Error messages name paths and counts, never values from
the file.
"""

import hashlib
import json
import os
import re

from . import schema, text
from .paths import REPO_ROOT

DEFAULT_PATH = os.path.join(REPO_ROOT, "config", "games.json")
MAX_GAMES = 500
MAX_ALIASES = 32
MAX_PATTERNS = 16
MAX_ALIAS = 64
MAX_TOKENS = 12
MAX_FIELD = 4096

BASES = ("alias_name", "alias_description", "alias_readme_title")
CONFIDENCE = {"alias_name": "strong", "alias_description": "strong", "alias_readme_title": "medium"}

KEY_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
ALIAS_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9'.:&+-]| (?=[A-Za-z0-9'.:&+-]))*\Z")
PATTERN_RE = re.compile(r"(?:[a-z0-9]+|\*)(?:-(?:[a-z0-9]+|\*))*\Z")
PLATFORM_RE = re.compile(r"[A-Z0-9][A-Za-z0-9 ]{0,15}\Z")

_SPLIT_RE = re.compile(r"[a-z]+|[0-9]+")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")

ENTRY = schema.Obj(
    {
        "key": schema.Str(pattern=KEY_RE, max_len=text.MAX_KEY),
        "title": schema.Str(max_len=text.MAX_TITLE),
        "aliases": schema.List(schema.Str(pattern=ALIAS_RE, max_len=MAX_ALIAS), MAX_ALIASES),
        "name_patterns": schema.List(schema.Str(pattern=PATTERN_RE, max_len=MAX_ALIAS), MAX_PATTERNS),
        "platform": schema.Str(pattern=PLATFORM_RE, max_len=16),
    },
    optional=("name_patterns", "platform"),
)
FILE = schema.Obj({"_comment": schema.Str(max_len=1024), "games": schema.List(ENTRY, MAX_GAMES)}, optional=("_comment",))
FILE_ERROR = "games: file is not strict JSON (syntax error, duplicate key, fractional or non-finite number)"


def _fold(value):
    """text.fold, bounded; case kept."""
    return text.fold(str(value)[:MAX_FIELD])


def tokens(value):
    """Lower-case letter runs and digit runs of value."""
    return _SPLIT_RE.findall(_fold(value).lower())


def name_token_lists(name):
    """Token lists of a repository name: plain, and with camelCase split (deduplicated)."""
    plain = tokens(name)
    camel = tokens(_CAMEL_RE.sub(" ", _fold(name)))
    return [plain] if camel == plain else [plain, camel]


def _pattern_tokens(pattern):
    out = []
    for part in pattern.split("-"):
        out.extend(["*"] if part == "*" else tokens(part))
    return tuple(out)


class Games:
    """A validated alias list with lookup tables; `fingerprint` identifies its content."""

    def __init__(self, entries):
        self.entries = entries
        self.by_first = {}
        self.patterns = []
        for order, entry in enumerate(entries):
            for alias in entry["aliases"]:
                toks = tuple(tokens(alias))
                self.by_first.setdefault(toks[0], []).append((toks, order, alias))
            for pattern in entry.get("name_patterns", []):
                self.patterns.append((_pattern_tokens(pattern), order, pattern))
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.fingerprint = hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]

    def _best_alias(self, token_lists):
        best = None
        for toks in token_lists:
            for i, tok in enumerate(toks):
                for alias_toks, order, alias in self.by_first.get(tok, ()):
                    if tuple(toks[i : i + len(alias_toks)]) == alias_toks:
                        rank = (-len(alias_toks), order)
                        if best is None or rank < best[0]:
                            best = (rank, order, alias)
        return None if best is None else (best[1], best[2])

    def _pattern(self, token_lists):
        for pattern_toks, order, pattern in self.patterns:
            for toks in token_lists:
                if len(toks) == len(pattern_toks) and all(p == "*" or p == t for p, t in zip(pattern_toks, toks)):
                    return order, pattern
        return None

    def match(self, name, description, readme_title):
        """(entry, basis, matched alias or pattern) for the first field with a hit, or None."""
        names = name_token_lists(name)
        hit = self._pattern(names) or self._best_alias(names)
        if hit is not None:
            return self.entries[hit[0]], "alias_name", hit[1]
        for value, basis in ((description, "alias_description"), (readme_title, "alias_readme_title")):
            if value:
                hit = self._best_alias([tokens(value)])
                if hit is not None:
                    return self.entries[hit[0]], basis, hit[1]
        return None


def _hostile(value):
    """True when value would change under the renderer's text rules, or is not a plain title."""
    return text.strip_controls(value) != value or text.neutralise_mentions(value) != value or not text.plausible_title(value)


def validate(data):
    """Check a parsed games file; returns a Games. Raises schema.SchemaError."""
    if not isinstance(data, dict):
        raise schema.SchemaError("games: expected an object")
    if set(data) - set(FILE.fields):
        raise schema.SchemaError("games: %d unknown field(s)" % len(set(data) - set(FILE.fields)))
    entries = data.get("games")
    if isinstance(entries, list):
        for i, entry in enumerate(entries):
            if isinstance(entry, dict) and set(entry) - set(ENTRY.fields):
                raise schema.SchemaError("games.games[%d]: %d unknown field(s)" % (i, len(set(entry) - set(ENTRY.fields))))
    FILE.check(data, "games")
    keys, titles, aliases, patterns = set(), set(), set(), set()
    for i, entry in enumerate(data["games"]):
        path = "games.games[%d]" % i
        if entry["key"] in keys:
            raise schema.SchemaError("%s.key: duplicate key" % path)
        keys.add(entry["key"])
        if _hostile(entry["title"]) or text.has_cjk(entry["title"]):
            raise schema.SchemaError("%s.title: not a plain title" % path)
        if entry["title"].casefold() in titles:
            raise schema.SchemaError("%s.title: duplicate title" % path)
        titles.add(entry["title"].casefold())
        if not entry["aliases"] and not entry.get("name_patterns"):
            raise schema.SchemaError("%s: at least one alias or name pattern is required" % path)
        for j, alias in enumerate(entry["aliases"]):
            toks = tuple(tokens(alias))
            if not toks or len(toks) > MAX_TOKENS or len("".join(toks)) < 3:
                raise schema.SchemaError("%s.aliases[%d]: an alias needs 1-%d tokens and 3 letters or digits" % (path, j, MAX_TOKENS))
            if toks in aliases:
                raise schema.SchemaError("%s.aliases[%d]: duplicate alias" % (path, j))
            aliases.add(toks)
        for j, pattern in enumerate(entry.get("name_patterns", [])):
            toks = _pattern_tokens(pattern)
            literal = [t for t in toks if t != "*"]
            if not literal or len(toks) > MAX_TOKENS or len("".join(literal)) < 3:
                raise schema.SchemaError("%s.name_patterns[%d]: a pattern needs 3 literal letters or digits" % (path, j))
            if toks in patterns:
                raise schema.SchemaError("%s.name_patterns[%d]: duplicate pattern" % (path, j))
            patterns.add(toks)
    return Games(data["games"])


def load(path=DEFAULT_PATH):
    try:
        data = schema.read_json(path)
    except schema.SchemaError:
        raise schema.SchemaError(FILE_ERROR) from None
    return validate(data)


_DEFAULT = []


def default():
    """The committed config/games.json, loaded once."""
    if not _DEFAULT:
        _DEFAULT.append(load())
    return _DEFAULT[0]

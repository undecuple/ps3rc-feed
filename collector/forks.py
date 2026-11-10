"""Fork network: ancestry, inherited associations, comparisons (spec 03).

Three relationships are kept apart and never merged into one another:

- ancestry: is_fork, parent and network root exactly as the source gave
  them (never guessed); `ancestry_state` says whether that is resolved;
- inferred game association: the repository's own README evidence, or the
  nearest ancestor's association labelled `inherited_from_parent`, always
  inferred and unverified;
- toolkit: detected from the repository's own text, or inherited.

Every walk up a parent chain carries a visited set and a depth cap, so a
repository that names itself as parent, a loop, or a very deep chain all
terminate; a loop is recorded as `ancestry_state: inconsistent`.

Comparisons (ahead/behind against the parent) are stored as the source
reported them; unknown is null with `state: unavailable`, never 0.
"""

from . import text

MAX_ANCESTRY_DEPTH = 32

ANCESTRY_STATES = ("none", "resolved", "unresolved", "unknown", "inconsistent")
COMPARISON_STATES = ("complete", "partial", "unavailable", "not_comparable")


def empty_comparison():
    return {
        "base_repository_id": None,
        "base_ref": None,
        "head_ref": None,
        "base_sha": None,
        "head_sha": None,
        "ahead": None,
        "behind": None,
        "state": "unavailable",
        "collected_at": None,
    }


def unknown_game():
    return {
        "key": None,
        "title": None,
        "basis": None,
        "confidence": "unknown",
        "inferred": True,
        "verified": False,
        "inherited_from": None,
    }


def unknown_toolkit():
    return {"name": None, "confidence": "unknown", "basis": None, "inherited_from": None}


def ancestry_state(obs, repositories):
    """State of the recorded ancestry of one observation."""
    if obs["is_fork"] is None:
        return "unknown"
    if obs["is_fork"] is False:
        return "none"
    parent = obs["parent_repository_id"]
    if parent is None:
        return "unresolved"
    if _loops(obs, repositories):
        # Covers a repository naming itself as parent: the walk starts
        # with the repository already in the visited set.
        return "inconsistent"
    return "resolved"


def _loops(obs, repositories):
    """True when following parent ids from obs returns to a visited repository."""
    visited = {obs["repository_id"]}
    current = obs
    for _ in range(MAX_ANCESTRY_DEPTH):
        parent_id = current["parent_repository_id"]
        if parent_id is None:
            return False
        if parent_id in visited:
            return True
        visited.add(parent_id)
        current = repositories.get(str(parent_id))
        if current is None:
            return False
    return False


def ancestors(obs, repositories):
    """Tracked ancestors of obs, nearest first, stopping at a loop or the depth cap."""
    chain = []
    visited = {obs["repository_id"]}
    current = obs
    for _ in range(MAX_ANCESTRY_DEPTH):
        parent_id = current["parent_repository_id"]
        if parent_id is None or parent_id in visited:
            break
        visited.add(parent_id)
        parent = repositories.get(str(parent_id))
        if parent is None:
            break
        chain.append(parent)
        current = parent
    return chain


def own_evidence(obs, repositories):
    """(category, game, toolkit) from the repository's own text, ignoring text copied from its parent.

    Returns None for game when the README carries no plausible title of its
    own, and None for toolkit when nothing in the text names one.
    """
    parent = repositories.get(str(obs["parent_repository_id"])) if obs["parent_repository_id"] is not None else None
    readme = obs["readme_excerpt"]
    description = obs["description"]
    if parent is not None and parent is not obs:
        if readme is not None and readme == parent["readme_excerpt"]:
            readme = None
        if description is not None and description == parent["description"]:
            description = None
    category, game, toolkit = text.classify(obs["full_name"], description, readme)
    if category == "framework":
        own_game = unknown_game()
        own_category = "framework"
    elif game["key"] is not None:
        own_game = dict(game, inherited_from=None)
        own_category = "game"
    else:
        own_game = None
        own_category = None
    own_toolkit = None
    if toolkit["name"] is not None:
        own_toolkit = {"name": toolkit["name"], "confidence": toolkit["confidence"], "basis": "own_text", "inherited_from": None}
    return own_category, own_game, own_toolkit


def resolve_associations(repositories):
    """Recompute category, game and toolkit for every observation (post-pass).

    Own evidence wins; otherwise the nearest tracked ancestor with evidence
    is inherited and labelled. Ancestry itself is never changed here.
    """
    own = {}
    for rid, obs in repositories.items():
        own[rid] = own_evidence(obs, repositories)
    for rid, obs in repositories.items():
        obs["ancestry_state"] = ancestry_state(obs, repositories)
        category, game, toolkit = own[rid]
        if game is None or toolkit is None:
            chain = ancestors(obs, repositories) if obs["is_fork"] else []
            for ancestor in chain:
                a_category, a_game, a_toolkit = own[str(ancestor["repository_id"])]
                if game is None and a_game is not None:
                    if a_game["key"] is not None:
                        game = dict(a_game, basis="inherited_from_parent", inherited_from=ancestor["repository_id"])
                    else:
                        # A framework ancestor settles the category only; there is no game to inherit.
                        game = unknown_game()
                    if category is None:
                        category = a_category
                if toolkit is None and a_toolkit is not None:
                    toolkit = dict(a_toolkit, basis="inherited_from_parent", inherited_from=ancestor["repository_id"])
                if game is not None and toolkit is not None:
                    break
        obs["category"] = category or "unclassified"
        obs["game"] = game if game is not None else unknown_game()
        obs["toolkit"] = toolkit if toolkit is not None else unknown_toolkit()
        obs["game"]["inferred"] = True
        obs["game"]["verified"] = False


def normalise_comparison(comparison):
    """Comparison block from a run, with unknown filled as null/unavailable."""
    if comparison is None:
        return empty_comparison()
    result = empty_comparison()
    result.update(comparison)
    return result


def divergence_changed(previous, current):
    """True when two known snapshots disagree on ahead/behind."""
    if previous["state"] not in ("complete", "partial") or current["state"] not in ("complete", "partial"):
        return False
    return (previous["ahead"], previous["behind"]) != (current["ahead"], current["behind"])

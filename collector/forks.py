"""Fork network: ancestry, inherited associations, comparisons (spec 03).

Three relationships are kept apart and never merged into one another:

- ancestry: is_fork, parent and network root exactly as the source gave
  them (never guessed); `ancestry_state` says whether that is resolved;
- inferred game association: the repository's own README evidence, or the
  nearest resolved tracked ancestor's association labelled
  `inherited_from_parent`, always inferred and unverified;
- toolkit: detected from the repository's own text, or inherited.

Copied READMEs: at first sighting the collector records the fork's own
heading and the heading of its nearest tracked ancestor (or root). A fork
whose heading has not changed since and matched its ancestor's heading
then is still carrying inherited text, even after the ancestor retitles.
When the parent is not tracked but the fork's heading matches the tracked
root's, the association is `inherited_unverified_copy`, never own evidence.

Every walk up a parent chain carries a visited set and a depth cap, so a
repository that names itself as parent, a loop, or a very deep chain all
terminate; a loop is `inconsistent`, a chain longer than the cap is
`depth_exceeded`, and nothing is inherited through either.

Comparisons (ahead/behind against the parent) are stored as the source
reported them; unknown is null with `state: unavailable`, never 0.
"""

from . import text

MAX_ANCESTRY_DEPTH = 32
MAX_HISTORY = 20

ANCESTRY_STATES = ("none", "resolved", "unresolved", "unknown", "inconsistent", "depth_exceeded")
COMPARISON_STATES = ("complete", "partial", "unavailable", "not_comparable")
COMPARABLE = ("complete", "partial")
INHERITED_BASES = ("inherited_from_parent", "inherited_unverified_copy")


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
        "future_dated": False,
    }


def empty_comparison_status():
    return {"last_reported_state": None, "last_reported_at": None, "stale": False}


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


# --- ancestry -------------------------------------------------------------


def _walk(obs, repositories):
    """Follow parent ids from obs. Returns (chain of tracked ancestors, outcome).

    outcome is "end" (chain ended: parent null or untracked), "loop" (a
    repository was revisited) or "depth" (the cap was hit while still
    walking tracked repositories).
    """
    chain = []
    visited = {obs["repository_id"]}
    current = obs
    for _ in range(MAX_ANCESTRY_DEPTH):
        parent_id = current["parent_repository_id"]
        if parent_id is None:
            return chain, "end"
        if parent_id in visited:
            return chain, "loop"
        visited.add(parent_id)
        parent = repositories.get(str(parent_id))
        if parent is None:
            return chain, "end"
        chain.append(parent)
        current = parent
    if current["parent_repository_id"] is None:
        return chain, "end"
    return chain, "depth"


def ancestry_state(obs, repositories):
    """State of the recorded ancestry of one observation."""
    if obs["is_fork"] is None:
        return "unknown"
    if obs["is_fork"] is False:
        if obs["parent_repository_id"] is not None:
            return "inconsistent"
        return "none"
    if obs["parent_repository_id"] is None:
        return "unresolved"
    chain, outcome = _walk(obs, repositories)
    if outcome == "loop":
        return "inconsistent"
    if outcome == "depth":
        return "depth_exceeded"
    if chain:
        parent = chain[0]
        own_root = obs["network_root_repository_id"]
        if parent["is_fork"] is True:
            parent_root = parent["network_root_repository_id"]
            if own_root is not None and parent_root is not None and own_root != parent_root:
                return "inconsistent"
        elif parent["is_fork"] is False:
            if own_root is not None and own_root != parent["repository_id"]:
                return "inconsistent"
    return "resolved"


def ancestors(obs, repositories):
    """Tracked ancestors of obs, nearest first (empty unless the ancestry is resolved)."""
    if obs.get("ancestry_state") != "resolved":
        return []
    chain, outcome = _walk(obs, repositories)
    if outcome != "end":
        return []
    return chain


def network_key(obs, repositories):
    """Identifier of the fork network: the declared root, else the top of the tracked chain, else self."""
    if obs["network_root_repository_id"] is not None:
        return obs["network_root_repository_id"]
    chain, _ = _walk(obs, repositories)
    if chain:
        return chain[-1]["repository_id"]
    return obs["repository_id"]


# --- associations ---------------------------------------------------------


def own_heading(obs):
    return text.readme_title(obs["readme_excerpt"])


def record_first_sight(obs, repositories):
    """Fingerprint the README heading and the nearest ancestor's heading, once a README is known."""
    if obs["first_sight"] is not None or obs["readme_excerpt"] is None:
        return
    ancestor = None
    chain = ancestors(obs, repositories)
    if chain:
        ancestor = chain[0]
    elif obs["is_fork"] and obs["network_root_repository_id"] is not None:
        ancestor = repositories.get(str(obs["network_root_repository_id"]))
    obs["first_sight"] = {
        "heading": own_heading(obs),
        "ancestor_heading": own_heading(ancestor) if ancestor is not None else None,
        "ancestor_repository_id": ancestor["repository_id"] if ancestor is not None else None,
    }


def own_evidence(obs, repositories):
    """(category, game, toolkit, copied_from) from the repository's own text.

    game is None when the README carries no plausible title of its own,
    toolkit None when nothing in the text names one. copied_from is the
    tracked repository whose heading the fork's README copies, or None.
    """
    heading = own_heading(obs)
    description = obs["description"]
    copied_from = None
    if obs["is_fork"] and heading is not None:
        chain = ancestors(obs, repositories)
        parent = chain[0] if chain else None
        root = repositories.get(str(obs["network_root_repository_id"])) if obs["network_root_repository_id"] is not None else None
        first = obs["first_sight"]
        if parent is not None and heading == own_heading(parent):
            copied_from = parent
        elif first is not None and first["heading"] == heading and first["ancestor_heading"] == heading:
            copied_from = repositories.get(str(first["ancestor_repository_id"])) if first["ancestor_repository_id"] is not None else None
            if copied_from is None:
                copied_from = parent or root
        elif parent is None and root is not None and heading == own_heading(root):
            copied_from = root
        if copied_from is not None:
            heading = None
    for other in (repositories.get(str(obs["parent_repository_id"])) if obs["parent_repository_id"] is not None else None,):
        if other is not None and other is not obs and description is not None and description == other["description"]:
            description = None
    readme = obs["readme_excerpt"] if heading is not None else None
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
    return own_category, own_game, own_toolkit, copied_from


def resolve_associations(repositories):
    """Recompute ancestry state, category, game and toolkit for every observation (post-pass).

    Own evidence wins; otherwise the nearest resolved tracked ancestor with
    evidence is inherited and labelled. Ancestry itself is never changed.
    Returns {rid: "own" | "inherited"} describing where each game came from.
    """
    for obs in repositories.values():
        obs["ancestry_state"] = ancestry_state(obs, repositories)
    for obs in repositories.values():
        record_first_sight(obs, repositories)
    own = {rid: own_evidence(obs, repositories) for rid, obs in repositories.items()}
    origin = {}
    for rid, obs in repositories.items():
        category, game, toolkit, copied_from = own[rid]
        origin[rid] = "own"
        if game is None or toolkit is None:
            chain = ancestors(obs, repositories)
            for ancestor in chain:
                a_category, a_game, a_toolkit, _ = own[str(ancestor["repository_id"])]
                if game is None and a_game is not None:
                    if a_game["key"] is not None:
                        game = dict(a_game, basis="inherited_from_parent", inherited_from=ancestor["repository_id"])
                        origin[rid] = "inherited"
                    else:
                        # A framework ancestor settles the category only; there is no game to inherit.
                        game = unknown_game()
                    if category is None:
                        category = a_category
                if toolkit is None and a_toolkit is not None:
                    toolkit = dict(a_toolkit, basis="inherited_from_parent", inherited_from=ancestor["repository_id"])
                if game is not None and toolkit is not None:
                    break
            if game is None and copied_from is not None and not chain:
                # Parent untracked; the heading copies the tracked root's: an unverified copy.
                # Same title rules as the root's own classification.
                heading = text.game_title(copied_from["full_name"].split("/", 1)[1].lower(), own_heading(copied_from))
                key = text.slug(heading)
                if key is not None:
                    game = {
                        "key": key,
                        "title": heading,
                        "basis": "inherited_unverified_copy",
                        "confidence": "weak",
                        "inferred": True,
                        "verified": False,
                        "inherited_from": copied_from["repository_id"],
                    }
                    origin[rid] = "inherited"
                    if category is None:
                        category = "game"
        obs["category"] = category or "unclassified"
        obs["game"] = game if game is not None else unknown_game()
        obs["toolkit"] = toolkit if toolkit is not None else unknown_toolkit()
    return origin


# --- comparisons ----------------------------------------------------------


def normalise_comparison(comparison):
    """Comparison block from a run, with unknown filled as null/unavailable."""
    result = empty_comparison()
    if comparison is not None:
        result.update(comparison)
    return result


def same_base(previous, current):
    return (
        previous["base_repository_id"] == current["base_repository_id"]
        and previous["base_ref"] == current["base_ref"]
    )


def divergence_changed(previous, current):
    """True when two comparable snapshots against the same base disagree on ahead/behind."""
    if previous["state"] not in COMPARABLE or current["state"] not in COMPARABLE:
        return False
    if not same_base(previous, current):
        return False
    return (previous["ahead"], previous["behind"]) != (current["ahead"], current["behind"])


def push_history(history, entry):
    """Append to a bounded history, dropping the oldest entries."""
    history.append(entry)
    del history[:-MAX_HISTORY]

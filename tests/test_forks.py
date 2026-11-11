"""T28-T38: fork discovery, ancestry, inherited associations, comparisons (spec 03).

Fixture scenario: tests/fixtures/forks_two_roots. Where a case has no
fixture (unresolved ancestry, comparisons, cycles, shared SHAs) the runs
are built in memory with the same builders as the hardening tests.
"""

import copy
import time

from helpers import CollectorTestCase, canonical, load_run, run_runs
from test_hardening import T, failing, repo, run, sha

FRAME = "frame-org/ps3recomp-example"
ROOT90 = {"repository_id": 90, "full_name": FRAME, "forks_scan_complete": True, "found_repository_ids": []}
SEARCH = {"query": "ps3recomp in:name", "complete": True, "repository_ids": [90]}


def fork(rid, name, parent, root, **extra):
    extra.setdefault("readme", "")
    extra.setdefault("desc", None)
    return repo(rid=rid, name=name, is_fork=True, parent_repository_id=parent, network_root_repository_id=root, **extra)


def framework_root():
    return repo(rid=90, name=FRAME, desc="Fictional framework", readme="# Fictional\n", commits=[(1, T(1))])


def prefix_run():
    """A baseline run that knows only the framework root, so fixture run 1 is a discovery run."""
    return run(T(4), [framework_root()], searches=[SEARCH], roots=[ROOT90])


def fixture_runs(upto=3):
    return [load_run("forks_two_roots", i) for i in range(1, upto + 1)]


def comparison(ahead, behind, state="complete", at=T(5), base=100, base_ref="main", head_ref="main"):
    return {
        "base_repository_id": base,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "base_sha": sha(1),
        "head_sha": sha(2),
        "ahead": ahead,
        "behind": behind,
        "state": state,
        "collected_at": at,
    }


class T28TwoRootsOneEntry(CollectorTestCase):
    def test_fixture_run_1_is_a_baseline_with_one_entry_per_repository(self):
        from collector import catalog

        state, events = self.assertScenarioDeterministic("forks_two_roots", upto=1)
        self.assertEqual(events[0], [])
        entries = catalog.build_catalog(state)
        self.assertEqual([e["id"] for e in entries], [90, 100, 301, 302, 303])
        e301 = [e for e in entries if e["id"] == 301][0]
        self.assertEqual(e301["discovery_roots"], [90, 100])
        self.assertEqual(e301["discovery_queries"], ["ps3recomp in:name"])
        self.assertEqual(
            state["repositories"]["301"]["discovery_sources"],
            ["fork_network:100", "fork_network:90", "search:ps3recomp in:name"],
        )

    def test_repository_found_through_two_roots_is_discovered_once(self):
        state, events = self.assertDeterministic([prefix_run(), load_run("forks_two_roots", 1)])
        for_301 = [e for e in events[1] if e["repository_id"] == 301]
        self.assertEqual(sorted(e["type"] for e in for_301), ["fork_discovered", "repository_discovered"])
        disc = [e for e in for_301 if e["type"] == "repository_discovered"][0]
        self.assertEqual(disc["details"]["discovery_roots"], [90, 100])
        self.assertEqual(disc["event_id"], "repo-301:discovered")
        forkd = [e for e in for_301 if e["type"] == "fork_discovered"][0]
        self.assertEqual(forkd["event_id"], "repo-301:fork_discovered")
        self.assertEqual(forkd["summary"], "fork of game-org/skyfall-recomp first seen")
        self.assertEqual(forkd["details"]["parent"], "game-org/skyfall-recomp")
        self.assertEqual(forkd["details"]["root"], FRAME)
        self.assertIs(forkd["details"]["verified"], False)
        self.assertEqual(len(state["repositories"]), 5)
        # Re-applying the discovery run changes nothing.
        _, again = run_runs([prefix_run(), load_run("forks_two_roots", 1), load_run("forks_two_roots", 1)])
        self.assertEqual(again[2], [])


class T29ForkNetworkDiscovery(CollectorTestCase):
    def test_fork_without_upstream_name_or_readme_is_found_through_the_network(self):
        from collector import catalog

        run1 = copy.deepcopy(load_run("forks_two_roots", 1))
        run1["searches"][0]["repository_ids"].remove(303)
        state, events = self.assertDeterministic([prefix_run(), run1])
        obs = state["repositories"]["303"]
        self.assertEqual(obs["discovery_queries"], [])
        self.assertEqual(obs["discovery_roots"], [90])
        self.assertEqual(obs["discovery_sources"], ["fork_network:90"])
        self.assertIsNone(obs["description"])
        self.assertEqual(obs["readme_excerpt"], "")
        for_303 = [e for e in events[1] if e["repository_id"] == 303]
        self.assertEqual(sorted(e["type"] for e in for_303), ["fork_discovered", "repository_discovered"])
        disc = [e for e in for_303 if e["type"] == "repository_discovered"][0]
        self.assertEqual(disc["summary"], "first seen via fork_network:90")
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 303][0]
        self.assertEqual(entry["repo"], "fork-c/unrelated-name")
        self.assertEqual(entry["parent"], FRAME)
        # No README and no description: nothing of its own, so the toolkit
        # and category come from the parent and are labelled as such.
        self.assertEqual(entry["toolkit"], "ps3recomp")
        self.assertEqual(entry["toolkit_detection"]["basis"], "inherited_from_parent")
        self.assertEqual(entry["toolkit_detection"]["inherited_from"], 90)
        self.assertEqual(entry["category"], "framework")
        self.assertIsNone(entry["game"])

    def test_ids_named_but_not_fetched_are_pending_not_invented(self):
        from collector import catalog

        base = run(T(5), [framework_root()], searches=[SEARCH], roots=[dict(ROOT90, found_repository_ids=[777])])
        state, _ = self.assertDeterministic([base])
        self.assertEqual(state["collection"]["pending_repository_ids"], [777])
        self.assertEqual(catalog.collection_status(state)["pending_repository_ids"], [777])
        self.assertNotIn("777", state["repositories"])
        later = run(T(6), [framework_root(), fork(777, "late/one", 90, 90)], searches=[SEARCH], roots=[dict(ROOT90, found_repository_ids=[777])])
        state, events = self.assertDeterministic([base, later])
        self.assertEqual(state["collection"]["pending_repository_ids"], [])
        self.assertEqual(sorted(e["type"] for e in events[1]), ["fork_discovered", "repository_discovered"])


class T30NestedAncestry(CollectorTestCase):
    def test_nested_fork_links_parent_and_root_as_given(self):
        from collector import catalog

        state, _ = self.assertScenarioDeterministic("forks_two_roots", upto=1)
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 302][0]
        self.assertEqual(entry["parent_repository_id"], 301)
        self.assertEqual(entry["parent"], "fork-a/skyfall-recomp")
        self.assertEqual(entry["root_repository_id"], 90)
        self.assertEqual(entry["root"], FRAME)
        self.assertEqual(entry["ancestry"]["state"], "resolved")
        self.assertIs(entry["fork"], True)

    def test_unresolved_ancestry_stays_explicit_and_is_never_guessed(self):
        from collector import catalog

        unresolved = fork(500, "lost/parentless", None, None)
        unknown = fork(501, "lost/unknown", None, None)
        unknown["is_fork"] = None
        untracked_parent = fork(502, "lost/orphan", 999, 999)
        state, _ = self.assertDeterministic([run(T(5), [framework_root(), unresolved, unknown, untracked_parent], roots=[dict(ROOT90, found_repository_ids=[500, 501, 502])])])
        entries = {c["id"]: c for c in catalog.build_catalog(state)}
        self.assertIsNone(entries[500]["parent_repository_id"])
        self.assertIsNone(entries[500]["parent"])
        self.assertIsNone(entries[500]["root_repository_id"])
        self.assertEqual(entries[500]["ancestry"]["state"], "unresolved")
        self.assertEqual(entries[501]["ancestry"]["state"], "unknown")
        self.assertIsNone(entries[501]["fork"])
        self.assertEqual(entries[502]["parent_repository_id"], 999)
        self.assertIsNone(entries[502]["parent"])
        self.assertEqual(entries[502]["ancestry"]["state"], "resolved")
        self.assertEqual(catalog.collection_status(state)["unresolved_ancestry"], [500])
        # The discovery root (90) was not written into any ancestry field.
        for rid in (500, 501):
            self.assertNotEqual(entries[rid]["parent_repository_id"], 90)
            self.assertNotEqual(entries[rid]["root_repository_id"], 90)
        # Later, as a discovery: a fork without a parent is announced as a repository only;
        # fork_discovered waits until a parent is known (see test_forks_hardening G5).
        state, events = self.assertDeterministic([prefix_run(), run(T(5), [framework_root(), unresolved], roots=[dict(ROOT90, found_repository_ids=[500])])])
        self.assertEqual([e["type"] for e in events[1]], ["repository_discovered"])
        self.assertIs(state["repositories"]["500"]["fork_discovered_done"], False)


class T31IncompleteScan(CollectorTestCase):
    def test_partial_root_scan_is_recorded_and_deletes_nothing(self):
        from collector import catalog

        state, events = self.assertScenarioDeterministic("forks_two_roots", upto=2)
        self.assertEqual(events[1], [])
        root = state["collection"]["roots"]["100"]
        self.assertEqual(root["scan_state"], "partial")
        self.assertIs(root["forks_scan_complete"], False)
        self.assertEqual(root["found_repository_ids"], [301, 302])
        self.assertEqual(root["last_complete_at"], T(5))
        self.assertEqual(state["collection"]["incomplete_roots"], [100])
        status = catalog.collection_status(state)
        self.assertIs(status["incomplete"], True)
        self.assertEqual(status["partial_roots"], [100])
        self.assertEqual(status["root_scans"]["100"]["scan_state"], "partial")
        obs = state["repositories"]["302"]
        self.assertEqual(obs["collection_state"], "ok")
        self.assertIs(obs["stale"], False)
        self.assertEqual(obs["last_attempted_refresh_at"], T(5))
        self.assertIn(302, [c["id"] for c in catalog.build_catalog(state)])
        self.assertEqual(status["pending_repository_ids"], [])

    def test_resume_cursor_is_kept_until_a_complete_scan(self):
        partial = dict(ROOT90, forks_scan_complete=False, found_repository_ids=[], resume_cursor="page=2")
        complete = dict(ROOT90, forks_scan_complete=True, found_repository_ids=[])
        state, _ = self.assertDeterministic([run(T(5), [framework_root()], roots=[complete]), run(T(6), [framework_root()], roots=[partial])])
        root = state["collection"]["roots"]["90"]
        self.assertEqual(root["resume_cursor"], "page=2")
        self.assertEqual(root["scan_state"], "partial")
        self.assertEqual(root["last_complete_at"], T(5))
        # Not re-scanned in the next run: still partial, cursor still there.
        state, _ = self.assertDeterministic([run(T(5), [framework_root()], roots=[complete]), run(T(6), [framework_root()], roots=[partial]), run(T(7), [framework_root()])])
        self.assertEqual(state["collection"]["roots"]["90"]["resume_cursor"], "page=2")
        self.assertIs(state["collection"]["incomplete"], True)
        state, _ = self.assertDeterministic([run(T(5), [framework_root()], roots=[partial]), run(T(6), [framework_root()], roots=[complete])])
        root = state["collection"]["roots"]["90"]
        self.assertIsNone(root["resume_cursor"])
        self.assertEqual(root["scan_state"], "complete")
        self.assertEqual(root["last_complete_at"], T(6))
        self.assertIs(state["collection"]["incomplete"], False)


class T32LaterCompleteScan(CollectorTestCase):
    def test_new_nested_fork_is_discovered_without_a_history_flood(self):
        from collector import catalog

        state, events = self.assertScenarioDeterministic("forks_two_roots")
        self.assertEqual([(e["repository_id"], e["type"]) for e in events[2]], [(304, "fork_discovered"), (304, "repository_discovered")])
        obs = state["repositories"]["304"]
        self.assertEqual(sorted(obs["observed_commits"]), [sha(1), sha(0x3C)])
        self.assertEqual(obs["parent_repository_id"], 303)
        forkd = events[2][0]
        self.assertEqual(forkd["details"]["parent"], "fork-c/unrelated-name")
        self.assertEqual(forkd["details"]["discovery_roots"], [90])
        # 302 is back, 301 is unchanged: neither is re-discovered.
        ids = [e["event_id"] for e in state["events"]]
        self.assertNotIn("repo-302:discovered", ids)
        self.assertNotIn("repo-301:discovered", ids)
        self.assertEqual(state["collection"]["roots"]["100"]["scan_state"], "complete")
        self.assertIs(catalog.collection_status(state)["incomplete"], False)
        # A later run with one more commit on 304 reports exactly that commit.
        run4 = copy.deepcopy(load_run("forks_two_roots", 3))
        run4["run_at"] = T(7)
        r304 = [r for r in run4["repositories"] if r["repository_id"] == 304][0]
        r304["commits"].append({"sha": sha(0x3D), "committed_at": T(7), "message": "m"})
        state, events = self.assertDeterministic(fixture_runs() + [run4])
        self.assertEqual(self.types(events[3]), ["commits_observed"])
        self.assertEqual(events[3][0]["details"]["commit_shas"], [sha(0x3D)])
        self.assertEqual(events[3][0]["details"]["shared_with_network_count"], 0)


class T33InheritedAssociation(CollectorTestCase):
    def _child(self, readme="", desc=None):
        return fork(400, "child-org/skyfall-recomp", 100, 90, readme=readme, desc=desc, commits=[(0xA, T(3))])

    def _parent(self, readme="# Skyfall\n"):
        return repo(rid=100, name="game-org/skyfall-recomp", is_fork=True, parent_repository_id=90, network_root_repository_id=90, readme=readme, commits=[(0xA, T(3))])

    def test_child_without_evidence_inherits_labelled_and_unverified(self):
        from collector import catalog

        state, _ = self.assertDeterministic([run(T(5), [framework_root(), self._parent(), self._child()])])
        game = state["repositories"]["400"]["game"]
        self.assertEqual(game["key"], "skyfall")
        self.assertEqual(game["basis"], "inherited_from_parent")
        self.assertEqual(game["inherited_from"], 100)
        self.assertIs(game["verified"], False)
        self.assertIs(game["inferred"], True)
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 400][0]
        self.assertIs(entry["game_inherited"], True)
        self.assertEqual(entry["game"], "Skyfall")
        self.assertEqual(entry["game_association"]["basis"], "inherited_from_parent")
        self.assertEqual(entry["category"], "game")
        # README identical to the parent's is inherited text, not child evidence.
        state, _ = self.assertDeterministic([run(T(5), [framework_root(), self._parent(), self._child(readme="# Skyfall\n")])])
        self.assertEqual(state["repositories"]["400"]["game"]["basis"], "inherited_from_parent")
        # The parent of the parent is reached through the chain (302 -> 301 -> 100 in spirit).
        grandchild = fork(401, "grand/skyfall", 400, 90)
        state, _ = self.assertDeterministic([run(T(5), [framework_root(), self._parent(), self._child(), grandchild])])
        self.assertEqual(state["repositories"]["401"]["game"]["inherited_from"], 100)

    def test_child_evidence_overrides_without_changing_ancestry(self):
        state, events = self.assertDeterministic([
            run(T(5), [framework_root(), self._parent(), self._child()]),
            run(T(6), [framework_root(), self._parent(), self._child(readme="# Other Game\n")]),
        ])
        obs = state["repositories"]["400"]
        self.assertEqual(obs["game"]["key"], "other-game")
        self.assertEqual(obs["game"]["basis"], "readme_title")
        self.assertIsNone(obs["game"]["inherited_from"])
        self.assertIs(obs["game"]["verified"], False)
        self.assertEqual(obs["parent_repository_id"], 100)
        self.assertEqual(obs["network_root_repository_id"], 90)
        self.assertEqual(obs["ancestry_state"], "resolved")
        self.assertEqual(self.types(events[1]), ["game_association_changed"])
        change = events[1][0]
        self.assertEqual(change["details"]["previous_basis"], "inherited_from_parent")
        self.assertEqual(change["details"]["basis"], "readme_title")
        self.assertIs(change["details"]["verified"], False)
        self.assertEqual(change["event_id"], "repo-400:game_association:20261106000000")

    def test_parent_reclassification_propagates_to_the_inheriting_child(self):
        state, events = self.assertDeterministic([
            run(T(5), [framework_root(), self._parent(), self._child()]),
            run(T(6), [framework_root(), self._parent(readme="# Renamed Title\n")]),
        ])
        self.assertEqual(state["repositories"]["400"]["game"]["key"], "renamed-title")
        changes = [e for e in events[1] if e["type"] == "game_association_changed"]
        # The parent's own change, plus one aggregated event for everything that inherited it.
        self.assertEqual(sorted(e["event_id"] for e in changes), ["repo-100:game_association:20261106000000", "repo-100:game_association_inherited:20261106000000"])
        aggregate = [e for e in changes if e["details"].get("cause") == "inherited"][0]
        self.assertEqual(aggregate["details"]["affected_repository_ids"], [400])
        self.assertEqual(aggregate["details"]["affected_count"], 1)
        self.assertEqual(aggregate["summary"], "inherited game association changed for 1 repository (automated, unverified)")


class T34ThreeRelationships(CollectorTestCase):
    def test_ancestry_game_and_toolkit_are_separate_fields(self):
        from collector import catalog

        state, _ = self.assertScenarioDeterministic("forks_two_roots", upto=1)
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 301][0]
        self.assertEqual(set(entry["ancestry"]), {"is_fork", "parent_repository_id", "parent", "network_root_repository_id", "root", "state", "fork_discovered"})
        self.assertEqual(set(entry["game_association"]), {"key", "title", "basis", "inherited_from", "confidence", "matched_alias", "inferred", "verified"})
        self.assertEqual(set(entry["toolkit_detection"]), {"name", "basis", "inherited_from", "confidence"})
        self.assertEqual(entry["ancestry"]["parent_repository_id"], 100)
        # 301's own README heading differs from its parent's, so the game is its own evidence,
        # while the toolkit still comes from the chain and the ancestry is untouched.
        self.assertEqual(entry["game_association"]["basis"], "readme_title")
        self.assertEqual(entry["toolkit_detection"]["basis"], "inherited_from_parent")
        self.assertEqual(entry["toolkit_detection"]["inherited_from"], 90)
        for key in ("game_association", "toolkit_detection"):
            self.assertNotIn("parent_repository_id", entry[key])


class T35ComparisonUnavailable(CollectorTestCase):
    def test_unavailable_comparison_is_null_and_flagged_never_zero(self):
        from collector import catalog

        state, _ = self.assertScenarioDeterministic("forks_two_roots", upto=1)
        for entry in catalog.build_catalog(state):
            self.assertIsNone(entry["ahead"])
            self.assertIsNone(entry["behind"])
            self.assertEqual(entry["comparison_state"], "unavailable")
            self.assertEqual(entry["comparison"]["state"], "unavailable")
            self.assertIsNone(entry["comparison"]["collected_at"])

    def test_known_snapshots_are_stored_and_changes_announced_once(self):
        from collector import catalog, schema

        child = lambda cmp, at: run(at, [framework_root(), repo(rid=100, name="game-org/skyfall-recomp", is_fork=True, parent_repository_id=90, network_root_repository_id=90, commits=[(0xA, T(3))], comparison=cmp)])
        runs = [
            child(None, T(5)),
            child(comparison(5, 2, at=T(6)), T(6)),
            child(comparison(5, 2, at=T(7)), T(7)),
            child(comparison(6, 2, at=T(8)), T(8)),
            child({"state": "unavailable"}, T(9)),
            child(comparison(6, 2, at=T(10)), T(10)),
        ]
        state, events = self.assertDeterministic(runs)
        self.assertEqual(events[1], [])  # first known snapshot is a baseline for divergence
        self.assertEqual(events[2], [])
        self.assertEqual(self.types(events[3]), ["fork_divergence_changed"])
        change = events[3][0]
        self.assertEqual(change["summary"], "ahead 6 / behind 2 against the recorded base ref")
        self.assertEqual((change["details"]["previous_ahead"], change["details"]["previous_behind"]), (5, 2))
        self.assertEqual(change["event_id"], "repo-100:divergence:20261108000000")
        self.assertEqual(change["source_at"], T(8))
        self.assertEqual(events[4], [])  # became unavailable: no event, values null
        self.assertEqual(events[5], [])  # known again, same numbers: no event
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 100][0]
        self.assertEqual((entry["ahead"], entry["behind"], entry["comparison_state"]), (6, 2, "complete"))
        mid, _ = run_runs(runs[:5])
        mid_entry = [c for c in catalog.build_catalog(mid) if c["id"] == 100][0]
        # The last comparable snapshot stays visible, marked stale, while the source reports unavailable.
        self.assertEqual((mid_entry["ahead"], mid_entry["behind"], mid_entry["comparison_state"]), (6, 2, "complete"))
        self.assertIs(mid_entry["comparison_stale"], True)
        self.assertIs(entry["comparison_stale"], False)
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(child({"state": "unavailable", "ahead": 0, "behind": 0}, T(11)))
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(child({"state": "complete"}, T(11)))
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(child(comparison(-1, 0), T(11)))
        # Partial comparison: numbers kept, flagged partial in the summary.
        state, events = self.assertDeterministic([child(comparison(5, 2, at=T(6)), T(6)), child(comparison(7, 2, state="partial", at=T(7)), T(7))])
        self.assertIn("(partial comparison)", events[1][0]["summary"])

    def test_hostile_ref_names_are_dropped_at_load_and_valid_refs_escaped(self):
        from collector import catalog

        # Refs outside the git character set never reach the store (test_forks_hardening G8);
        # refs inside it are still passed through the renderer escape.
        cmp = comparison(1, 0, base_ref="feature/x+y", head_ref="release-1.0")
        runs = [
            run(T(5), [framework_root(), repo(rid=100, name="game-org/skyfall-recomp", is_fork=True, parent_repository_id=90, network_root_repository_id=90, comparison=cmp)]),
            run(T(6), [framework_root(), repo(rid=100, name="game-org/skyfall-recomp", is_fork=True, parent_repository_id=90, network_root_repository_id=90, comparison=dict(cmp, ahead=2, collected_at=T(6)))]),
        ]
        state, events = self.assertDeterministic(runs)
        details = events[1][0]["details"]
        self.assertEqual(details["base_ref"], "feature/x+y")
        self.assertEqual(details["head_ref"], "release-1.0")
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 100][0]
        self.assertEqual(entry["comparison"]["base_ref"], "feature/x+y")
        hostile = comparison(1, 0, base_ref="<script>alert(1)</script>", head_ref="@everyone‮")
        state, events = self.assertDeterministic([run(T(5), [framework_root(), repo(rid=100, name="game-org/skyfall-recomp", is_fork=True, parent_repository_id=90, network_root_repository_id=90, comparison=hostile)])])
        self.assertEqual(state["repositories"]["100"]["comparison"]["state"], "unavailable")
        self.assertIsNone(state["repositories"]["100"]["comparison"]["base_ref"])
        self.assertNotIn("<script", canonical(state))


class T36T37DiscoveryRecords(CollectorTestCase):
    def test_first_seen_is_tracker_time_and_queries_are_retained(self):
        from collector import catalog

        state, _ = self.assertScenarioDeterministic("forks_two_roots", upto=1)
        for entry in catalog.build_catalog(state):
            self.assertEqual(entry["first_seen_at"], T(5))
            self.assertNotEqual(entry["first_seen_at"], entry["last_commit_at"])
            self.assertEqual(entry["discovery_queries"], ["ps3recomp in:name"])
        state, _ = self.assertScenarioDeterministic("forks_two_roots")
        self.assertEqual(state["repositories"]["304"]["first_seen_at"], T(6))

    def test_search_results_are_merged_by_repository_id(self):
        from collector import catalog

        searches = [
            {"query": "ps3recomp in:name", "complete": True, "repository_ids": [90, 301]},
            {"query": "ps3 recomp in:readme", "complete": True, "repository_ids": [301]},
        ]
        r301 = fork(301, "fork-a/skyfall-recomp", 90, 90, readme="# Fictional\n")
        state, _ = self.assertDeterministic([run(T(5), [framework_root(), r301], searches=searches, roots=[dict(ROOT90, found_repository_ids=[301])])])
        entries = catalog.build_catalog(state)
        self.assertEqual([e["id"] for e in entries], [90, 301])
        e301 = entries[1]
        self.assertEqual(e301["discovery_queries"], ["ps3 recomp in:readme", "ps3recomp in:name"])
        self.assertEqual(e301["discovery_roots"], [90])
        self.assertEqual(len(state["repositories"]), 2)


class T38SharedShasPerRepository(CollectorTestCase):
    def test_a_forks_unique_commit_is_not_suppressed_by_a_shared_sha(self):
        run4 = copy.deepcopy(load_run("forks_two_roots", 3))
        run4["run_at"] = T(7)
        by_id = {r["repository_id"]: r for r in run4["repositories"]}
        shared = {"sha": sha(0x77), "committed_at": T(7), "message": "shared"}
        by_id[303]["commits"].append(dict(shared))
        by_id[304]["commits"].append(dict(shared))
        by_id[304]["commits"].append({"sha": sha(0x78), "committed_at": T(7), "message": "unique"})
        state, events = self.assertDeterministic(fixture_runs() + [run4])
        by_repo = {e["repository_id"]: e for e in events[3]}
        self.assertEqual(sorted(by_repo), [303, 304])
        self.assertEqual(by_repo[303]["type"], "commits_observed")
        self.assertEqual(by_repo[303]["details"]["commit_shas"], [sha(0x77)])
        self.assertEqual(by_repo[304]["type"], "commits_observed")
        self.assertEqual(by_repo[304]["details"]["commit_shas"], [sha(0x77), sha(0x78)])
        # 0x77 arrived in both repositories in the same run: counted, never used to relabel.
        self.assertEqual(by_repo[304]["details"]["shared_shas"], [])
        self.assertEqual(by_repo[304]["details"]["shared_with_network_count"], 0)
        self.assertEqual(by_repo[304]["details"]["shared_in_run_count"], 1)
        self.assertEqual(by_repo[303]["details"]["shared_in_run_count"], 1)
        self.assertIn(sha(0x77), state["repositories"]["303"]["observed_commits"])
        self.assertIn(sha(0x77), state["repositories"]["304"]["observed_commits"])
        self.assertIn(sha(0x78), state["repositories"]["304"]["observed_commits"])
        self.assertNotIn(sha(0x78), state["repositories"]["303"]["observed_commits"])

    def test_fork_syncing_only_upstream_commits_is_inherited_activity(self):
        run4 = copy.deepcopy(load_run("forks_two_roots", 3))
        run4["run_at"] = T(7)
        by_id = {r["repository_id"]: r for r in run4["repositories"]}
        # 304 pulls commit 0a, which 100/301/302 already carry.
        by_id[304]["commits"].append({"sha": sha(0xA), "committed_at": T(3), "message": "m"})
        state, events = self.assertDeterministic(fixture_runs() + [run4])
        self.assertEqual([(e["repository_id"], e["type"]) for e in events[3]], [(304, "fork_activity_observed")])
        e = events[3][0]
        self.assertEqual(e["details"]["commit_shas"], [sha(0xA)])
        self.assertEqual(e["details"]["shared_with_network_count"], 1)
        self.assertIn("already observed elsewhere in the network", e["summary"])
        self.assertEqual(e["event_id"], "repo-304:commits:" + e["event_id"].split(":")[-1])
        # Still recorded per repository.
        self.assertIn(sha(0xA), state["repositories"]["304"]["observed_commits"])


class HostileAncestryTests(CollectorTestCase):
    def test_self_parent_and_loops_are_inconsistent_and_terminate(self):
        from collector import catalog

        selfie = fork(600, "loop/self", 600, 600, readme="")
        a = fork(601, "loop/a", 602, 90, readme="")
        b = fork(602, "loop/b", 603, 90, readme="")
        c = fork(603, "loop/c", 601, 90, readme="")
        started = time.monotonic()
        state, events = self.assertDeterministic([prefix_run(), run(T(5), [framework_root(), selfie, a, b, c], roots=[dict(ROOT90, found_repository_ids=[600, 601, 602, 603])])])
        self.assertLess(time.monotonic() - started, 2.0)
        for rid in ("600", "601", "602", "603"):
            obs = state["repositories"][rid]
            self.assertEqual(obs["ancestry_state"], "inconsistent", rid)
            self.assertIsNone(obs["game"]["key"])
            self.assertIsNone(obs["game"]["inherited_from"])
            # Ancestry is recorded as given, not repaired.
        self.assertEqual(state["repositories"]["600"]["parent_repository_id"], 600)
        self.assertEqual(state["repositories"]["601"]["parent_repository_id"], 602)
        self.assertEqual(catalog.collection_status(state)["unresolved_ancestry"], [600, 601, 602, 603])
        forkd = [e for e in events[1] if e["type"] == "fork_discovered" and e["repository_id"] == 600][0]
        self.assertEqual(forkd["summary"], "fork first seen; parent not resolved (inconsistent)")

    def test_deep_chain_terminates_and_inheritance_is_bounded(self):
        from collector import forks

        depth = 200
        repos = [repo(rid=100, name="game-org/skyfall-recomp", is_fork=True, parent_repository_id=90, network_root_repository_id=90, readme="# Skyfall\n")]
        parent = 100
        for i in range(depth):
            rid = 1000 + i
            repos.append(fork(rid, "deep/f%d" % i, parent, 90))
            parent = rid
        started = time.monotonic()
        state, _ = self.assertDeterministic([run(T(5), [framework_root()] + repos)])
        self.assertLess(time.monotonic() - started, 5.0)
        near = state["repositories"][str(1000 + forks.MAX_ANCESTRY_DEPTH - 2)]
        self.assertEqual(near["game"]["key"], "skyfall")
        self.assertEqual(near["game"]["inherited_from"], 100)
        far = state["repositories"][str(1000 + depth - 1)]
        self.assertIsNone(far["game"]["key"])
        self.assertEqual(far["ancestry_state"], "depth_exceeded")
        self.assertEqual(far["parent_repository_id"], 1000 + depth - 2)


class EventTypeBudget(CollectorTestCase):
    def test_only_spec_02_and_spec_03_event_types_exist(self):
        from collector import schema

        self.assertEqual(
            set(schema.EVENT_TYPES),
            {
                "repository_discovered",
                "commits_observed",
                "release_published",
                "repository_archived",
                "collection_stale",
                "collection_recovered",
                "game_association_changed",
                "fork_discovered",
                "fork_activity_observed",
                "fork_divergence_changed",
            },
        )

    def test_every_fixture_event_passes_the_strict_loader(self):
        from collector import schema

        state, _ = self.assertScenarioDeterministic("forks_two_roots")
        schema.load_events(copy.deepcopy(state["events"]))
        schema.validate_state(copy.deepcopy(state))

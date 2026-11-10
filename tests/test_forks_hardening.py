"""Regression tests for the step 2 review findings (G1-G9).

All runs are built in memory. Numbers in the class names refer to the
findings table in .local/STATUS.md.
"""

import copy
import json
import os
import random
import tempfile
import time

from helpers import CollectorTestCase, canonical, run_runs
from test_hardening import T, failing, repo, run, sha

FRAME = "frame-org/ps3recomp-example"


def fork(rid, name, parent, root, **extra):
    extra.setdefault("readme", "")
    extra.setdefault("desc", None)
    return repo(rid=rid, name=name, is_fork=True, parent_repository_id=parent, network_root_repository_id=root, **extra)


def frame(commits=((1, T(1)),), readme="# Fictional\n"):
    return repo(rid=90, name=FRAME, desc="Fictional framework", readme=readme, commits=list(commits))


def skyfall(commits=((0xA, T(3)),), readme="# Skyfall\n", **extra):
    return repo(rid=100, name="game-org/skyfall-recomp", is_fork=True, parent_repository_id=90, network_root_repository_id=90, readme=readme, commits=list(commits), **extra)


def failing_fork(rid, name):
    """A fork first seen while rate limited: no README, no ancestry yet."""
    return repo(rid=rid, name=name, fetch="rate_limited", readme=None, desc=None, is_fork=None, commits=None, releases=None, stars=None, archived=None)


def cmp(state, ahead=None, behind=None, at=None, base=90, base_ref="main", head_ref="main"):
    return {"state": state, "ahead": ahead, "behind": behind, "collected_at": at, "base_repository_id": base, "base_ref": base_ref, "head_ref": head_ref}


def types_by_repo(events):
    return sorted((e["repository_id"], e["type"]) for e in events)


class G1SharedShaLabellingIsOrderIndependent(CollectorTestCase):
    def _scenario(self, copier_id, copier_is_new=False, same_network=False):
        if same_network:
            copier = lambda commits: fork(copier_id, "copy/net", 90, 90, commits=commits)
        else:
            copier = lambda commits: repo(rid=copier_id, name="other/standalone", readme="", desc=None, commits=commits)
        run1 = [frame(), skyfall([(1, T(1)), (2, T(2))])]
        if not copier_is_new:
            run1.append(copier([(7, T(1))]))
        run2 = [frame(), skyfall([(1, T(1)), (2, T(2)), (3, T(6))]), copier(([(7, T(1))] if not copier_is_new else []) + [(3, T(6))])]
        return [run(T(5), run1), run(T(6), run2)]

    def test_same_run_copy_by_an_unrelated_repository_never_relabels(self):
        for copier_id in (50, 500):
            _, events = self.assertDeterministic(self._scenario(copier_id))
            for_100 = [e for e in events[1] if e["repository_id"] == 100][0]
            self.assertEqual(for_100["type"], "commits_observed", copier_id)
            self.assertEqual(for_100["details"]["shared_with_network_count"], 0)
            self.assertEqual(for_100["details"]["shared_in_run_count"], 0)
            self.assertEqual(for_100["summary"], "1 previously unseen default-branch commit")

    def test_repository_first_seen_this_run_never_relabels(self):
        _, events = self.assertDeterministic(self._scenario(60, copier_is_new=True))
        for_100 = [e for e in events[1] if e["repository_id"] == 100][0]
        self.assertEqual(for_100["type"], "commits_observed")
        self.assertEqual(for_100["details"]["shared_with_network_count"], 0)

    def test_same_run_overlap_inside_the_network_is_counted_not_relabelled(self):
        for upstream_id in (90, 900):
            def upstream(commits):
                return repo(rid=upstream_id, name="frame-org/ps3recomp-example", desc="Fictional framework", readme="# Fictional\n", commits=commits)

            def child(commits):
                return repo(rid=100, name="game-org/skyfall-recomp", is_fork=True, parent_repository_id=upstream_id, network_root_repository_id=upstream_id, readme="# Skyfall\n", commits=commits)

            runs = [run(T(5), [upstream([(1, T(1))]), child([(1, T(1))])]), run(T(6), [upstream([(1, T(1)), (3, T(6))]), child([(1, T(1)), (3, T(6))])])]
            _, events = self.assertDeterministic(runs)
            self.assertEqual([e["type"] for e in events[1]], ["commits_observed", "commits_observed"], upstream_id)
            for e in events[1]:
                self.assertEqual(e["details"]["shared_with_network_count"], 0)
                self.assertEqual(e["details"]["shared_in_run_count"], 1)

    def test_pre_run_shared_sha_in_the_network_is_inherited_activity(self):
        runs = [
            run(T(5), [frame([(1, T(1))]), skyfall([(1, T(1))])]),
            run(T(6), [frame([(1, T(1)), (3, T(6))]), skyfall([(1, T(1))])]),
            run(T(7), [frame([(1, T(1)), (3, T(6))]), skyfall([(1, T(1)), (3, T(6))])]),
        ]
        _, events = self.assertDeterministic(runs)
        self.assertEqual(types_by_repo(events[1]), [(90, "commits_observed")])
        self.assertEqual(types_by_repo(events[2]), [(100, "fork_activity_observed")])
        e = events[2][0]
        self.assertEqual(e["details"]["shared_shas"], [sha(3)])
        self.assertEqual(e["summary"], "1 commit observed on the tracked branch, all already observed elsewhere in the network")
        # A pre-run-shared SHA beside a unique one: still commits_observed, the shared one listed.
        runs = [
            run(T(5), [frame([(1, T(1)), (3, T(1))]), skyfall([(1, T(1))])]),
            run(T(6), [frame([(1, T(1)), (3, T(1))]), skyfall([(1, T(1)), (3, T(1)), (9, T(6))])]),
        ]
        _, events = self.assertDeterministic(runs)
        self.assertEqual(types_by_repo(events[1]), [(100, "commits_observed")])
        self.assertEqual(events[1][0]["details"]["shared_shas"], [sha(3)])
        self.assertEqual(events[1][0]["details"]["shared_with_network_count"], 1)
        self.assertEqual(events[1][0]["details"]["commit_shas"], [sha(3), sha(9)])
        # Outside the network the same SHA is not "shared".
        runs = [
            run(T(5), [frame([(1, T(1))]), skyfall([(1, T(1))]), repo(rid=50, name="other/standalone", readme="", desc=None, commits=[(3, T(1))])]),
            run(T(6), [frame([(1, T(1))]), skyfall([(1, T(1)), (3, T(6))]), repo(rid=50, name="other/standalone", readme="", desc=None, commits=[(3, T(1))])]),
        ]
        _, events = self.assertDeterministic(runs)
        self.assertEqual(types_by_repo(events[1]), [(100, "commits_observed")])

    def test_permuted_run_files_give_identical_ids_and_types(self):
        base = self._scenario(500, same_network=True)
        rng = random.Random(7)
        outcomes = []
        for _ in range(3):
            runs = copy.deepcopy(base)
            for r in runs:
                rng.shuffle(r["repositories"])
            _, events = run_runs(runs)
            outcomes.append([(e["event_id"], e["type"]) for r in events for e in r])
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[1], outcomes[2])


class G2IndexScales(CollectorTestCase):
    def test_five_thousand_forks_finish_in_a_few_seconds(self):
        n = 5000

        def big(day, extra):
            repos = [frame([(1, T(1))])]
            for i in range(n):
                rid = 1000 + i
                repos.append(fork(rid, "fork%d/f" % i, 90, 90, commits=[(1, T(1))] + [(10**6 + rid * 10 + k, T(day)) for k in range(extra)]))
            roots = [{"repository_id": 90, "full_name": FRAME, "forks_scan_complete": True, "found_repository_ids": [1000 + i for i in range(n)]}]
            return run(T(day), repos, roots=roots)

        started = time.monotonic()
        state, events = run_runs([big(5, 1), big(6, 2)])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 20.0, "two runs with %d forks took %.1fs" % (n, elapsed))
        self.assertEqual(len(events[1]), n)
        self.assertTrue(all(e["type"] == "commits_observed" for e in events[1]))
        self.assertEqual(len(state["repositories"]), n + 1)


class G3CopiedReadmeFingerprint(CollectorTestCase):
    def _child(self, readme="# Skyfall\n"):
        return fork(400, "child-org/skyfall-recomp", 100, 90, readme=readme, commits=[(0xA, T(3))])

    def test_copied_heading_stays_inherited_after_the_parent_retitles(self):
        runs = [
            run(T(5), [frame(), skyfall(), self._child()]),
            run(T(6), [frame(), skyfall(readme="# Free Bird\n"), self._child()]),
            run(T(7), [frame(), skyfall(readme="# Free Bird\n"), self._child(readme="# My Own Title\n")]),
        ]
        state, events = self.assertDeterministic(runs)
        mid, _ = run_runs(runs[:2])
        child = mid["repositories"]["400"]
        self.assertEqual(child["first_sight"], {"heading": "Skyfall", "ancestor_heading": "Skyfall", "ancestor_repository_id": 100})
        self.assertEqual(child["game"]["key"], "free-bird")
        self.assertEqual(child["game"]["basis"], "inherited_from_parent")
        self.assertEqual(child["game"]["inherited_from"], 100)
        self.assertIs(child["game"]["verified"], False)
        child = state["repositories"]["400"]
        self.assertEqual(child["game"]["key"], "my-own-title")
        self.assertEqual(child["game"]["basis"], "readme_title")
        self.assertIsNone(child["game"]["inherited_from"])
        self.assertEqual(child["parent_repository_id"], 100)

    def test_untracked_parent_with_root_heading_is_an_unverified_copy(self):
        from collector import catalog

        grandchild = fork(302, "fork-b/skyfall-recomp", 301, 100, readme="# Skyfall\n")
        state, _ = self.assertDeterministic([run(T(5), [frame(), skyfall(), grandchild])])
        game = state["repositories"]["302"]["game"]
        self.assertEqual(game["key"], "skyfall")
        self.assertEqual(game["basis"], "inherited_unverified_copy")
        self.assertEqual(game["inherited_from"], 100)
        self.assertIs(game["verified"], False)
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 302][0]
        self.assertIs(entry["game_inherited"], True)
        self.assertEqual(entry["game_basis"], "inherited_unverified_copy")
        # A different heading of its own is own evidence.
        other = fork(302, "fork-b/skyfall-recomp", 301, 100, readme="# Something Else\n")
        state, _ = self.assertDeterministic([run(T(5), [frame(), skyfall(), other])])
        self.assertEqual(state["repositories"]["302"]["game"]["basis"], "readme_title")


class G4InheritedChangesAreAggregated(CollectorTestCase):
    def test_thousand_children_flipping_three_times_give_six_events(self):
        n = 1000

        def big(day, title):
            repos = [frame(), skyfall(readme=title)]
            for i in range(n):
                repos.append(fork(2000 + i, "child%d/c" % i, 100, 90, readme="# Skyfall\n", commits=[(0xA, T(3))]))
            return run(T(day), repos)

        runs = [big(5, "# Skyfall\n"), big(6, "# Free Bird\n"), big(7, "# Skyfall\n"), big(8, "# Free Bird\n")]
        state, events = run_runs(runs)
        self.assertEqual(events[0], [])
        for day, run_events in zip((6, 7, 8), events[1:]):
            self.assertEqual(len(run_events), 2, [e["event_id"] for e in run_events][:5])
            self.assertEqual([e["repository_id"] for e in run_events], [100, 100])
            ids = sorted(e["event_id"] for e in run_events)
            self.assertEqual(ids, ["repo-100:game_association:202611%02d000000" % day, "repo-100:game_association_inherited:202611%02d000000" % day])
            agg = [e for e in run_events if e["details"].get("cause") == "inherited"][0]
            self.assertEqual(agg["details"]["affected_count"], n)
            self.assertEqual(len(agg["details"]["affected_repository_ids"]), 100)
            self.assertEqual(agg["details"]["affected_repository_ids"][:3], [2000, 2001, 2002])
            self.assertIs(agg["details"]["affected_truncated"], True)
            self.assertIs(agg["details"]["verified"], False)
        self.assertEqual(len(state["events"]), 6)
        self.assertEqual(state["repositories"]["2500"]["game"]["key"], "free-bird")

    def test_direct_change_of_a_child_is_its_own_event(self):
        child = lambda readme: fork(400, "child-org/skyfall-recomp", 100, 90, readme=readme, commits=[(0xA, T(3))])
        _, events = self.assertDeterministic([run(T(5), [frame(), skyfall(), child("# Skyfall\n")]), run(T(6), [frame(), skyfall(), child("# Mine\n")])])
        self.assertEqual([(e["repository_id"], e["event_id"]) for e in events[1]], [(400, "repo-400:game_association:20261106000000")])
        self.assertNotIn("cause", events[1][0]["details"])


class G5ForkDiscoveredOnceWhenKnown(CollectorTestCase):
    def test_fork_relationship_known_later_is_announced_once(self):
        from collector import catalog

        runs = [
            run(T(5), [frame(), repo(rid=200, name="late/becomes-fork", readme="", desc=None), failing_fork(300, "slow/first-failing")]),
            run(T(6), [frame(), fork(200, "late/becomes-fork", 90, 90), fork(300, "slow/first-failing", 90, 90)]),
            run(T(7), [frame(), fork(200, "late/becomes-fork", 90, 90), fork(300, "slow/first-failing", 90, 90)]),
        ]
        state, events = self.assertDeterministic(runs)
        self.assertEqual(types_by_repo(events[1]), [(200, "fork_discovered"), (300, "fork_discovered")])
        self.assertEqual(events[2], [])
        for rid in ("200", "300"):
            self.assertIs(state["repositories"][rid]["fork_discovered_done"], True)
        for e in events[1]:
            self.assertEqual(e["event_id"], "repo-%d:fork_discovered" % e["repository_id"])
            self.assertIsNone(e["details"]["related_event_id"])
        self.assertIs(catalog.build_catalog(state)[1]["ancestry"]["fork_discovered"], True)
        # Baseline forks are recorded silently.
        self.assertIs(state["repositories"]["90"]["fork_discovered_done"], False)
        base, _ = run_runs([run(T(5), [frame(), skyfall()])])
        self.assertIs(base["repositories"]["100"]["fork_discovered_done"], True)

    def test_new_fork_after_baseline_links_both_events(self):
        state, events = self.assertDeterministic([run(T(5), [frame()]), run(T(6), [frame(), skyfall()])])
        by_type = {e["type"]: e for e in events[1]}
        self.assertEqual(set(by_type), {"repository_discovered", "fork_discovered"})
        self.assertEqual(by_type["repository_discovered"]["details"]["related_event_id"], "repo-100:fork_discovered")
        self.assertEqual(by_type["fork_discovered"]["details"]["related_event_id"], "repo-100:discovered")
        self.assertEqual(by_type["fork_discovered"]["summary"], "fork of %s first seen" % FRAME)

    def test_new_failing_repository_is_announced_as_fork_only_when_resolved(self):
        runs = [
            run(T(5), [frame()]),
            run(T(6), [frame(), failing_fork(400, "quiet/one")]),
            run(T(7), [frame(), fork(400, "quiet/one", 90, 90)]),
            run(T(8), [frame(), fork(400, "quiet/one", 90, 90)]),
        ]
        _, events = self.assertDeterministic(runs)
        self.assertEqual(types_by_repo(events[1]), [(400, "repository_discovered")])
        self.assertIsNone(events[1][0]["details"]["related_event_id"])
        self.assertEqual(types_by_repo(events[2]), [(400, "fork_discovered")])
        self.assertEqual(events[3], [])
        # An unresolved fork (parent null) waits for its parent.
        _, events = self.assertDeterministic([run(T(5), [frame()]), run(T(6), [frame(), fork(500, "lost/parentless", None, None)]), run(T(7), [frame(), fork(500, "lost/parentless", 90, 90)])])
        self.assertEqual(types_by_repo(events[1]), [(500, "repository_discovered")])
        self.assertEqual(types_by_repo(events[2]), [(500, "fork_discovered")])


class G6AncestryHistory(CollectorTestCase):
    def test_parent_change_is_recorded_and_bounded(self):
        from collector import catalog, forks

        other = repo(rid=91, name="other-org/other", readme="# Other Game\n", commits=[(5, T(1))])
        runs = [run(T(5), [frame(), other, skyfall()]), run(T(6), [frame(), other, repo(rid=100, name="game-org/skyfall-recomp", is_fork=True, parent_repository_id=91, network_root_repository_id=91, readme="# Skyfall\n", commits=[(0xA, T(3))])])]
        state, events = self.assertDeterministic(runs)
        obs = state["repositories"]["100"]
        self.assertEqual(obs["parent_repository_id"], 91)
        self.assertEqual(obs["ancestry_history"], [{"is_fork": True, "parent_repository_id": 90, "network_root_repository_id": 90, "until": T(6)}])
        self.assertEqual(catalog.build_catalog(state)[2]["ancestry_history"][0]["parent_repository_id"], 90)
        self.assertEqual([e["type"] for e in events[1]], [])
        # Bounded: 25 changes keep the newest 20.
        parents = [repo(rid=200 + i, name="p%d/p" % i, readme="", desc=None) for i in range(26)]
        runs = []
        for i in range(26):
            runs.append(run(T(1 + i // 24, i % 24), parents + [repo(rid=100, name="game-org/skyfall-recomp", is_fork=True, parent_repository_id=200 + i, network_root_repository_id=200 + i, readme="# Skyfall\n", commits=[(0xA, T(1))])]))
        state, _ = run_runs(runs)
        history = state["repositories"]["100"]["ancestry_history"]
        self.assertEqual(len(history), forks.MAX_HISTORY)
        self.assertEqual(history[0]["parent_repository_id"], 200 + 25 - forks.MAX_HISTORY)
        self.assertEqual(history[-1]["parent_repository_id"], 224)


class G7Comparisons(CollectorTestCase):
    def _go(self, comparisons, is_fork=True):
        runs = []
        for day, c in enumerate(comparisons, start=1):
            if is_fork:
                r = skyfall(comparison=c)
            else:
                r = repo(rid=100, name="game-org/skyfall-recomp", readme="# Skyfall\n", commits=[(0xA, T(1))], comparison=c)
            runs.append(run(T(day), [frame(), r]))
        state, events = self.assertDeterministic(runs)
        divergence = [[e for e in run_events if e["type"] == "fork_divergence_changed"] for run_events in events]
        return state, divergence

    def test_last_snapshot_survives_unavailable_and_is_the_next_baseline(self):
        from collector import catalog

        state, div = self._go([cmp("complete", 5, 0, T(1)), cmp("unavailable"), cmp("complete", 9, 0, T(3))])
        self.assertEqual([len(d) for d in div], [0, 0, 1])
        self.assertEqual((div[2][0]["details"]["previous_ahead"], div[2][0]["details"]["ahead"]), (5, 9))
        state, div = self._go([cmp("complete", 5, 0, T(1)), cmp("unavailable")])
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 100][0]
        self.assertEqual((entry["ahead"], entry["behind"], entry["comparison_state"]), (5, 0, "complete"))
        self.assertIs(entry["comparison_stale"], True)
        self.assertEqual(entry["comparison"]["last_reported_state"], "unavailable")
        self.assertEqual(catalog.collection_status(state)["comparison_stale"], [100])
        # Never comparable: null and unavailable, not stale.
        state, _ = self._go([cmp("unavailable")])
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 100][0]
        self.assertEqual((entry["ahead"], entry["comparison_state"], entry["comparison_stale"]), (None, "unavailable", False))

    def test_older_snapshot_is_ignored(self):
        state, div = self._go([cmp("complete", 5, 0, T(1)), cmp("complete", 1, 0, "2026-10-01T00:00:00Z"), cmp("complete", 1, 0, T(3))])
        self.assertEqual([len(d) for d in div], [0, 0, 1])
        mid, _ = self._go([cmp("complete", 5, 0, T(1)), cmp("complete", 1, 0, "2026-10-01T00:00:00Z")])
        self.assertEqual(mid["repositories"]["100"]["comparison"]["ahead"], 5)
        self.assertEqual(mid["repositories"]["100"]["comparison"]["collected_at"], T(1))
        self.assertEqual(state["repositories"]["100"]["comparison"]["ahead"], 1)

    def test_base_change_resets_silently_and_is_recorded(self):
        state, div = self._go([cmp("complete", 5, 0, T(1)), cmp("complete", 6, 0, T(2), base=91, base_ref="dev"), cmp("complete", 7, 0, T(3), base=91, base_ref="dev")])
        self.assertEqual([len(d) for d in div], [0, 0, 1])
        history = state["repositories"]["100"]["comparison_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["base_repository_id"], history[0]["ahead"], history[0]["replaced_at"]), (90, 5, T(2)))
        state, div = self._go([cmp("complete", 5, 0, T(1)), cmp("complete", 5, 0, T(2), base_ref="dev")])
        self.assertEqual(div[1], [])

    def test_state_flip_with_equal_counts_and_non_forks_emit_nothing(self):
        _, div = self._go([cmp("complete", 5, 0, T(1)), cmp("partial", 5, 0, T(2)), cmp("complete", 5, 0, T(3))])
        self.assertEqual([len(d) for d in div], [0, 0, 0])
        _, div = self._go([cmp("complete", 5, 0, T(1)), cmp("partial", 6, 0, T(2))])
        self.assertEqual([len(d) for d in div], [0, 1])
        state, div = self._go([cmp("complete", 5, 0, T(1)), cmp("complete", 6, 0, T(2))], is_fork=False)
        self.assertEqual([len(d) for d in div], [0, 0])
        self.assertEqual(state["repositories"]["100"]["comparison"]["ahead"], 6)

    def test_future_collected_at_is_clamped_at_load(self):
        from collector import schema

        data = run(T(5), [frame(), skyfall(comparison=cmp("complete", 5, 0, "2027-01-01T00:00:00Z"))])
        schema.validate_run(data)
        c = data["repositories"][1]["comparison"]
        self.assertEqual(c["collected_at"], T(5))
        self.assertIs(c["future_dated"], True)
        state, div = self._go([cmp("complete", 5, 0, T(1)), cmp("complete", 6, 0, "2027-01-01T00:00:00Z")])
        self.assertEqual(div[1][0]["source_at"], T(2))
        self.assertIs(div[1][0]["details"]["future_dated"], True)
        self.assertEqual(state["repositories"]["100"]["comparison"]["collected_at"], T(2))

    def test_loader_rejects_malformed_comparisons(self):
        from collector import schema

        for bad in (
            cmp("complete", True, 0, T(1)),
            cmp("complete", -1, 0, T(1)),
            cmp("complete", 2**60, 0, T(1)),
            cmp("complete", None, 0, T(1)),
            dict(cmp("unavailable"), extra=1),
            {"state": "complete", "ahead": 0, "behind": 0},
            cmp("not_comparable", 0, 0),
        ):
            with self.assertRaises(schema.SchemaError, msg=bad):
                schema.validate_run(run(T(5), [frame(), skyfall(comparison=bad)]))


class G8HostileFieldsNeverRefuseTheRun(CollectorTestCase):
    def test_invalid_ref_downgrades_the_comparison_and_is_reported(self):
        from collector import catalog, schema

        for ref in ("javascript:alert(1)", "@everyone", "<b>x</b>", "x‮evil", "http://invalid.example/", "a..b", "a@{b", ".hidden", "with space", "a/.b"):
            data = run(T(5), [frame(), skyfall(comparison=cmp("complete", 5, 0, T(5), base_ref=ref))])
            schema.validate_run(data)
            c = data["repositories"][1]["comparison"]
            self.assertEqual((c["state"], c["ahead"], c["base_ref"]), ("unavailable", None, None), ref)
            self.assertEqual(data["rejected_fields"], [{"path": "repositories[1].comparison.base_ref", "reason": "invalid_ref"}])
        state, events = self.assertDeterministic([
            run(T(5), [frame(), skyfall(comparison=cmp("complete", 5, 0, T(5)))]),
            run(T(6), [frame(), skyfall(comparison=cmp("complete", 6, 0, T(6), base_ref="data:x"))]),
            run(T(7), [frame(), skyfall(comparison=cmp("complete", 7, 0, T(7)))]),
        ])
        self.assertEqual(events[1], [])
        mid, _ = run_runs([run(T(5), [frame(), skyfall(comparison=cmp("complete", 5, 0, T(5)))]), run(T(6), [frame(), skyfall(comparison=cmp("complete", 6, 0, T(6), base_ref="data:x"))])])
        status = catalog.collection_status(mid)
        self.assertEqual(status["rejected_fields"], [{"path": "repositories[1].comparison.base_ref", "reason": "invalid_ref"}])
        self.assertIs(mid["repositories"]["100"]["comparison_status"]["stale"], True)
        self.assertEqual([e["type"] for e in events[2]], ["fork_divergence_changed"])
        self.assertEqual(events[2][0]["details"]["previous_ahead"], 5)
        self.assertEqual(catalog.collection_status(state)["rejected_fields"], [])
        for ok in ("main", "feature/x-y", "release+1.2", "v1.0"):
            data = run(T(5), [frame(), skyfall(comparison=cmp("complete", 5, 0, T(5), base_ref=ok))])
            schema.validate_run(data)
            self.assertEqual(data["repositories"][1]["comparison"]["base_ref"], ok)
        # An over-long ref is cut to the text bound first and then judged on its characters.
        data = run(T(5), [frame(), skyfall(comparison=cmp("complete", 5, 0, T(5), base_ref="a" * 300))])
        schema.validate_run(data)
        self.assertEqual(data["repositories"][1]["comparison"]["base_ref"], "a" * schema.MAX_TAG)
        self.assertEqual(data["rejected_fields"], [])

    def test_invalid_cursor_is_dropped_and_reported(self):
        from collector import catalog, schema

        roots = [{"repository_id": 90, "full_name": FRAME, "forks_scan_complete": False, "found_repository_ids": [], "resume_cursor": "javascript:alert(1)//@everyone‮<x>"}]
        data = run(T(5), [frame()], roots=roots)
        schema.validate_run(data)
        self.assertIsNone(data["roots"][0]["resume_cursor"])
        self.assertEqual(data["rejected_fields"], [{"path": "roots[0].resume_cursor", "reason": "invalid_cursor"}])
        state, _ = self.assertDeterministic([data])
        self.assertIsNone(state["collection"]["roots"]["90"]["resume_cursor"])
        self.assertEqual(catalog.collection_status(state)["rejected_fields"][0]["reason"], "invalid_cursor")
        good = dict(roots[0], resume_cursor="page=2:abc/def+g")
        data = run(T(5), [frame()], roots=[good])
        schema.validate_run(data)
        self.assertEqual(data["rejected_fields"], [])

    def test_search_query_with_a_url_is_a_clear_schema_error(self):
        from collector import schema

        for query in ("http://x in:readme", "ps3recomp https://invalid.example", "javascript:alert(1)", "<script>", "x‮y"):
            with self.assertRaises(schema.SchemaError) as ctx:
                schema.validate_run(run(T(5), [frame()], searches=[{"query": query, "complete": True, "repository_ids": [90]}]))
            self.assertIn("run.searches[0].query", str(ctx.exception))
        schema.validate_run(run(T(5), [frame()], searches=[{"query": 'ps3recomp "in:name" topic:ps3 -archived', "complete": True, "repository_ids": [90]}]))


class G9LesserFindings(CollectorTestCase):
    def test_no_inheritance_through_an_inconsistent_chain(self):
        a = fork(10, "loop/a", 11, 11, readme="no heading\n")
        b = fork(11, "loop/b", 10, 10, readme="# Skyfall\n")
        state, _ = self.assertDeterministic([run(T(5), [a, b])])
        self.assertEqual(state["repositories"]["10"]["ancestry_state"], "inconsistent")
        self.assertIsNone(state["repositories"]["10"]["game"]["key"])
        self.assertEqual(state["repositories"]["11"]["game"]["basis"], "readme_title")

    def test_contradicting_root_non_fork_with_parent_and_deep_chain_are_not_resolved(self):
        from collector import forks

        repos = [frame(), skyfall(), fork(101, "x/a", 100, 555), repo(rid=102, name="y/b", readme="", desc=None, parent_repository_id=100, network_root_repository_id=90)]
        state, _ = self.assertDeterministic([run(T(5), repos)])
        self.assertEqual(state["repositories"]["101"]["ancestry_state"], "inconsistent")
        self.assertEqual(state["repositories"]["102"]["ancestry_state"], "inconsistent")
        self.assertIsNone(state["repositories"]["101"]["game"]["inherited_from"])
        chain = [repo(rid=1, name="top/one", readme="# Skyfall\n")] + [fork(i, "deep/f%d" % i, i - 1, 1) for i in range(2, 60)]
        state, _ = self.assertDeterministic([run(T(5), chain)])
        self.assertEqual(state["repositories"]["59"]["ancestry_state"], "depth_exceeded")
        self.assertIsNone(state["repositories"]["59"]["game"]["key"])
        self.assertEqual(state["repositories"][str(forks.MAX_ANCESTRY_DEPTH)]["ancestry_state"], "resolved")
        self.assertEqual(state["repositories"][str(forks.MAX_ANCESTRY_DEPTH)]["game"]["inherited_from"], 1)

    def test_cursor_kept_when_a_later_partial_scan_omits_it(self):
        partial = lambda cursor: [{"repository_id": 90, "full_name": FRAME, "forks_scan_complete": False, "found_repository_ids": [], "resume_cursor": cursor}]
        state, _ = self.assertDeterministic([run(T(5), [frame()], roots=partial("page=2")), run(T(6), [frame()], roots=partial(None))])
        self.assertEqual(state["collection"]["roots"]["90"]["resume_cursor"], "page=2")

    def test_found_ids_replaced_by_complete_scan_and_unioned_across_partial(self):
        from collector import schema

        def root(found, complete):
            return [{"repository_id": 90, "full_name": FRAME, "forks_scan_complete": complete, "found_repository_ids": found}]

        state, _ = self.assertDeterministic([run(T(5), [frame()], roots=root([100, 101], True)), run(T(6), [frame()], roots=root([102], False)), run(T(7), [frame()], roots=root([103], True))])
        mid, _ = run_runs([run(T(5), [frame()], roots=root([100, 101], True)), run(T(6), [frame()], roots=root([102], False))])
        self.assertEqual(mid["collection"]["roots"]["90"]["found_repository_ids"], [100, 101, 102])
        self.assertEqual(state["collection"]["roots"]["90"]["found_repository_ids"], [103])
        # Bounded by dropping the oldest-seen ids, not the highest.
        many = list(range(10, 10 + schema.MAX_IDS))
        state, _ = run_runs([run(T(5), [frame()], roots=root(many, True)), run(T(6), [frame()], roots=root([5, 6], False))])
        found = state["collection"]["roots"]["90"]["found_repository_ids"]
        self.assertEqual(len(found), schema.MAX_IDS)
        self.assertEqual(found[-2:], [5, 6])
        self.assertNotIn(10, found)
        self.assertIn(10 + schema.MAX_IDS - 1, found)

    def test_dropped_root_stays_incomplete_and_later_fetch_gets_its_roots(self):
        from collector import catalog

        partial = [{"repository_id": 90, "full_name": FRAME, "forks_scan_complete": False, "found_repository_ids": [555], "resume_cursor": "c1"}]
        state, _ = self.assertDeterministic([run(T(5), [frame()], roots=partial), run(T(6), [frame()], roots=[])])
        status = catalog.collection_status(state)
        self.assertIs(status["incomplete"], True)
        self.assertEqual(status["incomplete_roots"], [90])
        self.assertEqual(state["collection"]["pending_repository_ids"], [555])
        state, events = self.assertDeterministic([run(T(5), [frame()], roots=partial), run(T(6), [frame(), fork(555, "late/one", 90, 90)], roots=[])])
        obs = state["repositories"]["555"]
        self.assertEqual(obs["discovery_roots"], [90])
        self.assertEqual(obs["discovery_sources"], ["fork_network:90"])
        self.assertEqual(state["collection"]["pending_repository_ids"], [])
        self.assertEqual(types_by_repo(events[1]), [(555, "fork_discovered"), (555, "repository_discovered")])

    def test_discovery_roots_accumulate_across_runs(self):
        r = lambda rid, found: {"repository_id": rid, "full_name": "frame-org/ps3recomp-example" if rid == 90 else "game-org/skyfall-recomp", "forks_scan_complete": True, "found_repository_ids": found}
        child = fork(301, "fork-a/skyfall-recomp", 100, 90, readme="# Fictional\n")
        state, _ = self.assertDeterministic([run(T(5), [frame(), skyfall(), child], roots=[r(90, [301])]), run(T(6), [frame(), skyfall(), child], roots=[r(100, [301])])])
        self.assertEqual(state["repositories"]["301"]["discovery_roots"], [90, 100])
        # Even when root 90's later complete scan no longer lists 301, the record of
        # having been found through it stays.
        state, _ = self.assertDeterministic([
            run(T(5), [frame(), skyfall(), child], roots=[r(90, [301])]),
            run(T(6), [frame(), skyfall(), child], roots=[r(90, []), r(100, [301])]),
            run(T(7), [frame(), skyfall(), child], roots=[r(100, [301])]),
        ])
        self.assertEqual(state["collection"]["roots"]["90"]["found_repository_ids"], [])
        self.assertEqual(state["repositories"]["301"]["discovery_roots"], [90, 100])
        self.assertIn("fork_network:90", state["repositories"]["301"]["discovery_sources"])

    def test_duplicate_roots_are_rejected(self):
        from collector import schema

        roots = [{"repository_id": 90, "full_name": FRAME, "forks_scan_complete": True, "found_repository_ids": []}] * 2
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(run(T(5), [frame()], roots=roots))

    def test_version_1_state_is_refused_with_a_clear_message(self):
        from collector import schema, store

        state, _ = run_runs([run(T(5), [frame()])])
        self.assertEqual(state["schema_version"], 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            store.save_state(state, path)
            self.assertEqual(store.load_state(path)["schema_version"], 2)
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            raw["schema_version"] = 1
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(raw, fh)
            with self.assertRaises(schema.SchemaError) as ctx:
                store.load_state(path)
            self.assertIn("schema_version 1 is not supported", str(ctx.exception))
            self.assertIn("replaying the run files", str(ctx.exception))

    def test_validate_state_rejects_inconsistent_step2_fields(self):
        from collector import schema

        state, _ = run_runs([run(T(5), [frame(), skyfall()], roots=[{"repository_id": 90, "full_name": FRAME, "forks_scan_complete": False, "found_repository_ids": [100], "resume_cursor": "c"}])])
        schema.validate_state(copy.deepcopy(state))

        def broken(mutate):
            bad = copy.deepcopy(state)
            mutate(bad)
            with self.assertRaises(schema.SchemaError):
                schema.validate_state(bad)

        broken(lambda s: s["repositories"]["100"]["comparison"].pop("base_sha"))
        broken(lambda s: s["repositories"]["100"]["comparison"].pop("head_ref"))
        broken(lambda s: s["collection"]["roots"]["90"].__setitem__("scan_state", "complete"))
        broken(lambda s: s["collection"]["roots"]["90"].__setitem__("resume_cursor", True))
        broken(lambda s: s["repositories"]["90"].__setitem__("discovery_roots", [90, 90]))
        broken(lambda s: s["repositories"]["90"].__setitem__("ancestry_state", "resolved"))
        broken(lambda s: s["repositories"]["90"]["game"].__setitem__("verified", True))
        broken(lambda s: s["repositories"]["100"]["game"].__setitem__("basis", "inherited_unverified_copy"))

    def test_fork_activity_summary_reads_as_a_sentence(self):
        runs = [
            run(T(5), [frame([(1, T(1))]), skyfall([(1, T(1))])]),
            run(T(6), [frame([(1, T(1)), (3, T(6)), (4, T(6))]), skyfall([(1, T(1))])]),
            run(T(7), [frame([(1, T(1)), (3, T(6)), (4, T(6))]), skyfall([(1, T(1)), (3, T(6)), (4, T(6))])]),
        ]
        _, events = self.assertDeterministic(runs)
        self.assertEqual(events[2][0]["summary"], "2 commits observed on the tracked branch, all already observed elsewhere in the network")
        self.assertNotIn("tracked branch (", events[2][0]["summary"])

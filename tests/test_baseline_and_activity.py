"""T1-T12: baseline, new activity, batch boundaries, rename, history rewrite."""

import copy
import hashlib
import re

import helpers
from helpers import CollectorTestCase, canonical, load_run, mask_timestamps, run_runs


class BaselineUnchangedTests(CollectorTestCase):
    def test_t1_first_run_is_a_silent_baseline(self):
        state, events = self.assertScenarioDeterministic("baseline_unchanged", upto=1)
        self.assertEqual(events[0], [])
        self.assertEqual(sorted(state["repositories"]), ["100", "90"])
        self.assertEqual(state["events"], [])
        obs = state["repositories"]["100"]
        self.assertEqual(
            sorted(obs["observed_commits"]),
            [
                "000000000000000000000000000000000000000a",
                "000000000000000000000000000000000000000b",
            ],
        )
        self.assertIn("7001", obs["releases"])
        self.assertEqual(obs["stars"], 5)
        self.assertEqual(obs["first_seen_at"], "2026-11-05T00:00:00Z")

    def test_t2_identical_rerun_is_silent_and_state_unchanged(self):
        runs = [load_run("baseline_unchanged", 1), load_run("baseline_unchanged", 2)]
        state_after_1, _ = run_runs(runs[:1])
        state_after_2, events = self.assertDeterministic(runs)
        self.assertEqual(events[1], [])
        self.assertEqual(state_after_2["events"], [])
        self.assertEqual(
            canonical(mask_timestamps(state_after_1)),
            canonical(mask_timestamps(state_after_2)),
        )
        # The timestamps themselves did move.
        self.assertEqual(state_after_2["last_run_at"], "2026-11-05T06:00:00Z")

    def test_t3_run_1_twice_from_empty_gives_identical_state(self):
        run = load_run("baseline_unchanged", 1)
        state_a, _ = run_runs([run])
        state_b, _ = run_runs([run])
        self.assertEqual(canonical(state_a), canonical(state_b))


class NewActivityTests(CollectorTestCase):
    def test_t4_run_2_one_commit_group_and_one_release(self):
        state, events = self.assertScenarioDeterministic("new_activity", upto=2)
        self.assertValidEvents(events[1])
        self.assertEqual(sorted(self.types(events[1])), ["commits_observed", "release_published"])
        for e in events[1]:
            self.assertEqual(e["repository_id"], 100)
        commits = [e for e in events[1] if e["type"] == "commits_observed"][0]
        self.assertEqual(
            commits["details"]["commit_shas"],
            [
                "000000000000000000000000000000000000000c",
                "000000000000000000000000000000000000000d",
            ],
        )
        self.assertEqual(commits["details"]["commit_count"], 2)
        self.assertTrue(commits["details"]["complete"])
        self.assertEqual(commits["source_at"], "2026-11-06T12:00:00Z")
        self.assertEqual(commits["observed_at"], "2026-11-06T12:00:00Z")
        release = [e for e in events[1] if e["type"] == "release_published"][0]
        self.assertEqual(release["details"]["release_id"], 7002)
        self.assertEqual(release["event_id"], "repo-100:release:7002")
        self.assertEqual(release["source_at"], "2026-11-06T09:00:00Z")
        # Nothing for repository 90.
        self.assertEqual([e for e in state["events"] if e["repository_id"] == 90], [])

    def test_t5_archived_emitted_once_on_false_to_true(self):
        _, events = self.assertScenarioDeterministic("new_activity", upto=4)
        self.assertEqual(self.types(events[2]), ["repository_archived"])
        self.assertEqual(events[2][0]["repository_id"], 100)
        self.assertEqual(events[2][0]["details"], {"archived": True, "previous": False})
        # Still archived in run 4: no repeat.
        self.assertNotIn("repository_archived", self.types(events[3]))
        # Re-applying run 3 on the state after run 3: no repeat either.
        runs = [load_run("new_activity", i) for i in (1, 2, 3, 3)]
        _, events_again = run_runs(runs)
        self.assertEqual(events_again[3], [])

    def test_t6_new_repository_is_discovered_without_history_flood(self):
        state, events = self.assertScenarioDeterministic("new_activity", upto=4)
        self.assertValidEvents(events[3])
        self.assertEqual(self.types(events[3]), ["repository_discovered"])
        disc = events[3][0]
        self.assertEqual(disc["repository_id"], 110)
        self.assertEqual(disc["event_id"], "repo-110:discovered")
        self.assertEqual(disc["source_url"], "https://github.com/solo-dev/lonely-port")
        # Its pre-existing history is stored but not announced.
        self.assertIn("0000000000000000000000000000000000000014", state["repositories"]["110"]["observed_commits"])
        self.assertEqual([e for e in state["events"] if e["repository_id"] == 110 and e["type"] != "repository_discovered"], [])
        self.assertEqual(state["repositories"]["110"]["first_seen_at"], "2026-11-08T00:00:00Z")

    def test_t7_rerunning_any_step_on_persisted_state_is_idempotent(self):
        base = [load_run("new_activity", i) for i in (1, 2, 3, 4)]
        for k in range(1, 5):
            runs = base[:k] + [base[k - 1]]
            _, events = self.assertDeterministic(runs)
            self.assertEqual(events[-1], [], "re-run of step %d emitted events" % k)


class BatchBoundaryTests(CollectorTestCase):
    SHA_A = "000000000000000000000000000000000000000a"
    SHA_30 = "000000000000000000000000000000000000001e"
    SHA_31 = "000000000000000000000000000000000000001f"
    SHA_32 = "0000000000000000000000000000000000000020"

    def test_t8_individual_shas_persisted_independently(self):
        state, events = self.assertScenarioDeterministic("batch_boundaries", upto=3)
        observed = state["repositories"]["100"]["observed_commits"]
        for sha in (self.SHA_A, self.SHA_30, self.SHA_31, self.SHA_32):
            self.assertIn(sha, observed)
            self.assertIn("first_observed_at", observed[sha])
        self.assertEqual(observed[self.SHA_A]["first_observed_at"], "2026-11-05T00:00:00Z")
        self.assertEqual(observed[self.SHA_30]["first_observed_at"], "2026-11-05T12:00:00Z")
        self.assertEqual(observed[self.SHA_32]["first_observed_at"], "2026-11-06T12:00:00Z")
        # Old sha gone from the window is still in the history.
        self.assertIn(self.SHA_A, observed)

    def test_t9_overlap_window_notifies_only_the_new_sha(self):
        _, events = self.assertScenarioDeterministic("batch_boundaries", upto=3)
        self.assertEqual(self.types(events[1]), ["commits_observed"])
        self.assertEqual(events[1][0]["details"]["commit_shas"], [self.SHA_30, self.SHA_31])
        self.assertEqual(self.types(events[2]), ["commits_observed"])
        self.assertEqual(events[2][0]["details"]["commit_shas"], [self.SHA_32])
        self.assertEqual(events[2][0]["details"]["commit_count"], 1)
        self.assertNotEqual(events[1][0]["event_id"], events[2][0]["event_id"])

    def test_t10_grouped_event_id_is_digest_of_sorted_shas(self):
        from collector import model

        _, events = self.assertScenarioDeterministic("batch_boundaries", upto=2)
        a = model.commits_event_id(100, [self.SHA_30, self.SHA_31])
        b = model.commits_event_id(100, [self.SHA_31, self.SHA_30])
        self.assertEqual(a, b)
        self.assertEqual(events[1][0]["event_id"], a)
        digest = hashlib.sha256(("\n".join(sorted([self.SHA_30, self.SHA_31]))).encode("ascii")).hexdigest()
        self.assertEqual(a, "repo-100:commits:" + digest)
        self.assertNotEqual(a, model.commits_event_id(101, [self.SHA_30, self.SHA_31]))


class RenameTests(CollectorTestCase):
    def test_t11_rename_keeps_identity_and_history(self):
        from collector import catalog

        state, events = self.assertScenarioDeterministic("rename")
        self.assertEqual(events[1], [])
        self.assertEqual(list(state["repositories"]), ["100"])
        obs = state["repositories"]["100"]
        self.assertEqual(obs["full_name"], "new-owner/skyfall-recomp-renamed")
        self.assertEqual(obs["owner"], "new-owner")
        self.assertEqual(
            [h["full_name"] for h in obs["name_history"]],
            ["game-org/skyfall-recomp"],
        )
        self.assertEqual(obs["name_history"][0]["until"], "2026-11-05T12:00:00Z")
        entries = catalog.build_catalog(state)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["repo"], "new-owner/skyfall-recomp-renamed")
        self.assertEqual(entries[0]["source_url"], "https://github.com/new-owner/skyfall-recomp-renamed")
        self.assertEqual(state["events"], [])


class HistoryRewriteTests(CollectorTestCase):
    OLD = ["000000000000000000000000000000000000000a", "000000000000000000000000000000000000000b"]
    NEW = [
        "0000000000000000000000000000000000000028",
        "0000000000000000000000000000000000000029",
        "000000000000000000000000000000000000002a",
    ]

    def _check(self, state, events):
        observed = state["repositories"]["100"]["observed_commits"]
        for sha in self.OLD + self.NEW:
            self.assertIn(sha, observed)
        self.assertTrue(state["repositories"]["100"]["history_rewrite_suspected"])
        self.assertLessEqual(len(events[1]), 1)
        for e in events[1]:
            self.assertEqual(e["type"], "commits_observed")
            self.assertFalse(e["details"]["complete"])
            self.assertTrue(e["details"]["history_rewrite_suspected"])
            self.assertIsNone(e["details"]["commit_count"])
            self.assertFalse(e["details"]["count_exact"])
            self.assertIsNone(re.search(r"\d+ (new|previously unseen|commits)", e["summary"]), e["summary"])

    def test_t12_rewrite_with_hint_is_recorded_conservatively(self):
        state, events = self.assertScenarioDeterministic("history_rewrite")
        self._check(state, events)

    def test_t12_rewrite_without_hint_is_still_detected(self):
        run1 = load_run("history_rewrite", 1)
        run2 = copy.deepcopy(load_run("history_rewrite", 2))
        del run2["repositories"][0]["rewrite_hint"]
        state, events = self.assertDeterministic([run1, run2])
        self._check(state, events)

    def test_t12_overlap_window_is_not_mistaken_for_a_rewrite(self):
        state, _ = self.assertScenarioDeterministic("batch_boundaries")
        self.assertFalse(state["repositories"]["100"]["history_rewrite_suspected"])

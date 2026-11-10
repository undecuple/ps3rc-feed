"""T13-T19: partial pagination, nulls, stale debounce, recovery, not found, truncated search."""

import copy

from helpers import CollectorTestCase, load_run, run_runs


def _rate_limited(repo_id, full_name, owner):
    return {
        "repository_id": repo_id,
        "full_name": full_name,
        "owner": owner,
        "description": "Fictional project",
        "readme_excerpt": "# Fictional\n",
        "is_fork": False,
        "parent_repository_id": None,
        "network_root_repository_id": None,
        "archived": None,
        "stars": None,
        "default_branch": "main",
        "fetch_state": "rate_limited",
        "commits_complete": True,
        "commits": None,
        "releases": None,
    }


class PartialPaginationTests(CollectorTestCase):
    def test_t13_incomplete_commits_are_a_lower_bound(self):
        from collector import catalog

        state, events = self.assertScenarioDeterministic("failures", upto=2)
        obs = state["repositories"]["100"]
        self.assertIs(obs["commits_complete"], False)
        self.assertEqual(self.types(events[1]), ["commits_observed"])
        e = events[1][0]
        self.assertEqual(e["details"]["commit_shas"], ["000000000000000000000000000000000000000e"])
        self.assertIs(e["details"]["complete"], False)
        self.assertIs(e["details"]["count_exact"], False)
        self.assertEqual(e["details"]["commit_count"], 1)
        self.assertIn("at least", e["summary"])
        self.assertIn("incomplete", e["summary"])
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 100][0]
        self.assertIs(entry["commits_30d_complete"], False)
        self.assertIs(entry["commits_complete"], False)


class NullHandlingTests(CollectorTestCase):
    def test_t14_rate_limited_keeps_known_values_and_marks_them_stale(self):
        state, events = self.assertScenarioDeterministic("failures", upto=3)
        obs = state["repositories"]["120"]
        self.assertEqual(obs["stars"], 5)
        self.assertIs(obs["archived"], False)
        self.assertIn("0000000000000000000000000000000000000032", obs["observed_commits"])
        self.assertIs(obs["stale"], True)
        self.assertEqual(obs["fetch_state"], "rate_limited")
        self.assertEqual(obs["last_successful_refresh_at"], "2026-11-06T00:00:00Z")
        self.assertEqual(obs["last_attempted_refresh_at"], "2026-11-06T06:00:00Z")
        self.assertEqual(events[2], [])

    def test_t14_never_known_values_are_null_not_zero(self):
        from collector import catalog

        run1 = load_run("failures", 1)
        run2 = copy.deepcopy(load_run("failures", 2))
        run2["repositories"].append(_rate_limited(130, "quiet-org/unknown-port", "quiet-org"))
        run2["searches"][0]["repository_ids"].append(130)
        state, events = self.assertDeterministic([run1, run2])
        obs = state["repositories"]["130"]
        self.assertIsNone(obs["stars"])
        self.assertIsNone(obs["archived"])
        self.assertIsNone(obs["commits_complete"])
        self.assertEqual(obs["observed_commits"], {})
        self.assertIsNone(obs["last_successful_refresh_at"])
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 130][0]
        self.assertIsNone(entry["stars"])
        self.assertIsNone(entry["commits_30d"])
        self.assertIsNone(entry["archived"])
        self.assertIsNone(entry["last_commit_at"])
        # The repository is still discovered, with unknown metrics.
        self.assertIn("repository_discovered", self.types(events[1]))


class StaleDebounceTests(CollectorTestCase):
    def test_t15_stale_only_after_threshold_and_only_once(self):
        _, events = self.assertScenarioDeterministic("failures", upto=5, stale_threshold=3)
        self.assertEqual(events[2], [])  # one failed request
        self.assertEqual(events[3], [])  # second
        self.assertEqual(self.types(events[4]), ["collection_stale"])
        self.assertValidEvents(events[4])
        stale = events[4][0]
        self.assertEqual(stale["repository_id"], 120)
        self.assertEqual(stale["details"]["consecutive_failures"], 3)
        self.assertEqual(stale["details"]["threshold"], 3)
        self.assertEqual(stale["summary"], "refresh failed 3 times in a row")
        # A further failure does not repeat the warning.
        runs = [load_run("failures", i) for i in (1, 2, 3, 4, 5, 5)]
        _, again = run_runs(runs)
        self.assertEqual(again[5], [])

    def test_t15_threshold_comes_from_config(self):
        _, events = self.assertScenarioDeterministic("failures", upto=4, stale_threshold=2)
        self.assertEqual(events[2], [])
        self.assertEqual(self.types(events[3]), ["collection_stale"])

    def test_t16_recovered_exactly_once_and_only_if_stale(self):
        _, events = self.assertScenarioDeterministic("failures", upto=6)
        self.assertEqual(self.types(events[5]), ["collection_recovered"])
        self.assertEqual(events[5][0]["repository_id"], 120)
        # Never stale (one failure, then success): no recovery event.
        runs = [load_run("failures", i) for i in (1, 3, 6)]
        _, quiet = self.assertDeterministic(runs)
        self.assertEqual(quiet[1], [])
        self.assertEqual(quiet[2], [])


class NotFoundAndTruncationTests(CollectorTestCase):
    def test_t17_not_found_keeps_the_entry_and_its_data(self):
        state, events = self.assertScenarioDeterministic("failures", upto=7)
        self.assertIn("120", state["repositories"])
        obs = state["repositories"]["120"]
        self.assertEqual(obs["collection_state"], "not_found")
        self.assertEqual(obs["fetch_state"], "not_found")
        self.assertEqual(obs["stars"], 5)
        self.assertIn("0000000000000000000000000000000000000032", obs["observed_commits"])
        self.assertEqual(events[6], [])
        for e in state["events"]:
            self.assertNotIn("delet", e["summary"])
            self.assertNotIn("removed", e["summary"])

    def test_t18_truncated_search_is_recorded_and_deletes_nothing(self):
        state, events = self.assertScenarioDeterministic("failures")
        search = state["collection"]["searches"]["ps3 recomp in:readme"]
        self.assertIs(search["complete"], False)
        self.assertIs(state["collection"]["incomplete"], True)
        self.assertIn("ps3 recomp in:readme", state["collection"]["incomplete_searches"])
        self.assertEqual(sorted(state["repositories"]), ["100", "120"])
        self.assertEqual(events[7], [])
        # Earlier complete search is still recorded as complete.
        self.assertIs(state["collection"]["searches"]["ps3recomp in:name"]["complete"], True)

    def test_t19_unknown_to_archived_is_not_a_transition(self):
        run1 = load_run("failures", 1)
        run2 = copy.deepcopy(load_run("failures", 2))
        run2["repositories"].append(_rate_limited(130, "quiet-org/unknown-port", "quiet-org"))
        run3 = copy.deepcopy(load_run("failures", 3))
        ok = _rate_limited(130, "quiet-org/unknown-port", "quiet-org")
        ok.update({"fetch_state": "ok", "archived": True, "stars": 1, "commits": [], "releases": []})
        run3["repositories"].append(ok)
        state, events = self.assertDeterministic([run1, run2, run3])
        self.assertNotIn("repository_archived", self.types(events[2]))
        self.assertIs(state["repositories"]["130"]["archived"], True)

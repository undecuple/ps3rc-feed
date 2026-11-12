"""The removed state: a repository not found for as many runs in a row as the stale threshold is shown as removed."""

import copy
import json
from datetime import datetime, timedelta, timezone

from helpers import CollectorTestCase, load_run

from collector import catalog, events as events_mod, store

LAST = 7


def later(run, hours):
    """A copy of the run one or more hours later (every run time field moves; the contents stay)."""
    out = copy.deepcopy(run)
    stamp = datetime.strptime(run["run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(hours=hours)
    text = json.dumps(out)
    text = text.replace(run["run_at"], stamp.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return json.loads(text)


def apply(runs, threshold=3):
    config = events_mod.Config(stale_threshold=threshold)
    state = store.empty_state()
    per_run = [events_mod.apply_run(state, copy.deepcopy(r), config) for r in runs]
    return state, per_run


def entry(state, rid):
    return next(e for e in catalog.build_catalog(state) if e["id"] == rid)


class RemovedState(CollectorTestCase):
    def runs(self, not_found_runs):
        base = [load_run("failures", i) for i in range(1, LAST + 1)]
        return base + [later(base[-1], 6 * n) for n in range(1, not_found_runs)]

    def test_one_not_found_is_failing_not_removed(self):
        state, _ = apply(self.runs(1))
        rec = entry(state, 120)
        self.assertEqual((rec["collection_failing"], rec["removed"]), (True, False))

    def test_three_not_found_in_a_row_is_removed_with_one_event(self):
        state, per_run = apply(self.runs(3))
        rec = entry(state, 120)
        self.assertIs(rec["removed"], True)
        stale = [e for run in per_run[LAST:] for e in run if e["type"] == "collection_stale" and e["repository_id"] == 120]
        self.assertEqual(len(stale), 1)
        self.assertIn("no longer found on GitHub for 3 checks in a row", stale[0]["summary"])
        self.assertIn("unverified", stale[0]["summary"])

    def test_a_further_not_found_run_adds_no_second_event(self):
        _, per_run = apply(self.runs(4))
        stale = [e for run in per_run[LAST:] for e in run if e["type"] == "collection_stale" and e["repository_id"] == 120]
        self.assertEqual(len(stale), 1)

    def test_an_ordinary_failure_that_goes_stale_is_not_removed(self):
        state, per_run = apply([load_run("failures", i) for i in range(1, 6)])
        stale = [e for run in per_run for e in run if e["type"] == "collection_stale"]
        self.assertTrue(stale)
        self.assertTrue(all("no longer found" not in e["summary"] for e in stale))
        self.assertTrue(all(e["removed"] is False for e in catalog.build_catalog(state)))

    def test_a_repository_that_comes_back_is_no_longer_removed(self):
        runs = self.runs(3)
        back = later(load_run("failures", 6), 6 * 3)  # the run in which repository 120 was fetched again
        state, per_run = apply(runs + [back])
        self.assertIs(entry(state, 120)["removed"], False)

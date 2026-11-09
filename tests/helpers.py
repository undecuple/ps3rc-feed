"""Shared helpers for the step 1 tests.

Every scenario is a directory of numbered run files under tests/fixtures.
The helpers run them in order through the collector and return the state
and the events of every run, so tests can look at any step. They also
provide the determinism check the test plan asks for on every test.
"""

import copy
import json
import os
import re
import unittest

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Keys whose values are allowed to differ between an unchanged re-run and
# the run before it (test T2: "byte-identical apart from last-run timestamps").
RUN_TIMESTAMP_KEYS = {
    "last_run_at",
    "last_attempted_refresh_at",
    "last_successful_refresh_at",
    "last_seen_at",
}


def scenario_files(name):
    folder = os.path.join(FIXTURES, name)
    files = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
    return [os.path.join(folder, f) for f in files]


def load_run(name, index):
    """Return the parsed run file number `index` (1-based) of a scenario."""
    path = scenario_files(name)[index - 1]
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def mask_timestamps(obj):
    """Deep copy with the per-run timestamp values replaced by None."""
    if isinstance(obj, dict):
        return {
            k: (None if k in RUN_TIMESTAMP_KEYS else mask_timestamps(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask_timestamps(v) for v in obj]
    return obj


def run_runs(runs, stale_threshold=3):
    """Apply a list of already-parsed run dicts to an empty state.

    Returns (state, [events_of_run_1, events_of_run_2, ...]).
    """
    from collector import events as events_mod
    from collector import store

    config = events_mod.Config(stale_threshold=stale_threshold)
    state = store.empty_state()
    per_run = []
    for run in runs:
        emitted = events_mod.apply_run(state, copy.deepcopy(run), config)
        per_run.append(emitted)
    return state, per_run


def run_scenario(name, upto=None, stale_threshold=3):
    runs = [load_run(name, i + 1) for i in range(len(scenario_files(name)))]
    if upto is not None:
        runs = runs[:upto]
    return run_runs(runs, stale_threshold=stale_threshold)


def all_strings(obj):
    """Yield every string value anywhere inside a JSON-like structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            for s in all_strings(v):
                yield s
    elif isinstance(obj, list):
        for v in obj:
            for s in all_strings(v):
                yield s


EVENT_ID_RE = re.compile(r"^repo-[0-9]+:[a-z_]+(:[0-9a-f]+)?$")


class CollectorTestCase(unittest.TestCase):
    """Base class with the determinism assertion every test must make."""

    def assertDeterministic(self, runs, stale_threshold=3):
        state_a, events_a = run_runs(runs, stale_threshold)
        state_b, events_b = run_runs(runs, stale_threshold)
        self.assertEqual(canonical(events_a), canonical(events_b))
        self.assertEqual(canonical(state_a), canonical(state_b))
        ids_a = [e["event_id"] for run in events_a for e in run]
        ids_b = [e["event_id"] for run in events_b for e in run]
        self.assertEqual(ids_a, ids_b)
        return state_a, events_a

    def assertScenarioDeterministic(self, name, upto=None, stale_threshold=3):
        runs = [load_run(name, i + 1) for i in range(len(scenario_files(name)))]
        if upto is not None:
            runs = runs[:upto]
        return self.assertDeterministic(runs, stale_threshold)

    def assertValidEvents(self, events):
        from collector import schema

        for event in events:
            schema.validate_event(event)
            self.assertRegex(event["event_id"], EVENT_ID_RE)

    def types(self, events):
        return [e["type"] for e in events]

"""export --config: the deny list removes repositories, their events and their listings from the saved state and the feed."""

import copy
import json
import os

from helpers import run_scenario
from test_export import ExportTestCase

from collector import collect, events, export, forks, schema, store

ROOTS = [{"repository_id": 90, "full_name": "frame-org/ps3recomp-example"}]


def config(repositories=(), owners=(), roots=ROOTS, include=()):
    return collect.validate_config(
        {
            "queries": ["ps3recomp in:name"],
            "roots": copy.deepcopy(list(roots)),
            "include": copy.deepcopy(list(include)),
            "deny": {"repositories": list(repositories), "owners": list(owners)},
        }
    )


def scenario_state():
    # forks_two_roots: 90 <- 100 <- 301 <- 302 and 90 <- 303 <- 304; 302 inherits its game from 301,
    # and 304 (found after the baseline) has a repository_discovered and a fork_discovered event.
    return run_scenario("forks_two_roots")[0]


def mentions(obj, rid):
    return any(rid == v or "r%d" % rid == v for v in walk(obj))


def walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)
    else:
        yield obj


class PruneDenied(ExportTestCase):
    def test_denied_repository_its_events_and_listings_go(self):
        state = scenario_state()
        self.assertEqual(sorted(e["type"] for e in state["events"] if e["repository_id"] == 304), ["fork_discovered", "repository_discovered"])
        state["collection"]["pending_repository_ids"] = [301, 999]
        removed = export.prune_denied(state, config(repositories=["Fork-A/Skyfall-Recomp"], owners=["fork-d"]))
        self.assertEqual(removed, [301, 304])
        schema.validate_state(state)
        self.assertEqual(sorted(state["repositories"]), ["100", "302", "303", "90"])
        self.assertEqual(state["events"], [])
        col = state["collection"]
        self.assertEqual(col["pending_repository_ids"], [999])
        self.assertFalse(any(301 in s["repository_ids"] or 304 in s["repository_ids"] for s in col["searches"].values()))
        self.assertFalse(any(301 in r["found_repository_ids"] or 304 in r["found_repository_ids"] for r in col["roots"].values()))
        feed = export.build_feed(state)
        self.assertEqual([r["id"] for r in feed["repos"]], ["r90", "r100", "r302", "r303"])
        self.assertFalse(mentions(feed, 301) or mentions(feed, 304))

    def test_a_fork_of_a_denied_repository_stays_and_loses_the_link(self):
        state = scenario_state()
        self.assertEqual(state["repositories"]["302"]["game"]["inherited_from"], 301)
        export.prune_denied(state, config(repositories=["fork-a/skyfall-recomp"]))
        fork = state["repositories"]["302"]
        # GitHub's facts stay; the tracked link is gone from the feed.
        self.assertEqual(fork["parent_repository_id"], 301)
        record = [r for r in export.build_feed(state)["repos"] if r["id"] == "r302"][0]
        self.assertIsNone(record["parent"])
        forks.resolve_associations(state["repositories"], events.Config().aliases)
        self.assertNotEqual(fork["game"]["inherited_from"], 301)
        self.assertNotEqual(fork["toolkit"]["inherited_from"], 301)
        schema.validate_state(state)

    def test_roots_and_include_are_never_denied(self):
        # Matched by id: the config names them differently (a rename), so the config itself is accepted.
        cfg = config(
            owners=["frame-org"],
            repositories=["game-org/skyfall-recomp", "fork-c/unrelated-name"],
            roots=[{"repository_id": 90, "full_name": "renamed-org/ps3recomp-example"}],
            include=[{"repository_id": 100, "full_name": "renamed-org/skyfall-recomp"}],
        )
        state = scenario_state()
        self.assertEqual(export.prune_denied(state, cfg), [303])
        self.assertIn("90", state["repositories"])
        self.assertIn("100", state["repositories"])

    def test_idempotent_and_untouched_without_a_match(self):
        state = scenario_state()
        before = store.dumps(state)
        self.assertEqual(export.prune_denied(state, config()), [])
        self.assertEqual(store.dumps(state), before)
        cfg = config(owners=["fork-d"])
        self.assertEqual(export.prune_denied(state, cfg), [304])
        once = store.dumps(state)
        self.assertEqual(export.prune_denied(state, cfg), [])
        self.assertEqual(store.dumps(state), once)


class ExportWithConfig(ExportTestCase):
    def put_config(self, cfg):
        cfg = {k: cfg[k] for k in ("queries", "roots", "include", "deny")}
        path = os.path.join(self.repo, "sources.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return path

    def read_state(self, name):
        with open(os.path.join(self.data, name), "rb") as fh:
            return fh.read()

    def test_an_empty_deny_list_changes_no_byte(self):
        runs = self.put_scenario("forks_two_roots")
        code, _, err = self.export(runs, "data/a", ["--state-out", "data/a.json"])
        self.assertEqual(code, 0, err)
        code, out, err = self.export(runs, "data/b", ["--state-out", "data/b.json", "--config", self.put_config(config())])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["denied_removed"], 0)
        self.assertEqual(self.read("data/a")[2:], self.read("data/b")[2:])
        self.assertEqual(self.read_state("a.json"), self.read_state("b.json"))

    def test_denied_repositories_leave_the_state_and_the_feed_and_stay_out(self):
        runs = self.put_scenario("forks_two_roots")
        code, _, err = self.export(runs[:2], "data/site", ["--state-out", "data/state.json"])
        self.assertEqual(code, 0, err)
        cfg = self.put_config(config(repositories=["fork-a/skyfall-recomp"], owners=["fork-d"]))
        # The last run lists both again (made with an older config): they are removed after it too.
        code, out, err = self.export(
            runs[2:], "data/site", ["--overwrite", "--state-in", "data/state.json", "--state-out", "data/state.json", "--config", cfg]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["denied_removed"], 2)
        feed = self.read()[0]
        self.assertSiteShape(feed)
        self.assertNotIn("r301", [r["id"] for r in feed["repos"]])
        self.assertFalse(mentions(feed, 301) or mentions(feed, 304))
        state = store.load_state(os.path.join(self.data, "state.json"))
        self.assertNotIn("301", state["repositories"])
        self.assertNotIn("304", state["repositories"])
        self.assertFalse(any(e["repository_id"] in (301, 304) for e in state["events"]))
        fork = state["repositories"]["302"]
        self.assertNotEqual(fork["game"]["inherited_from"], 301)
        self.assertNotEqual(fork["toolkit"]["inherited_from"], 301)

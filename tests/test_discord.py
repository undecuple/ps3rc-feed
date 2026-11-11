"""Discord planning (collector/discord.py): which events become which messages, and the state around them.

Pure functions over a parsed feed and a delivery state, so the tests build
small fictional feeds. Nothing here sends anything: the module has no
socket, clock or webhook, and a test pins that.
"""

import ast
import copy
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import CollectorTestCase

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo(i, **extra):
    rec = {"id": "r%d" % i, "repo": "owner%d/repo%d" % (i, i), "url": "https://github.com/owner%d/repo%d" % (i, i),
           "fork": False, "game": "Game %d" % i, "game_confidence": "strong"}
    rec.update(extra)
    return rec


def event(i, kind, minute, repo_id=None, **extra):
    rec = {"id": "repo-%d:%s:%d" % (i, kind, minute), "type": kind, "repo_id": repo_id or "r%d" % i,
           "summary": "v1.%d published" % minute, "observed_at": "2026-11-11T10:%02d:00Z" % minute}
    rec.update(extra)
    return rec


def feed(repos, events):
    return {"generated_at": "2026-11-11T11:00:00Z", "repos": repos, "events": events}


def seen_state(*ids):
    from collector import discord

    state = discord.empty_state()
    state["seen"] = list(ids)
    return state


class PlanTests(CollectorTestCase):
    def setUp(self):
        super().setUp()
        from collector import discord

        self.d = discord

    def test_no_state_is_a_baseline_with_every_event_seen(self):
        f = feed([repo(1)], [event(1, "repository_discovered", 1), event(1, "release_published", 2)])
        plan = self.d.plan(f)
        self.assertTrue(plan["baseline"])
        self.assertEqual(plan["messages"], [])
        self.assertEqual(sorted(plan["state"]["seen"]), sorted(e["id"] for e in f["events"]))

    def test_seen_events_are_not_planned_again_and_unsent_types_are_only_marked_seen(self):
        events = [event(1, "repository_discovered", 1), event(2, "repository_discovered", 2),
                  event(3, "fork_discovered", 3), event(3, "game_association_changed", 4), event(3, "collection_stale", 5)]
        f = feed([repo(1), repo(2), repo(3)], events)
        plan = self.d.plan(f, seen_state(events[0]["id"]))
        self.assertFalse(plan["baseline"])
        self.assertEqual([m["event_ids"] for m in plan["messages"]], [[events[1]["id"]]])
        self.assertEqual(plan["state"]["seen"], [events[0]["id"]] + [e["id"] for e in events[2:]])

    def test_discovery_and_release_text_follow_the_template(self):
        f = feed([repo(1), repo(2, fork=True, game_confidence="low")],
                 [event(1, "repository_discovered", 1), event(2, "release_published", 2, summary="v2.0 published")])
        a, b = (m["payload"] for m in self.d.plan(f, seen_state())["messages"])
        self.assertEqual(a["content"].splitlines()[0], "[NEW] Game 1 — owner1/repo1")
        self.assertIn("Source: <https://github.com/owner1/repo1>", a["content"])
        self.assertEqual(b["content"].splitlines()[0], "[RELEASE] game unknown — owner2/repo2")
        self.assertIn("v2.0 published", b["content"])
        for payload in (a, b):
            self.assertEqual(payload["allowed_mentions"], {"parse": []})
            self.assertEqual(payload["flags"], 4)

    def test_more_than_the_cap_folds_into_one_overflow_message(self):
        n = self.d.MAX_INDIVIDUAL + 4
        f = feed([repo(i) for i in range(1, n + 1)], [event(i, "repository_discovered", i) for i in range(1, n + 1)])
        plan = self.d.plan(f, seen_state())
        kinds = [m["kind"] for m in plan["messages"]]
        self.assertEqual(kinds, ["repository_discovered"] * self.d.MAX_INDIVIDUAL + ["overflow"])
        self.assertEqual(len(plan["messages"][-1]["event_ids"]), 4)
        self.assertIn("4 more new repositories", plan["messages"][-1]["payload"]["content"])
        # oldest first: the individual messages are the oldest events
        self.assertEqual(plan["messages"][0]["event_ids"], ["repo-1:repository_discovered:1"])

    def test_commit_events_become_one_digest_with_a_cap(self):
        n = self.d.MAX_DIGEST_REPOSITORIES + 3
        events = [event(i, "commits_observed", i % 50) for i in range(1, n + 1)] + [event(1, "commits_observed", 59)]
        f = feed([repo(i) for i in range(1, n + 1)], events)
        digests = [m for m in self.d.plan(f, seen_state())["messages"] if m["kind"] == "commits_observed"]
        self.assertEqual(len(digests), 1)
        self.assertEqual(len(digests[0]["event_ids"]), n + 1)
        lines = digests[0]["payload"]["content"].splitlines()
        self.assertIn("Activity on %d repositories" % n, lines[0])
        self.assertEqual(sum(1 for line in lines if line.startswith("- ")), self.d.MAX_DIGEST_REPOSITORIES)
        self.assertIn("and 3 more", lines)

    def test_hostile_repository_text_cannot_ping_link_or_format(self):
        evil = "@everyone @here <@123> <#9> **bold** [x](http://evil.example/a) https://evil.example www.evil.example `code`\n\x00\x1b|spoiler|"
        f = feed([repo(1, repo=evil, game=evil, url="https://evil.example/x"), repo(2, url="javascript:alert(1)", game=evil)],
                 [event(1, "release_published", 1, summary=evil), event(2, "repository_discovered", 2)])
        for m in self.d.plan(f, seen_state())["messages"]:
            content = m["payload"]["content"]
            self.assertNotIn("@everyone", content)
            self.assertNotIn("@here", content)
            self.assertNotIn("<@", content)
            self.assertNotIn("<#", content)
            self.assertNotIn("evil.example", content)
            self.assertNotIn("](", content)
            self.assertNotIn("\x00", content)
            self.assertNotIn("\x1b", content)
            self.assertNotIn("Source:", content)
            self.assertEqual(m["payload"]["allowed_mentions"], {"parse": []})
        # the markdown characters that remain are backslash-escaped
        self.assertIn(r"\*\*bold\*\*", self.d.clean(evil, 200))

    def test_content_is_bounded_and_the_overflow_text_never_depends_on_repository_text(self):
        f = feed([repo(1, repo="x" * 5000, game="y" * 5000)], [event(1, "repository_discovered", 1)])
        content = self.d.plan(f, seen_state())["messages"][0]["payload"]["content"]
        self.assertLessEqual(len(content), self.d.MAX_CONTENT)

    def test_event_without_a_repository_record_still_plans(self):
        f = feed([], [event(9, "release_published", 1)])
        content = self.d.plan(f, seen_state())["messages"][0]["payload"]["content"]
        self.assertIn("unknown repository", content)
        self.assertNotIn("Source:", content)

    def test_malformed_feeds_plan_nothing(self):
        for bad in (None, [], "x", {}, {"events": "x", "repos": 3}, {"events": [None, 1, {"id": 5}, {"id": "bad id", "type": "x"}]}):
            plan = self.d.plan(bad, seen_state())
            self.assertEqual(plan["messages"], [])

    def test_same_input_same_plan(self):
        f = feed([repo(i) for i in range(1, 9)], [event(i, "repository_discovered", i) for i in range(1, 9)])
        s = seen_state()
        self.assertEqual(json.dumps(self.d.plan(f, s), sort_keys=True), json.dumps(self.d.plan(copy.deepcopy(f), copy.deepcopy(s)), sort_keys=True))


class DeliveryTests(CollectorTestCase):
    def setUp(self):
        super().setUp()
        from collector import discord

        self.d = discord
        self.f = feed([repo(1), repo(2)], [event(1, "repository_discovered", 1), event(2, "repository_discovered", 2)])

    def test_delivered_events_become_seen_and_a_rerun_plans_nothing(self):
        plan = self.d.plan(self.f, seen_state())
        state = self.d.apply_delivery(plan, [True, True])
        self.assertEqual(sorted(state["seen"]), sorted(e["id"] for e in self.f["events"]))
        self.assertEqual(self.d.plan(self.f, state)["messages"], [])

    def test_failed_events_are_retried_then_given_up_after_the_limit(self):
        state = seen_state()
        plan = self.d.plan(self.f, state)
        self.assertEqual(len(plan["messages"]), 2)
        state = self.d.apply_delivery(plan, [True, False])
        self.assertEqual(state["attempts"], {"repo-2:repository_discovered:2": 1})
        for attempt in range(2, self.d.MAX_ATTEMPTS):
            plan = self.d.plan(self.f, state)
            self.assertEqual(len(plan["messages"]), 1)
            state = self.d.apply_delivery(plan, [False])
            self.assertEqual(state["attempts"], {"repo-2:repository_discovered:2": attempt})
        # the failing event is on its last attempt: one more failure gives it up
        plan = self.d.plan(self.f, state)
        self.assertEqual([m["event_ids"] for m in plan["messages"]], [["repo-2:repository_discovered:2"]])
        state = self.d.apply_delivery(plan, [False])
        self.assertEqual(state["attempts"], {})
        self.assertIn("repo-2:repository_discovered:2", state["seen"])
        self.assertEqual(self.d.plan(self.f, state)["messages"], [])

    def test_success_clears_an_earlier_failure(self):
        state = self.d.apply_delivery(self.d.plan(self.f, seen_state()), [False, True])
        self.assertEqual(len(state["attempts"]), 1)
        state = self.d.apply_delivery(self.d.plan(self.f, state), [True])
        self.assertEqual(state["attempts"], {})

    def test_results_must_match_the_messages(self):
        with self.assertRaises(ValueError):
            self.d.apply_delivery(self.d.plan(self.f, seen_state()), [True])

    def test_seen_is_trimmed_to_the_newest_ids(self):
        ids = ["e%d" % i for i in range(self.d.MAX_SEEN)]
        f = feed([repo(1)], [event(1, "repository_discovered", 1)])
        plan = self.d.plan(f, seen_state(*ids))
        state = self.d.apply_delivery(plan, [True])
        self.assertEqual(len(state["seen"]), self.d.MAX_SEEN)
        self.assertEqual(state["seen"][-1], "repo-1:repository_discovered:1")
        self.assertNotIn("e0", state["seen"])


class StateTests(CollectorTestCase):
    def test_validation_refuses_bad_state_with_fixed_wording(self):
        from collector import discord

        good = discord.empty_state()
        self.assertIs(discord.validate_state(good), good)
        bad = [None, [], {}, dict(good, format_version=2), dict(good, seen="x"), dict(good, seen=["a", "a"]),
               dict(good, seen=["bad id"]), dict(good, attempts={"a": 0}), dict(good, attempts={"a": discord.MAX_ATTEMPTS}),
               dict(good, attempts={"a": True}), dict(good, extra=1), dict(good, seen=["e%d" % i for i in range(discord.MAX_SEEN + 1)])]
        for state in bad:
            with self.subTest(state=str(state)[:40]), self.assertRaises(discord.StateError) as ctx:
                discord.validate_state(state)
            self.assertNotIn("bad id", str(ctx.exception))


class ModuleBoundaryTests(CollectorTestCase):
    def test_the_module_has_no_network_clock_or_process_imports(self):
        with open(os.path.join(REPO_ROOT, "collector", "discord.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0] if node.level == 0 else "relative")
        self.assertEqual(names, {"re", "relative"})


class CliTests(CollectorTestCase):
    def run_cli(self, argv):
        from collector import cli

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_feed_outside_data_is_refused(self):
        code, out, err = self.run_cli(["discord", "--feed", os.path.join(REPO_ROOT, "config", "sources.json")])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("refused", err)

    def test_a_missing_feed_flag_is_refused(self):
        code, _, _ = self.run_cli(["discord"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

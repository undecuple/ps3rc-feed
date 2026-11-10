"""Regression tests for the review findings (F1-F13) on the step 1 code.

Every scenario here is built from in-memory run dicts, so each test
states exactly the input that used to go wrong. Numbers in the test names
refer to the findings list in .local/STATUS.md.
"""

import copy
import json
import os
import stat
import tempfile
import time
import unittest.mock

from helpers import CollectorTestCase, canonical, run_runs


def T(day, hour=0, month=11):
    return "2026-%02d-%02dT%02d:00:00Z" % (month, day, hour)


def sha(n):
    return "%040x" % n


def repo(rid=100, name="game-org/skyfall-recomp", fetch="ok", commits=(), releases=(), archived=False, stars=5,
         readme="# Skyfall\n", desc="Recomp of Skyfall (fictional)", complete=True, **extra):
    data = {
        "repository_id": rid,
        "full_name": name,
        "owner": name.split("/")[0],
        "description": desc,
        "readme_excerpt": readme,
        "is_fork": False,
        "parent_repository_id": None,
        "network_root_repository_id": None,
        "archived": archived,
        "stars": stars,
        "default_branch": "main",
        "fetch_state": fetch,
        "commits_complete": complete,
        "commits": None if commits is None else [{"sha": sha(n), "committed_at": t, "message": "m"} for n, t in commits],
        "releases": None if releases is None else [{"id": i, "tag": tg, "published_at": T(1)} for i, tg in releases],
    }
    data.update(extra)
    return data


def failing(rid=100, name="game-org/skyfall-recomp", fetch="rate_limited"):
    return repo(rid=rid, name=name, fetch=fetch, commits=None, releases=None, stars=None, archived=None)


def run(at, repos, searches=None, roots=None):
    return {"run_at": at, "roots": roots or [], "searches": searches or [], "repositories": repos}


class F1LinearParsingAndLoadBounds(CollectorTestCase):
    def test_readme_heading_parse_is_linear(self):
        from collector import text

        hostile = "# a" + " " * 4000 + "x\x00"
        started = time.monotonic()
        title = text.readme_title(hostile)
        self.assertLess(time.monotonic() - started, 1.0)
        # Heading lines are capped at MAX_HEADING_LINE before parsing.
        self.assertEqual(title, "a")
        self.assertEqual(text.readme_title("# a" + " " * 500 + "x\x00"), "a x")
        self.assertEqual(text.heading_text("## Title ##"), "Title")
        self.assertEqual(text.heading_text("   # Deep"), "Deep")
        self.assertIsNone(text.heading_text("    # code block"))
        self.assertIsNone(text.heading_text("#NoSpace"))
        self.assertIsNone(text.heading_text("### level three"))

    def test_free_text_is_bounded_at_load(self):
        from collector import schema

        r = repo(desc="d" * 10000, readme="# T\n" + "r" * 100000, commits=[(1, T(1))], releases=[(1, "v" * 1000)])
        r["commits"][0]["message"] = "m" * 10000
        data = run(T(5), [r])
        schema.validate_run(data)
        got = data["repositories"][0]
        self.assertEqual(len(got["description"]), schema.MAX_DESCRIPTION)
        self.assertEqual(len(got["readme_excerpt"]), schema.MAX_README)
        self.assertEqual(len(got["commits"][0]["message"]), schema.MAX_MESSAGE)
        self.assertEqual(len(got["releases"][0]["tag"]), schema.MAX_TAG)
        too_many = repo(commits=[(n, T(1)) for n in range(1, schema.MAX_COMMITS + 2)])
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(run(T(5), [too_many]))

    def test_event_sha_lists_are_bounded_but_the_id_covers_every_sha(self):
        from collector import model

        many = [(n, T(2)) for n in range(1, 151)]
        state, events = self.assertDeterministic([run(T(1), [repo(commits=[(0, T(1))])]), run(T(2), [repo(commits=[(0, T(1))] + many)])])
        e = events[1][0]
        self.assertEqual(len(e["details"]["commit_shas"]), model.MAX_EVENT_SHAS)
        self.assertEqual(len(e["details"]["commit_urls"]), model.MAX_EVENT_SHAS)
        self.assertIs(e["details"]["commit_shas_truncated"], True)
        self.assertEqual(e["details"]["commit_count"], 150)
        self.assertEqual(e["event_id"], model.commits_event_id(100, [sha(n) for n, _ in many]))


class F2BaselinePerRepository(CollectorTestCase):
    HISTORY = [(n, T(1)) for n in range(1, 201)]
    RELEASES = [(i, "v%d" % i) for i in range(1, 51)]

    def test_first_sighting_failed_then_success_does_not_flood(self):
        state, events = self.assertDeterministic([
            run(T(5), [failing()]),
            run(T(6), [repo(commits=self.HISTORY, releases=self.RELEASES)]),
            run(T(7), [repo(commits=self.HISTORY + [(999, T(7))], releases=self.RELEASES + [(99, "v99")])]),
        ])
        self.assertEqual(events[1], [])
        obs = state["repositories"]["100"]
        self.assertIs(obs["commits_baseline_done"], True)
        self.assertEqual(len(obs["observed_commits"]), 201)
        self.assertEqual(sorted(self.types(events[2])), ["commits_observed", "release_published"])
        commits = [e for e in events[2] if e["type"] == "commits_observed"][0]
        self.assertEqual(commits["details"]["commit_shas"], [sha(999)])

    def test_discovered_while_failing_then_success_does_not_flood(self):
        state, events = self.assertDeterministic([
            run(T(5), [repo(rid=1, name="a/a")]),
            run(T(6), [repo(rid=1, name="a/a"), failing()]),
            run(T(7), [repo(rid=1, name="a/a"), repo(commits=self.HISTORY[:30], releases=[(9, "v9")])]),
            run(T(8), [repo(rid=1, name="a/a"), repo(commits=self.HISTORY[:31], releases=[(9, "v9")])]),
        ])
        self.assertEqual(self.types(events[1]), ["repository_discovered"])
        self.assertEqual(events[2], [])
        self.assertEqual(self.types(events[3]), ["commits_observed"])
        self.assertEqual(events[3][0]["details"]["commit_shas"], [sha(31)])

    def test_empty_first_run_does_not_make_the_next_run_a_discovery_flood(self):
        state, events = self.assertDeterministic([
            run(T(5), []),
            run(T(6), [repo(rid=i, name="o/r%d" % i) for i in range(1, 101)]),
            run(T(7), [repo(rid=i, name="o/r%d" % i) for i in range(1, 102)]),
        ])
        self.assertEqual(events[0], [])
        self.assertEqual(events[1], [])
        self.assertIs(state["baseline_done"], True)
        self.assertEqual(state["baseline_at"], T(6))
        self.assertEqual(self.types(events[2]), ["repository_discovered"])
        self.assertEqual(events[2][0]["repository_id"], 101)


class F3AnchoredPatterns(CollectorTestCase):
    def test_trailing_newline_is_rejected_everywhere(self):
        from collector import schema

        base = run(T(5), [repo(commits=[(1, T(1))])])
        for mutate in (
            lambda r: r["repositories"][0].__setitem__("full_name", "o/r\n"),
            lambda r: r["repositories"][0].__setitem__("owner", "game-org\n"),
            lambda r: r["repositories"][0]["commits"][0].__setitem__("sha", sha(1) + "\n"),
            lambda r: r["repositories"][0]["commits"][0].__setitem__("committed_at", T(1) + "\n"),
            lambda r: r.__setitem__("run_at", T(5) + "\n"),
        ):
            bad = copy.deepcopy(base)
            mutate(bad)
            with self.assertRaises(schema.SchemaError):
                schema.validate_run(bad)
        state, events = run_runs([run(T(5), [repo(commits=[(1, T(1))])]), run(T(6), [repo(commits=[(1, T(1)), (2, T(6))])])])
        event = copy.deepcopy(events[1][0])
        event["event_id"] += "\n"
        with self.assertRaises(schema.SchemaError):
            schema.validate_event(event)
        event = copy.deepcopy(events[1][0])
        event["source_url"] += "\n"
        with self.assertRaises(schema.SchemaError):
            schema.validate_event(event)


class F4RealTimestampsAndHugeIntegers(CollectorTestCase):
    def test_invalid_calendar_date_is_a_schema_error(self):
        from collector import schema

        bad = run(T(5), [repo(commits=[(1, "2026-13-45T99:99:99Z")])])
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(bad)
        bad = run("2026-02-30T00:00:00Z", [])
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(bad)

    def test_huge_integers_are_schema_errors_not_value_errors(self):
        from collector import schema

        base = json.dumps(run(T(5), [repo()]))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.json")
            for digits in (20, 4000, 5000):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(base.replace('"stars": 5', '"stars": ' + "9" * digits))
                with self.assertRaises(schema.SchemaError, msg=digits):
                    schema.load_run_file(path)
        bad = run(T(5), [repo(rid=2**53 + 1)])
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(bad)


class F5AtomicApplyAndIdRanges(CollectorTestCase):
    def test_non_positive_and_oversized_ids_are_rejected(self):
        from collector import schema

        for rid in (0, -1, 2**53 + 1):
            with self.assertRaises(schema.SchemaError, msg=rid):
                schema.validate_run(run(T(5), [repo(rid=rid, name="n/n")]))
        for rel_id in (0, -5):
            with self.assertRaises(schema.SchemaError, msg=rel_id):
                schema.validate_run(run(T(5), [repo(releases=[(rel_id, "x")])]))
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(run(T(5), [repo(stars=True)]))
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(run(T(5), [repo(stars=-1)]))

    def test_failed_apply_leaves_state_untouched(self):
        from collector import events, schema

        state, _ = run_runs([run(T(5), [repo(commits=[(1, T(1))])])])
        before = canonical(state)
        later = run(T(6), [repo(commits=[(1, T(1)), (2, T(6))])])
        with unittest.mock.patch.object(schema, "validate_state", side_effect=schema.SchemaError("boom")):
            with self.assertRaises(schema.SchemaError):
                events.apply_run(state, copy.deepcopy(later), events.Config())
        self.assertEqual(canonical(state), before)
        self.assertNotIn(sha(2), state["repositories"]["100"]["observed_commits"])
        # Retrying then still reports the commit: nothing was silently lost.
        emitted = events.apply_run(state, later, events.Config())
        self.assertEqual(emitted[0]["details"]["commit_shas"], [sha(2)])


class F6FixtureRootHardening(CollectorTestCase):
    def test_symlinks_special_files_and_linked_entries_are_refused(self):
        from collector import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "fixtures")
            os.mkdir(root)
            scenario = os.path.join(root, "scenario")
            os.mkdir(scenario)
            good = os.path.join(scenario, "01-run.json")
            with open(good, "w", encoding="utf-8") as fh:
                json.dump(run(T(5), [repo()]), fh)
            # A symlink inside the root that points inside the root.
            inner_link = os.path.join(root, "alias")
            os.symlink(scenario, inner_link)
            with self.assertRaises(cli.RefusedInput):
                cli.resolve_fixture_path(inner_link, root=root)
            with self.assertRaises(cli.RefusedInput):
                cli.resolve_fixture_path(os.path.join(inner_link, "01-run.json"), root=root)
            # A directory entry that is a symlink (even to a file inside the root).
            os.symlink(good, os.path.join(scenario, "02-link.json"))
            with self.assertRaises(cli.RefusedInput):
                cli.collect_run_files([scenario], root=root)
            os.unlink(os.path.join(scenario, "02-link.json"))
            # A directory entry that links outside the root.
            outside = os.path.join(tmp, "outside.json")
            with open(outside, "w", encoding="utf-8") as fh:
                fh.write("{}")
            os.symlink(outside, os.path.join(scenario, "02-escape.json"))
            with self.assertRaises(cli.RefusedInput):
                cli.collect_run_files([scenario], root=root)
            os.unlink(os.path.join(scenario, "02-escape.json"))
            # A FIFO.
            fifo = os.path.join(scenario, "03-fifo.json")
            os.mkfifo(fifo)
            with self.assertRaises(cli.RefusedInput):
                cli.collect_run_files([scenario], root=root)
            with self.assertRaises(cli.RefusedInput):
                cli.resolve_fixture_path(fifo, root=root)
            os.unlink(fifo)
            # The clean directory is accepted.
            self.assertEqual(cli.collect_run_files([scenario], root=root), [good])


class F7HistoryRewriteAndHead(CollectorTestCase):
    def test_rebase_with_fresh_dates_is_flagged_from_reachability(self):
        from collector import catalog

        state, events = self.assertDeterministic([
            run(T(5), [repo(commits=[(1, T(1)), (2, T(2))])]),
            run(T(6), [repo(commits=[(3, T(3)), (4, T(4)), (5, T(5))])]),
        ])
        e = events[1][0]
        self.assertIs(e["details"]["history_rewrite_suspected"], True)
        self.assertIsNone(e["details"]["commit_count"])
        self.assertIs(e["details"]["count_exact"], False)
        self.assertIs(state["repositories"]["100"]["history_rewrite_suspected"], True)
        self.assertIs(catalog.build_catalog(state)[0]["commits_30d_complete"], False)

    def test_incomplete_window_without_old_head_is_inexact_not_rewrite_and_flag_clears(self):
        from collector import catalog

        state, events = self.assertDeterministic([
            run(T(5), [repo(commits=[(1, T(1)), (2, T(4))])]),
            run(T(6), [repo(commits=[(9, T(3)), (3, T(5))], complete=False)]),
            run(T(7), [repo(commits=[(1, T(1)), (9, T(3)), (2, T(4)), (3, T(5))])]),
            run(T(8), [repo(commits=[(1, T(1)), (9, T(3)), (2, T(4)), (3, T(5)), (10, T(8))])]),
        ])
        e = events[1][0]
        self.assertIs(e["details"]["history_rewrite_suspected"], False)
        self.assertIs(e["details"]["count_exact"], False)
        self.assertIn("at least", e["summary"])
        # Run 3 is a complete listing that still reaches the previous head: clean.
        self.assertIs(state["repositories"]["100"]["history_rewrite_suspected"], False)
        self.assertIs(events[3][0]["details"]["count_exact"], True)
        self.assertIs(catalog.build_catalog(state)[0]["commits_30d_complete"], True)

    def test_rewrite_flag_clears_after_a_later_clean_complete_run(self):
        state, events = self.assertDeterministic([
            run(T(5), [repo(commits=[(1, T(1)), (2, T(2))])]),
            run(T(6), [repo(commits=[(3, T(3))])]),
            run(T(7), [repo(commits=[(3, T(3)), (4, T(7))])]),
        ])
        self.assertIs(events[1][0]["details"]["history_rewrite_suspected"], True)
        self.assertIs(state["repositories"]["100"]["history_rewrite_suspected"], False)
        self.assertIs(events[2][0]["details"]["count_exact"], True)

    def test_head_is_the_listed_head_not_the_latest_date_and_future_dates_are_clamped(self):
        from collector import catalog

        state, events = self.assertDeterministic([
            run(T(5), [repo(commits=[(1, T(1))])]),
            run(T(6), [repo(commits=[(1, T(1)), (2, "2099-01-01T00:00:00Z"), (3, T(6))])]),
        ])
        obs = state["repositories"]["100"]
        self.assertEqual(obs["tip_sha"], sha(3))
        e = events[1][0]
        self.assertEqual(e["source_at"], T(6))
        self.assertIs(e["details"]["future_dated"], True)
        self.assertLessEqual(catalog.build_catalog(state)[0]["last_commit_at"], T(6))
        # The data keeps the reported date; only derived fields are clamped.
        self.assertEqual(obs["observed_commits"][sha(2)]["committed_at"], "2099-01-01T00:00:00Z")
        # An explicit head wins over list position and is clamped too.
        state2, _ = self.assertDeterministic([
            run(T(5), [repo(commits=[(1, T(1))])]),
            run(T(6), [repo(commits=[(1, T(1)), (3, T(6)), (2, "2099-01-01T00:00:00Z")], head_sha=sha(2))]),
        ])
        self.assertEqual(state2["repositories"]["100"]["tip_sha"], sha(2))
        self.assertEqual(state2["repositories"]["100"]["tip_committed_at"], T(6))


class F8NullsOnSuccess(CollectorTestCase):
    def test_null_fields_on_a_successful_fetch_keep_previous_values(self):
        state, events = self.assertDeterministic([
            run(T(5), [repo(commits=[(1, T(1))], stars=7)]),
            run(T(6), [repo(commits=[(1, T(1))], desc=None, readme=None, stars=None, archived=None, is_fork=None)]),
            run(T(7), [repo(commits=[(1, T(1))], stars=7)]),
        ])
        self.assertEqual(events[1], [])
        self.assertEqual(events[2], [])
        obs = state["repositories"]["100"]
        self.assertEqual(obs["description"], "Recomp of Skyfall (fictional)")
        self.assertEqual(obs["readme_excerpt"], "# Skyfall\n")
        self.assertEqual(obs["game"]["key"], "skyfall")
        self.assertEqual(obs["stars"], 7)
        self.assertIs(obs["archived"], False)
        self.assertIs(obs["is_fork"], False)
        self.assertNotIn("game_association_changed", [e["type"] for e in state["events"]])
        # A known fork whose parent comes back null keeps its ancestry.
        fork, _ = run_runs([
            run(T(5), [repo(commits=[(1, T(1))], is_fork=True, parent_repository_id=90, network_root_repository_id=90)]),
            run(T(6), [repo(commits=[(1, T(1))], is_fork=True)]),
        ])
        self.assertEqual(fork["repositories"]["100"]["parent_repository_id"], 90)
        self.assertIs(fork["repositories"]["100"]["partial"], True)
        # Partial was flagged for the null run and cleared by the full one.
        mid, _ = run_runs([run(T(5), [repo(commits=[(1, T(1))], stars=7)]), run(T(6), [repo(commits=[(1, T(1))], stars=None, archived=None)])])
        self.assertIs(mid["repositories"]["100"]["partial"], True)
        self.assertEqual(mid["repositories"]["100"]["stars"], 7)
        self.assertIs(mid["repositories"]["100"]["archived"], False)
        self.assertIs(obs["partial"], False)

    def test_success_that_refreshes_nothing_is_not_a_recovery(self):
        from collector import catalog

        runs = [run(T(5), [repo(commits=[(1, T(1))])])] + [run(T(5 + i), [failing()]) for i in (1, 2, 3)]
        runs.append(run(T(9), [repo(commits=None, releases=None, stars=None, archived=None)]))
        state, events = self.assertDeterministic(runs)
        self.assertEqual(self.types(events[3]), ["collection_stale"])
        self.assertEqual(events[4], [])
        obs = state["repositories"]["100"]
        self.assertIs(obs["stale"], True)
        self.assertEqual(obs["last_successful_refresh_at"], T(5))
        self.assertEqual(obs["stars"], 5)
        entry = catalog.build_catalog(state)[0]
        self.assertIs(entry["stale"], True)
        self.assertEqual(entry["stars"], 5)
        # A real refresh afterwards recovers exactly once.
        state, events = self.assertDeterministic(runs + [run(T(10), [repo(commits=[(1, T(1))])])])
        self.assertEqual(self.types(events[5]), ["collection_recovered"])


class F9StaleMetricsArePreserved(CollectorTestCase):
    def test_commits_30d_of_a_stale_entry_is_the_preserved_value(self):
        from collector import catalog

        state, _ = self.assertDeterministic([
            run(T(1), [repo(commits=[(1, T(1)), (2, T(1))])]),
            run("2026-12-15T00:00:00Z", [failing()]),
        ])
        entry = catalog.build_catalog(state)[0]
        self.assertIs(entry["stale"], True)
        self.assertEqual(entry["commits_30d"], 2)
        self.assertIs(entry["commits_30d_complete"], False)
        # Never successful: unknown, not zero.
        state, _ = self.assertDeterministic([run(T(1), [repo(rid=1, name="a/a")]), run(T(2), [repo(rid=1, name="a/a"), failing()])])
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 100][0]
        self.assertIsNone(entry["commits_30d"])
        self.assertIsNone(entry["commits_30d_complete"])

    def test_never_successful_repository_is_failing_not_stale(self):
        runs = [run(T(1), [repo(rid=1, name="a/a")])] + [run(T(1 + i), [repo(rid=1, name="a/a"), failing()]) for i in (1, 2, 3, 4)]
        state, events = self.assertDeterministic(runs)
        obs = state["repositories"]["100"]
        self.assertIs(obs["stale"], False)
        self.assertEqual(obs["collection_state"], "failing")
        stale_events = [e for run_events in events for e in run_events if e["type"] == "collection_stale"]
        self.assertEqual(len(stale_events), 1)
        self.assertIs(stale_events[0]["details"]["known_values_stale"], False)


class F10TransitionIdsSurviveStateLoss(CollectorTestCase):
    def test_ids_differ_across_different_runs_and_duplicates_are_rejected(self):
        from collector import events, schema

        _, ev1 = run_runs([run(T(5), [repo(archived=False)]), run(T(6), [repo(archived=True)])])
        _, ev2 = run_runs([run(T(9), [repo(archived=False)]), run(T(10), [repo(archived=True)])])
        self.assertEqual(ev1[1][0]["event_id"], "repo-100:archived:20261106000000")
        self.assertNotEqual(ev1[1][0]["event_id"], ev2[1][0]["event_id"])
        state, _ = run_runs([run(T(5), [repo(archived=False)]), run(T(6), [repo(archived=True)])])
        state["events"].append(copy.deepcopy(state["events"][0]))
        with self.assertRaises(schema.SchemaError):
            schema.validate_state(state)
        with self.assertRaises(schema.SchemaError):
            schema.load_events(state["events"])

    def test_older_run_is_rejected_and_same_run_is_a_no_op(self):
        from collector import events

        r1 = run(T(5), [repo(archived=False)])
        r2 = run(T(6), [repo(archived=True, name="new-owner/r")])
        state, emitted = run_runs([r1, r2])
        self.assertEqual(self.types(emitted[1]), ["repository_archived"])
        before = canonical(state)
        with self.assertRaises(events.RunOrderError):
            events.apply_run(state, copy.deepcopy(r1), events.Config())
        self.assertEqual(canonical(state), before)
        self.assertEqual(events.apply_run(state, copy.deepcopy(r2), events.Config()), [])
        self.assertEqual([h["full_name"] for h in state["repositories"]["100"]["name_history"]], ["game-org/skyfall-recomp"])


class F11StrictLoader(CollectorTestCase):
    def _write(self, tmp, body):
        path = os.path.join(tmp, "run.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_duplicate_keys_nan_floats_and_bool_ints_are_rejected(self):
        from collector import schema

        base = json.dumps(run(T(5), [repo()]))
        with tempfile.TemporaryDirectory() as tmp:
            # The reader itself rejects these, before any schema check: the
            # duplicate's last value is valid, and NaN/floats sit in a key
            # no schema looks at.
            for body in (
                '{"a": 1, "a": 2}',
                '{"a": NaN}',
                '{"a": Infinity}',
                '{"a": -Infinity}',
                '{"a": 1.5}',
                '{"a": 1e3}',
            ):
                with self.assertRaises(schema.SchemaError, msg=body):
                    schema.read_json(self._write(tmp, body))
            self.assertEqual(schema.read_json(self._write(tmp, '{"a": [1, {"b": null}]}')), {"a": [1, {"b": None}]})
            for body in (
                base.replace('"stars": 5', '"stars": 5, "stars": 6'),
                base.replace('"stars": 5', '"stars": NaN'),
                base.replace('"stars": 5', '"stars": 5.0'),
                base.replace('"stars": 5', '"stars": true'),
                base.replace('"stars": 5', '"stars": -1'),
                base.replace('"repository_id": 100', '"repository_id": 9007199254740993'),
            ):
                with self.assertRaises(schema.SchemaError, msg=body[:80]):
                    schema.load_run_file(self._write(tmp, body))
            schema.load_run_file(self._write(tmp, base))

    def test_loaded_events_must_already_be_escaped_and_bounded(self):
        from collector import schema

        state, _ = run_runs([run(T(5), [repo(commits=[(1, T(1))])]), run(T(6), [repo(commits=[(1, T(1)), (2, T(6))])])])
        good = state["events"][0]
        schema.load_events([copy.deepcopy(good)])
        for field, value in (
            ("summary", "<script>alert(1)</script>"),
            ("summary", "refresh @everyone"),
            ("summary", "refresh @Here now"),
            ("title", "<img src=x onerror=alert(1)>"),
            ("title", 'say "hi"'),
            ("title", "x" * (schema.MAX_RENDERED + 1)),
            ("game_title", "a‮b"),
            ("game_title", "see https://invalid.example/x"),
        ):
            bad = copy.deepcopy(good)
            bad[field] = value
            with self.assertRaises(schema.SchemaError, msg=(field, value[:30])):
                schema.load_events([bad])
        for details in (
            {"url": "javascript:alert(1)"},
            {"url": "https://invalid.example/x"},
            {"n": float("nan")},
            {"n": 1.5},
            {"raw": "<b>"},
            {"raw": "@everyone"},
            {"deep": {"a": {"b": {"c": {"d": 1}}}}},
            {"Bad-Key": 1},
        ):
            bad = copy.deepcopy(good)
            bad["details"] = details
            with self.assertRaises(schema.SchemaError, msg=details):
                schema.load_events([bad])
            with self.assertRaises(schema.SchemaError, msg=details):
                st = copy.deepcopy(state)
                st["events"][0] = bad
                schema.validate_state(st)

    def test_state_counters_and_keys_are_checked(self):
        from collector import schema

        state, _ = run_runs([run(T(5), [repo(commits=[(1, T(1))])])])
        bad = copy.deepcopy(state)
        bad["repositories"]["100"]["consecutive_failures"] = -50
        with self.assertRaises(schema.SchemaError):
            schema.validate_state(bad)
        bad = copy.deepcopy(state)
        bad["repositories"]["100"]["observed_commits"][sha(1) + "\n"] = {"committed_at": T(1), "message": "", "first_observed_at": T(1)}
        with self.assertRaises(schema.SchemaError):
            schema.validate_state(bad)
        bad = copy.deepcopy(state)
        bad["repositories"]["100"]["owner"] = "someone-else"
        with self.assertRaises(schema.SchemaError):
            schema.validate_state(bad)
        bad = copy.deepcopy(state)
        bad["repositories"]["100"]["tip_sha"] = sha(77)
        with self.assertRaises(schema.SchemaError):
            schema.validate_state(bad)


class F12EscapeText(CollectorTestCase):
    def test_bound_is_applied_after_escaping_on_whole_entities(self):
        from collector import text

        out = text.escape_text("<" * 5000)
        self.assertLessEqual(len(out), text.MAX_TEXT)
        self.assertTrue(out.endswith("..."))
        body = out[:-3]
        self.assertEqual(body, "&lt;" * (len(body) // 4))
        out = text.escape_text("a" * 296 + "<<<<")
        self.assertLessEqual(len(out), text.MAX_TEXT)
        self.assertNotRegex(out, r"&[a-z#0-9]*\.\.\.$")
        self.assertEqual(text.escape_text("\"'"), "&quot;&#x27;")

    def test_mentions_are_neutralised_after_stripping_and_in_every_form(self):
        from collector import text

        self.assertEqual(text.escape_text("@​everyone"), "(at)everyone")
        self.assertEqual(text.escape_text("@‍‌here"), "(at)here")
        for raw in ("@Everyone", "@everyoneé", "@everyone_now", "@here2", "@HERE", "＠everyone"):
            out = text.escape_text(raw)
            self.assertTrue(out.startswith("(at)"), (raw, out))
            self.assertIsNone(__import__("re").search(r"(?i)@(everyone|here)", out), out)
        for raw, expected in (("<@123>", "&lt;(ref)@123&gt;"), ("<@!1>", "&lt;(ref)@!1&gt;"), ("<@&1>", "&lt;(ref)@&amp;1&gt;"), ("<#1>", "&lt;(ref)#1&gt;")):
            self.assertEqual(text.escape_text(raw), expected)

    def test_separators_fillers_and_combining_floods_are_stripped(self):
        from collector import text

        self.assertEqual(text.escape_text("a b c"), "abc")
        self.assertEqual(text.escape_text("xㅤy"), "xy")
        self.assertEqual(text.escape_text("xᅟᅠﾠy"), "xy")
        self.assertEqual(text.escape_text("‮gnp.exe"), "gnp.exe")
        flooded = "e" + "́" * 50
        self.assertLessEqual(len(text.escape_text(flooded)), 1 + text.MAX_COMBINING_RUN)


class F13SmallerFindings(CollectorTestCase):
    def test_owner_must_match_full_name_and_dot_parts_are_rejected(self):
        from collector import schema

        bad = run(T(5), [repo(name="victim/real-port")])
        bad["repositories"][0]["owner"] = "someone-else"
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(bad)
        for name in ("o/..", "./x", "../x", "o/.", "o/r/x", "/r", "o/"):
            with self.assertRaises(schema.SchemaError, msg=name):
                schema.validate_run(run(T(5), [repo(name=name)]))

    def test_release_url_is_percent_encoded_only(self):
        from collector import text

        self.assertEqual(text.release_url("o/r", "a/b"), "https://github.com/o/r/releases/tag/a%2Fb")
        self.assertEqual(text.release_url("o/r", "v1@2"), "https://github.com/o/r/releases/tag/v1%402")
        self.assertEqual(text.release_url("o/r", "rel@everyone"), "https://github.com/o/r/releases/tag/rel%40everyone")
        self.assertEqual(text.release_url("o/r", "v1.0 beta"), "https://github.com/o/r/releases/tag/v1.0%20beta")
        for tag in (".", "..", "", "​"):
            self.assertEqual(text.release_url("o/r", tag), "https://github.com/o/r/releases")
        self.assertEqual(text.release_url("o/r", "a​b"), "https://github.com/o/r/releases/tag/ab")

    def test_incomplete_search_stays_visible_until_rerun_complete(self):
        from collector import catalog

        incomplete = [{"query": "q", "complete": False, "repository_ids": []}]
        state, _ = self.assertDeterministic([run(T(5), [repo()], searches=incomplete), run(T(6), [repo()], searches=[])])
        status = catalog.collection_status(state)
        self.assertIs(status["incomplete"], True)
        self.assertEqual(status["incomplete_searches"], ["q"])
        self.assertIs(state["collection"]["incomplete"], True)
        complete = [{"query": "q", "complete": True, "repository_ids": []}]
        state, _ = self.assertDeterministic([run(T(5), [repo()], searches=incomplete), run(T(6), [repo()], searches=complete)])
        self.assertIs(catalog.collection_status(state)["incomplete"], False)

    def test_discovery_summary_does_not_depend_on_search_order(self):
        searches = [{"query": "a", "complete": True, "repository_ids": [5]}, {"query": "b", "complete": True, "repository_ids": [5]}]
        summaries = []
        for order in (searches, list(reversed(searches))):
            _, events = run_runs([run(T(5), [repo(rid=1, name="a/a")]), run(T(6), [repo(rid=1, name="a/a"), repo(rid=5, name="x/y")], searches=order)])
            summaries.append(events[1][0]["summary"])
            self.assertEqual(events[1][0]["details"]["discovery_sources"], ["search:a", "search:b"])
        self.assertEqual(summaries[0], summaries[1])

    def test_save_state_is_atomic_and_refuses_symlinks(self):
        from collector import store

        state, _ = run_runs([run(T(5), [repo(commits=[(1, T(1))])])])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            store.save_state(state, path)
            self.assertEqual(sorted(os.listdir(tmp)), ["state.json"])
            self.assertEqual(canonical(store.load_state(path)), canonical(state))
            self.assertTrue(stat.S_ISREG(os.lstat(path).st_mode))
            link = os.path.join(tmp, "link.json")
            os.symlink(path, link)
            with self.assertRaises(OSError):
                store.save_state(state, link)
            self.assertEqual(sorted(os.listdir(tmp)), ["link.json", "state.json"])
            # A broken write leaves no temp file behind.
            with unittest.mock.patch.object(os, "replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    store.save_state(state, path)
            self.assertEqual(sorted(os.listdir(tmp)), ["link.json", "state.json"])

    def test_package_disables_bytecode_writing(self):
        import sys

        import collector  # noqa: F401

        self.assertTrue(sys.dont_write_bytecode)

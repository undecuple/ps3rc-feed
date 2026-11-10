"""Dry-run CLI: fixture-only paths, no URLs, read-only, prints what it would publish."""

import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout

from helpers import FIXTURES, CollectorTestCase

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_cli(argv):
    from collector import cli

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class DryRunPathTests(CollectorTestCase):
    def test_refuses_paths_outside_fixtures(self):
        from collector import cli

        for bad in (
            "/etc/hosts",
            os.path.join(REPO_ROOT, "README.md"),
            os.path.join(FIXTURES, "..", "..", "README.md"),
            os.path.join(FIXTURES, "..", "helpers.py"),
            "tests",
            os.path.join(REPO_ROOT, "tests", "fixtures", "..", "fixtures", "..", "test_cli.py"),
        ):
            with self.assertRaises(cli.RefusedInput, msg=bad):
                cli.resolve_fixture_path(bad)
            code, out, err = _run_cli(["dry-run", bad])
            self.assertEqual(code, 2, bad)
            self.assertEqual(out, "")
            self.assertIn("refused", err)

    def test_refuses_symlink_escaping_the_fixture_root(self):
        from collector import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "fixtures")
            os.mkdir(root)
            escape = os.path.join(root, "escape")
            os.symlink(tmp, escape)
            with self.assertRaises(cli.RefusedInput):
                cli.resolve_fixture_path(os.path.join(escape, "anything.json"), root=root)
            with self.assertRaises(cli.RefusedInput):
                cli.resolve_fixture_path(escape, root=root)
            inside = os.path.join(root, "ok")
            os.mkdir(inside)
            self.assertEqual(cli.resolve_fixture_path(inside, root=root), os.path.realpath(inside))

    def test_refuses_urls(self):
        from collector import cli

        for bad in (
            "https://github.com/example/example",
            "http://invalid.example/run.json",
            "file:///etc/hosts",
            "ftp://invalid.example/x",
            "javascript:alert(1)",
            "data:text/plain,hello",
        ):
            with self.assertRaises(cli.RefusedInput, msg=bad):
                cli.resolve_fixture_path(bad)
            code, out, err = _run_cli(["dry-run", bad])
            self.assertEqual(code, 2, bad)
            self.assertEqual(out, "")
            self.assertIn("refused", err)

    def test_accepts_relative_fixture_paths(self):
        from collector import cli

        resolved = cli.resolve_fixture_path(os.path.join("tests", "fixtures", "baseline_unchanged"))
        self.assertEqual(resolved, os.path.realpath(os.path.join(FIXTURES, "baseline_unchanged")))


class DryRunOutputTests(CollectorTestCase):
    def test_scenario_directory_prints_publishable_json(self):
        code, out, err = _run_cli(["dry-run", os.path.join(FIXTURES, "new_activity")])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIs(payload["dry_run"], True)
        self.assertEqual(len(payload["runs"]), 4)
        self.assertEqual(payload["runs"][0]["events"], [])
        self.assertEqual([e["type"] for e in payload["runs"][3]["events"]], ["repository_discovered"])
        self.assertEqual(len(payload["events"]), 4)
        self.assertEqual(len(payload["catalog"]), 3)
        self.assertIn("collection_status", payload)
        self.assertIn("incomplete", payload["collection_status"])
        self.assertIn("inputs", payload)
        self.assertTrue(all(p.startswith("tests/fixtures/") for p in payload["inputs"]))

    def test_individual_files_in_order_and_threshold_option(self):
        files = [os.path.join(FIXTURES, "failures", f) for f in sorted(os.listdir(os.path.join(FIXTURES, "failures")))]
        code, out, err = _run_cli(["dry-run", "--stale-threshold", "2"] + files[:4])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([e["type"] for e in payload["runs"][3]["events"]], ["collection_stale"])

    def test_dry_run_writes_nothing(self):
        before = {}
        for dirpath, _, files in os.walk(REPO_ROOT):
            if "/.git" in dirpath or "__pycache__" in dirpath:
                continue
            for f in files:
                p = os.path.join(dirpath, f)
                before[p] = os.stat(p).st_mtime_ns
        code, _, _ = _run_cli(["dry-run", os.path.join(FIXTURES, "hostile_text")])
        self.assertEqual(code, 0)
        after = {}
        for dirpath, _, files in os.walk(REPO_ROOT):
            if "/.git" in dirpath or "__pycache__" in dirpath:
                continue
            for f in files:
                p = os.path.join(dirpath, f)
                after[p] = os.stat(p).st_mtime_ns
        self.assertEqual(before, after)

    def test_every_step1_scenario_runs_through_the_cli(self):
        for name in (
            "baseline_unchanged",
            "new_activity",
            "batch_boundaries",
            "rename",
            "history_rewrite",
            "failures",
            "classification_change",
            "hostile_text",
        ):
            code, out, err = _run_cli(["dry-run", os.path.join(FIXTURES, name)])
            self.assertEqual(code, 0, (name, err))
            json.loads(out)

    def test_usage_error_without_inputs(self):
        code, out, err = _run_cli(["dry-run"])
        self.assertEqual(code, 2)
        code, out, err = _run_cli([])
        self.assertEqual(code, 2)

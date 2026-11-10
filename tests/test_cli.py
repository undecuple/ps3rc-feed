"""Dry-run CLI: fixture-only paths, no URLs, read-only, prints what it would publish."""

import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

from helpers import FIXTURES, CollectorTestCase

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Events every step 1 scenario emits over all its runs, as the test plan describes them.
EXPECTED_EVENT_COUNTS = {
    "baseline_unchanged": 0,
    "new_activity": 4,  # commits + release, archived, discovered
    "batch_boundaries": 2,  # two commit groups
    "rename": 0,
    "history_rewrite": 1,  # one conservative commits event
    "failures": 3,  # incomplete commits, collection_stale, collection_recovered
    "classification_change": 1,
    "hostile_text": 1,
}


def _run_cli(argv):
    from collector import cli

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _snapshot(root):
    """Hash and size of every file under root, skipping only an exact .git component and bytecode caches."""
    snap = {}
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith(".pyc"):
                continue
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            snap[p] = (digest, os.stat(p).st_size)
    return snap


class DryRunPathTests(CollectorTestCase):
    def test_refuses_paths_outside_fixtures(self):
        from collector import cli

        cases = (
            "/etc/hosts",
            os.path.join(REPO_ROOT, "README.md"),
            os.path.join(FIXTURES, "..", "..", "README.md"),
            os.path.join(FIXTURES, "..", "helpers.py"),
            "tests",
            os.path.join(REPO_ROOT, "tests", "fixtures", "..", "fixtures", "..", "test_cli.py"),
        )
        self.assertEqual(len(cases), 6)
        for bad in cases:
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

        cases = (
            "https://github.com/example/example",
            "http://invalid.example/run.json",
            "file:///etc/hosts",
            "ftp://invalid.example/x",
            "javascript:alert(1)",
            "data:text/plain,hello",
        )
        self.assertEqual(len(cases), 6)
        for bad in cases:
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
        self.assertEqual(len(payload["inputs"]), 4)
        for p in payload["inputs"]:
            self.assertTrue(p.startswith("tests/fixtures/"), p)

    def test_individual_files_in_order_and_threshold_option(self):
        files = [os.path.join(FIXTURES, "failures", f) for f in sorted(os.listdir(os.path.join(FIXTURES, "failures")))]
        self.assertEqual(len(files), 8)
        code, out, err = _run_cli(["dry-run", "--stale-threshold", "2"] + files[:4])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([e["type"] for e in payload["runs"][3]["events"]], ["collection_stale"])

    def test_dry_run_writes_nothing(self):
        before = _snapshot(REPO_ROOT)
        self.assertGreater(len(before), 20)
        code, _, _ = _run_cli(["dry-run", os.path.join(FIXTURES, "hostile_text")])
        self.assertEqual(code, 0)
        self.assertEqual(_snapshot(REPO_ROOT), before)

    def test_every_step1_scenario_runs_through_the_cli(self):
        self.assertEqual(len(EXPECTED_EVENT_COUNTS), 8)
        for name, expected in EXPECTED_EVENT_COUNTS.items():
            code, out, err = _run_cli(["dry-run", os.path.join(FIXTURES, name)])
            self.assertEqual(code, 0, (name, err))
            payload = json.loads(out)
            self.assertEqual(len(payload["events"]), expected, name)
            self.assertEqual(sum(len(r["events"]) for r in payload["runs"]), expected, name)

    def test_usage_error_without_inputs(self):
        code, out, err = _run_cli(["dry-run"])
        self.assertEqual(code, 2)
        code, out, err = _run_cli([])
        self.assertEqual(code, 2)

    def test_output_is_identical_across_hash_seeds(self):
        """Set and dict ordering bugs only show across processes with different hash seeds."""
        outputs = []
        for seed in ("0", "1", "4242"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run(
                [sys.executable, "-B", "-m", "collector.cli", "dry-run", os.path.join("tests", "fixtures", "forks_two_roots")],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(proc.stdout)
        self.assertEqual(len(outputs), 3)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])
        self.assertIn("fork_discovered", outputs[0])


class NetworkImportBoundary(CollectorTestCase):
    def test_only_github_py_imports_network_modules(self):
        """cli.py and collect.py reach the network only through github.py."""
        forbidden = {"http", "http.client", "urllib", "urllib.request", "socket", "ssl"}
        for name in ("cli.py", "collect.py", "events.py", "catalog.py", "schema.py", "store.py", "forks.py", "model.py"):
            path = os.path.join(REPO_ROOT, "collector", name)
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=name)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            self.assertEqual(imported & forbidden, set(), name)
        with open(os.path.join(REPO_ROOT, "collector", "github.py"), "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertIn("http.client", names)

"""export: run files under data/ -> the static site's feed.json and manifest.json, offline and deterministic."""

import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from urllib.parse import urlsplit

from helpers import CollectorTestCase, load_run, scenario_files

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTILE = '<b>&amp;"'

# The repository record keys the page reads (site/terminal.js joinData, lines 229-255) plus url and description.
REPO_KEYS = {
    "id", "repo", "owner", "url", "description", "game", "game_inferred", "game_inherited", "category", "toolkit",
    "fork", "parent", "root", "ahead", "behind", "comparison_state", "stars", "commits_30d", "first_seen_at",
    "last_commit_at", "archived", "collection_failing", "removed", "game_key", "game_basis", "game_confidence", "matched_alias",
}
# The event record keys joinData reads (site/terminal.js lines 257-272).
EVENT_KEYS = {"id", "type", "repo_id", "source_at", "observed_at", "summary"}
CATEGORIES = ("framework", "game", "tool", "unclassified")  # terminal.js ENUMS.category, line 25
TYPES = (  # terminal.js TYPES, lines 11-22
    "repository_discovered", "commits_observed", "release_published", "repository_archived", "collection_stale",
    "collection_recovered", "fork_discovered", "fork_activity_observed", "fork_divergence_changed",
    "game_association_changed",
)


def _iso(value):
    """Date.parse accepts the collector's YYYY-MM-DDTHH:MM:SSZ form; anything else is treated as invalid here."""
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return True
    except (TypeError, ValueError):
        return False


def valid_rec(r):
    # site/app.js line 50: validRec = r is an object with a non-empty string id.
    return isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"] != ""


def valid_feed(f):
    # site/app.js lines 52-55: repos and events are arrays, generated_at a parseable string, every record validRec.
    return (
        isinstance(f, dict)
        and isinstance(f.get("repos"), list)
        and isinstance(f.get("events"), list)
        and isinstance(f.get("generated_at"), str)
        and _iso(f["generated_at"])
        and all(valid_rec(r) for r in f["repos"])
        and all(valid_rec(e) for e in f["events"])
    )


def safe_github_url(url):
    # site/terminal.js lines 180-190: https, host github.com, no port, no credentials, at most 2048 characters.
    if not isinstance(url, str) or len(url) > 2048:
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == "github.com" and parts.port is None and parts.username is None and parts.password is None


# site/terminal.js lines 204-210: the normalisers joinData applies. An exported value must be a fixed point:
# either null or a value the normaliser keeps as it is, so nothing turns into "unknown" by accident.
def js_str_ok(v):
    return v is None or (isinstance(v, str) and v.strip() != "")


def js_num_ok(v):
    return v is None or (isinstance(v, int) and not isinstance(v, bool) and v >= 0)


def js_bool_ok(v):
    return v is None or isinstance(v, bool)


def js_time_ok(v):
    return v is None or _iso(v)


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path + "." + k, k, v
            yield from walk(v, path + "." + k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, "%s[%d]" % (path, i))


def _run_cli(argv, fake_repo):
    from collector import cli

    out, err = io.StringIO(), io.StringIO()
    with unittest.mock.patch.object(cli, "REPO_ROOT", fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", os.path.join(fake_repo, "data")):
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _snapshot(root):
    snap = {}
    for dirpath, dirnames, files in os.walk(root):
        for name in dirnames + files:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            digest = None
            if os.path.isfile(p) and not os.path.islink(p):
                with open(p, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
            snap[p] = (st.st_mode, digest)
    return snap


class ExportTestCase(CollectorTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        self.data = os.path.join(self.repo, "data")
        os.makedirs(self.data)

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def put(self, name, run):
        path = os.path.join(self.data, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(run, fh)
        return "data/" + name

    def put_scenario(self, scenario):
        return [self.put("%s-%02d.json" % (scenario, i + 1), load_run(scenario, i + 1)) for i in range(len(scenario_files(scenario)))]

    def export(self, runs, out_dir="data/site", extra=()):
        return _run_cli(["export", "--runs"] + list(runs) + ["--out-dir", out_dir] + list(extra), self.repo)

    def read(self, out_dir="data/site"):
        base = os.path.join(self.repo, out_dir)
        with open(os.path.join(base, "feed.json"), "rb") as fh:
            feed_bytes = fh.read()
        with open(os.path.join(base, "manifest.json"), "rb") as fh:
            manifest_bytes = fh.read()
        return json.loads(feed_bytes), json.loads(manifest_bytes), feed_bytes, manifest_bytes

    def assertSiteShape(self, feed):
        self.assertTrue(valid_feed(feed))
        repo_ids = [r["id"] for r in feed["repos"]]
        self.assertEqual(len(repo_ids), len(set(repo_ids)))
        event_ids = [e["id"] for e in feed["events"]]
        self.assertEqual(len(event_ids), len(set(event_ids)))
        self.assertEqual(set(repo_ids) & set(event_ids), set())  # `show <id>` searches both lists
        for r in feed["repos"]:
            self.assertEqual(set(r), REPO_KEYS)
            for k in ("repo", "owner", "game", "toolkit", "parent", "root", "comparison_state", "description", "game_key", "game_basis", "game_confidence", "matched_alias"):
                self.assertTrue(js_str_ok(r[k]), (r["id"], k))
            for k in ("ahead", "behind", "stars", "commits_30d"):
                self.assertTrue(js_num_ok(r[k]), (r["id"], k))
            for k in ("fork", "archived", "collection_failing", "removed", "game_inferred", "game_inherited"):
                self.assertTrue(js_bool_ok(r[k]), (r["id"], k))
            for k in ("first_seen_at", "last_commit_at"):
                self.assertTrue(js_time_ok(r[k]), (r["id"], k))
            self.assertIn(r["category"], CATEGORIES + (None,))
            self.assertTrue(safe_github_url(r["url"]))
            self.assertEqual(r["url"], "https://github.com/" + r["repo"])
        for e in feed["events"]:
            self.assertEqual(set(e), EVENT_KEYS)
            self.assertIn(e["repo_id"], repo_ids)  # joinData finds the repository (byId.get(e.repo_id))
            self.assertIn(e["type"], TYPES)
            self.assertTrue(js_str_ok(e["summary"]))
            self.assertTrue(js_time_ok(e["source_at"]))
            self.assertTrue(_iso(e["observed_at"]))
        for path, key, value in walk(feed):
            self.assertNotEqual(key, "sample", path)
            self.assertNotIn(key, ("source_url", "href", "html_url"), path)
            if isinstance(value, str) and "://" in value:
                self.assertEqual(key, "url", path)


class ExportOutput(ExportTestCase):
    def test_new_activity_feed_and_manifest(self):
        runs = self.put_scenario("new_activity")
        code, out, err = self.export(runs)
        self.assertEqual(code, 0, err)
        feed, manifest, feed_bytes, manifest_bytes = self.read()
        self.assertSiteShape(feed)
        self.assertEqual(feed["generated_at"], "2026-11-08T00:00:00Z")
        self.assertEqual(manifest, {"feed": "feed.json", "generated_at": "2026-11-08T00:00:00Z", "version": hashlib.sha256(feed_bytes).hexdigest()[:16]})
        self.assertEqual(
            [e["id"] for e in feed["events"]],
            [
                "repo-110:discovered",
                "repo-100:archived:20261107000000",
                "repo-100:commits:a58ad27a05c413c56b11a28af491e38064a6898798fd5d190f5fe75b5a6a6272",
                "repo-100:release:7002",
            ],
        )
        self.assertEqual([r["id"] for r in feed["repos"]], ["r90", "r100", "r110"])
        skyfall = feed["repos"][1]
        self.assertEqual((skyfall["game"], skyfall["fork"], skyfall["parent"], skyfall["archived"]), ("Skyfall", True, "frame-org/ps3recomp-example", True))
        self.assertEqual(feed["events"][3]["summary"], "v0.2.0 published")
        self.assertEqual(json.loads(out)["version"], manifest["version"])
        # The canonical file form: sorted keys, ASCII, trailing newline.
        self.assertEqual(feed_bytes, (json.dumps(feed, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii"))
        self.assertEqual(manifest_bytes, (json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii"))

    def test_generated_at_is_the_newest_run_and_argument_order_does_not_matter(self):
        runs = self.put_scenario("new_activity")
        self.assertEqual(self.export(runs, "data/a")[0], 0)
        self.assertEqual(self.export(list(reversed(runs)), "data/b")[0], 0)
        a, b = self.read("data/a"), self.read("data/b")
        self.assertEqual(a[2], b[2])
        self.assertEqual(a[3], b[3])
        with unittest.mock.patch("time.time", return_value=0):
            self.assertEqual(self.export(runs, "data/c")[0], 0)
        self.assertEqual(self.read("data/c")[2], a[2])

    def test_version_changes_when_the_feed_changes(self):
        runs = self.put_scenario("new_activity")
        self.export(runs[:3], "data/a")
        self.export(runs, "data/b")
        self.assertNotEqual(self.read("data/a")[1]["version"], self.read("data/b")[1]["version"])

    def test_every_fixture_scenario_exports_a_valid_feed(self):
        for scenario in sorted(os.listdir(os.path.join(REPO_ROOT, "tests", "fixtures"))):
            if scenario == "github_recorded" or not os.path.isdir(os.path.join(REPO_ROOT, "tests", "fixtures", scenario)):
                continue
            with self.subTest(scenario=scenario):
                code, out, err = self.export(self.put_scenario(scenario), "data/" + scenario)
                self.assertEqual(code, 0, err)
                self.assertSiteShape(self.read("data/" + scenario)[0])

    def test_empty_run_file(self):
        run = load_run("new_activity", 1)
        run["repositories"] = []
        code, out, err = self.export([self.put("empty.json", run)])
        self.assertEqual(code, 0, err)
        feed, manifest, _, _ = self.read()
        collection = {"complete": True, "stopped": None, "not_fetched": 0, "error_count": 0, "last_run_at": run["run_at"]}
        self.assertEqual(feed, {"generated_at": run["run_at"], "collection": collection, "repos": [], "events": []})
        self.assertTrue(valid_feed(feed))
        self.assertEqual(manifest["generated_at"], run["run_at"])

    def test_unknown_values_are_null_and_every_key_is_present(self):
        first, second = load_run("new_activity", 1), load_run("new_activity", 2)
        failed = copy.deepcopy(second["repositories"][0])
        failed.update(
            repository_id=555, full_name="frame-org/never-fetched", description=None, readme_excerpt=None, is_fork=None,
            parent_repository_id=None, network_root_repository_id=None, archived=None, stars=None, default_branch=None,
            fetch_state="failed", commits_complete=None, commits=None, releases=None,
        )
        second["repositories"].append(failed)
        code, out, err = self.export([self.put("1.json", first), self.put("2.json", second)])
        self.assertEqual(code, 0, err)
        feed = self.read()[0]
        self.assertSiteShape(feed)
        rec = next(r for r in feed["repos"] if r["id"] == "r555")
        for key in ("description", "game", "toolkit", "fork", "parent", "root", "ahead", "behind", "stars", "commits_30d", "last_commit_at", "archived"):
            self.assertIsNone(rec[key], key)
        # Known states, not unknown values: no comparison was reported, the fetch failed.
        self.assertEqual((rec["comparison_state"], rec["category"], rec["collection_failing"]), ("unavailable", "unclassified", True))
        found = next(e for e in feed["events"] if e["repo_id"] == "r555")
        self.assertEqual((found["type"], found["source_at"]), ("repository_discovered", None))

    def test_escaped_store_text_is_exported_as_the_original_plain_text(self):
        from collector import store, events

        first, second = load_run("new_activity", 1), load_run("new_activity", 2)
        for run in (first, second):
            run["repositories"][1]["description"] = HOSTILE
        second["repositories"][1]["releases"][-1]["tag"] = HOSTILE
        state = store.empty_state()
        events.apply_run(state, copy.deepcopy(first))
        events.apply_run(state, copy.deepcopy(second))
        stored = [e["summary"] for e in state["events"] if e["type"] == "release_published"]
        self.assertEqual(stored, ["&lt;b&gt;&amp;amp;&quot; published"])  # the store keeps its escaped form

        code, out, err = self.export([self.put("1.json", first), self.put("2.json", second)])
        self.assertEqual(code, 0, err)
        feed, _, feed_bytes, _ = self.read()
        release = next(e for e in feed["events"] if e["type"] == "release_published")
        self.assertEqual(release["summary"], HOSTILE + " published")
        self.assertEqual(next(r for r in feed["repos"] if r["id"] == "r100")["description"], HOSTILE)
        for entity in (b"&lt;", b"&gt;", b"&quot;", b"&amp;amp;", b"&#x27;"):
            self.assertNotIn(entity, feed_bytes)

    def test_unescape_inverts_escape_text_including_bounded_text(self):
        from collector import export, text

        samples = [HOSTILE, "a & b", "&amp;lt;", "'quoted' \"double\"", "<script>alert(1)</script>", "x" * 400, "&" * 400, "<>" * 200, "@everyone hi", "tab\there"]
        for s in samples:
            escaped = text.escape_text(s)
            got = export.plain(escaped)
            if len(escaped) <= text.MAX_TEXT and not escaped.endswith("..."):
                self.assertEqual(got, text.plain_text(s), s[:20])
            else:
                # A cut never leaves half an entity: unescaping the kept prefix gives a prefix of the plain text.
                self.assertTrue(got.endswith("..."))
                self.assertTrue(text.plain_text(s, 10**6).startswith(got[:-3].rstrip()), s[:20])
        self.assertIsNone(export.plain(None))
        self.assertIsNone(export.plain("   "))

    def test_hostile_text_feed_has_no_link_but_repository_urls(self):
        code, out, err = self.export(self.put_scenario("hostile_text"))
        self.assertEqual(code, 0, err)
        feed = self.read()[0]
        self.assertSiteShape(feed)
        for path, key, value in walk(feed):
            if isinstance(value, str):
                self.assertFalse(any(ord(ch) < 32 or ord(ch) == 127 for ch in value), path)

    def test_event_and_repository_caps(self):
        from collector import export

        runs = self.put_scenario("new_activity")
        with unittest.mock.patch.object(export, "MAX_FEED_EVENTS", 2), unittest.mock.patch.object(export, "MAX_FEED_REPOS", 1):
            code, out, err = self.export(runs)
        self.assertEqual(code, 0, err)
        feed = self.read()[0]
        self.assertEqual([e["id"] for e in feed["events"]], ["repo-110:discovered", "repo-100:archived:20261107000000"])
        # Repositories the kept events refer to stay even above the repository cap.
        self.assertEqual([r["id"] for r in feed["repos"]], ["r100", "r110"])
        self.assertSiteShape(feed)


class ExportDeterminism(ExportTestCase):
    def test_byte_identical_across_runs_and_hash_seeds(self):
        runs = self.put_scenario("forks_two_roots")
        script = (
            "import sys, unittest.mock\n"
            "sys.path.insert(0, %r)\n"
            "from collector import cli\n"
            "repo = sys.argv[1]\n"
            "with unittest.mock.patch.object(cli, 'REPO_ROOT', repo), unittest.mock.patch.object(cli, 'DATA_ROOT', repo + '/data'):\n"
            "    sys.exit(cli.main(sys.argv[2:]))\n" % REPO_ROOT
        )
        outputs = []
        for i, seed in enumerate(("0", "1", "4242", "random")):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out_dir = "data/seed%d" % i
            proc = subprocess.run(
                [sys.executable, "-B", "-c", script, self.repo, "export", "--runs"] + runs + ["--out-dir", out_dir],
                cwd=self.tmp.name, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(self.read(out_dir)[2:])
        code, _, err = self.export(runs, "data/inproc")
        self.assertEqual(code, 0, err)
        outputs.append(self.read("data/inproc")[2:])
        for other in outputs[1:]:
            self.assertEqual(other, outputs[0])
        self.assertIn(b"fork_discovered", outputs[0][0])


class ExportWrites(ExportTestCase):
    def test_feed_is_written_before_manifest(self):
        from collector import cli

        real_write = cli.write_run_file
        order = []

        def recording(document, out_path, *args, **kwargs):
            # When the manifest is written the feed it names must already be on disk.
            order.append((os.path.basename(out_path), os.path.exists(os.path.join(os.path.dirname(out_path), "feed.json"))))
            return real_write(document, out_path, *args, **kwargs)

        runs = self.put_scenario("new_activity")
        with unittest.mock.patch.object(cli, "write_run_file", recording):
            code, out, err = self.export(runs)
        self.assertEqual(code, 0, err)
        self.assertEqual(order, [("feed.json", False), ("manifest.json", True)])

    def test_write_time_refusals_name_out_dir_and_rerun_with_overwrite_repairs(self):
        from collector import cli

        real_write = cli.write_run_file

        def manifest_appears(document, out_path, *args, **kwargs):
            if os.path.basename(out_path) == "manifest.json":
                with open(out_path, "w", encoding="utf-8") as fh:
                    fh.write("OTHER")
            return real_write(document, out_path, *args, **kwargs)

        runs = self.put_scenario("new_activity")
        with unittest.mock.patch.object(cli, "write_run_file", manifest_appears):
            code, out, err = self.export(runs)
        self.assertEqual(code, 2)
        self.assertIn("refused: --out-dir exists; pass --overwrite to replace it", err)
        self.assertTrue(os.path.exists(os.path.join(self.data, "site", "feed.json")))
        code, out, err = self.export(runs, "data/site", ["--overwrite"])
        self.assertEqual(code, 0, err)
        feed, manifest, feed_bytes, _ = self.read()
        self.assertEqual(manifest["version"], hashlib.sha256(feed_bytes).hexdigest()[:16])

    def test_shared_write_helpers_name_the_flag(self):
        from collector import cli

        target = os.path.join(self.data, "x.json")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.symlink(self.tmp.name, os.path.join(self.data, "link"))
        with unittest.mock.patch.object(cli, "REPO_ROOT", self.repo), unittest.mock.patch.object(cli, "DATA_ROOT", self.data):
            for flag in (None, "--out-dir"):
                kwargs = {} if flag is None else {"flag": flag}
                expect = flag or "--out"
                with self.subTest(flag=flag):
                    with self.assertRaises(cli.RefusedInput) as ctx:
                        cli.write_run_file({}, target, **kwargs)
                    self.assertEqual(str(ctx.exception), "refused: %s exists; pass --overwrite to replace it" % expect)
                    with self.assertRaises(cli.RefusedInput) as ctx:
                        cli.write_run_file({}, os.path.join(self.data, "link", "y.json"), **kwargs)
                    self.assertEqual(str(ctx.exception), "refused: %s path contains a symbolic link" % expect)
                    with self.assertRaises(cli.RefusedInput) as ctx:
                        cli.write_run_file({}, os.path.join(self.data, "x.json", "y.json"), **kwargs)
                    self.assertEqual(str(ctx.exception), "refused: %s path component is not a directory" % expect)
                    with self.assertRaises(cli.RefusedInput) as ctx:
                        cli.write_run_file({}, os.path.join(self.tmp.name, "z.json"), **kwargs)
                    self.assertEqual(str(ctx.exception), "refused: %s must lie under the repository" % expect)
                    os.symlink(target, os.path.join(self.data, "t.json"))
                    try:
                        with self.assertRaises(cli.RefusedInput) as ctx:
                            cli.write_run_file({}, os.path.join(self.data, "t.json"), overwrite=True, **kwargs)
                        self.assertEqual(str(ctx.exception), "refused: %s path contains a symbolic link" % expect)
                    finally:
                        os.unlink(os.path.join(self.data, "t.json"))
        self.assertEqual(os.listdir(self.tmp.name), ["repo"])


class ExportRefusals(ExportTestCase):
    def assertRefused(self, argv_runs, out_dir, needle, extra=()):
        before = _snapshot(self.tmp.name)
        code, out, err = self.export(argv_runs, out_dir, extra)
        self.assertEqual(code, 2, (out_dir, err))
        self.assertIn(needle, err)
        self.assertEqual(out, "")
        self.assertEqual(_snapshot(self.tmp.name), before)

    def test_out_dir_outside_data(self):
        runs = self.put_scenario("new_activity")
        for bad in ("site", "data/../site", os.path.join(self.tmp.name, "elsewhere"), "/tmp/ps3rc-export-test", "tests/fixtures"):
            self.assertRefused(runs, bad, "must lie under the data/ directory")

    def test_runs_outside_data_or_not_run_files(self):
        self.put_scenario("new_activity")
        outside = os.path.join(self.tmp.name, "outside.json")
        with open(outside, "w", encoding="utf-8") as fh:
            json.dump(load_run("new_activity", 1), fh)
        for bad, needle in (
            (outside, "outside the data/ directory"),
            ("tests/fixtures/new_activity/01-baseline.json", "outside the data/ directory"),
            ("data/../outside.json", "outside the data/ directory"),
            ("https://example.invalid/run.json", "URLs"),
            ("data/missing.json", "does not exist"),
            ("data", ".json run files"),
        ):
            self.assertRefused([bad], "data/site", needle)

    def test_over_long_names(self):
        runs = self.put_scenario("new_activity")
        self.assertRefused(runs, "data/" + "d" * 250, "longer than 200 bytes")
        self.assertRefused(runs, "data/" + "é" * 101, "longer than 200 bytes")
        self.assertRefused(["data/" + "r" * 300 + ".json"], "data/site", "does not exist")

    def test_control_characters(self):
        runs = self.put_scenario("new_activity")
        for bad in ("data/a\nb", "data/a\x00b", "data/a\x7fb"):
            self.assertRefused(runs, bad, "control character")

    def test_existing_target_needs_overwrite(self):
        runs = self.put_scenario("new_activity")
        self.assertEqual(self.export(runs[:2])[0], 0)
        old = self.read()
        self.assertRefused(runs, "data/site", "pass --overwrite")
        # Only one of the two files present is still refused before anything is written.
        os.unlink(os.path.join(self.data, "site", "feed.json"))
        self.assertRefused(runs, "data/site", "(manifest.json)")
        code, out, err = self.export(runs, "data/site", ["--overwrite"])
        self.assertEqual(code, 0, err)
        new = self.read()
        self.assertNotEqual(new[1]["version"], old[1]["version"])
        self.assertEqual(new[1]["generated_at"], "2026-11-08T00:00:00Z")

    def test_symbolic_links(self):
        runs = self.put_scenario("new_activity")
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(self.data, "linked"))
        self.assertRefused(runs, "data/linked", "symbolic link")
        os.makedirs(os.path.join(self.data, "site"))
        os.symlink(os.path.join(elsewhere, "feed.json"), os.path.join(self.data, "site", "feed.json"))
        self.assertRefused(runs, "data/site", "symbolic link", ["--overwrite"])
        os.symlink(os.path.join(REPO_ROOT, "tests", "fixtures", "new_activity", "01-baseline.json"), os.path.join(self.data, "link.json"))
        self.assertRefused(["data/link.json"], "data/out", "symbolic links are not accepted")
        os.symlink(os.path.join(REPO_ROOT, "tests", "fixtures", "new_activity"), os.path.join(self.data, "linkdir"))
        self.assertRefused(["data/linkdir/01-baseline.json"], "data/out", "symbolic links are not accepted")
        self.assertEqual(os.listdir(elsewhere), [])

    def test_duplicate_runs_and_inputs_as_targets(self):
        runs = self.put_scenario("new_activity")
        self.assertRefused([runs[0], runs[0]], "data/site", "same file twice")
        self.put("feed.json", load_run("new_activity", 1))
        self.assertRefused(["data/feed.json"], "data", "would replace one of the --runs files", ["--overwrite"])

    def test_run_files_with_the_same_run_at_are_refused_in_any_order(self):
        first, second = load_run("new_activity", 1), load_run("new_activity", 2)
        second["run_at"] = first["run_at"]
        a, b, c = self.put("a.json", first), self.put("b.json", second), self.put("c.json", load_run("new_activity", 3))
        for order in ([a, b, c], [b, a, c], [c, b, a]):
            self.assertRefused(order, "data/site", "have the same run_at")
            err = self.export(order)[2]
            self.assertIn("data/a.json", err)
            self.assertIn("data/b.json", err)
        # A byte-identical copy under another name is refused too, not applied twice.
        with open(os.path.join(self.data, "a.json"), "rb") as src, open(os.path.join(self.data, "a-copy.json"), "wb") as dst:
            dst.write(src.read())
        self.assertRefused([a, "data/a-copy.json"], "data/site", "have the same run_at")

    def test_invalid_run_file_is_an_error_and_writes_nothing(self):
        bad = load_run("new_activity", 1)
        bad["unexpected"] = True
        path = self.put("bad.json", bad)
        before = _snapshot(self.tmp.name)
        code, out, err = self.export([path])
        self.assertEqual(code, 1)
        self.assertIn("unknown field", err)
        self.assertEqual(_snapshot(self.tmp.name), before)

    def test_python_dash_m_collector_entry_point(self):
        proc = subprocess.run([sys.executable, "-B", "-m", "collector", "export", "--help"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--out-dir", proc.stdout)
        proc = subprocess.run([sys.executable, "-B", "-m", "collector", "export", "--runs", "/etc/hosts", "--out-dir", "data/never-written"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(REPO_ROOT, "data", "never-written")))


def _baseline_and_later():
    """A baseline run, then a later run that adds a new repository and a new fork of the root."""
    first, second = load_run("new_activity", 1), load_run("new_activity", 2)
    template = second["repositories"][0]
    added = copy.deepcopy(template)
    added.update(repository_id=120, full_name="other-org/new-recomp", owner="other-org", description="Another fictional project")
    fork = copy.deepcopy(template)
    fork.update(
        repository_id=130, full_name="fork-z/ps3recomp-example", owner="fork-z", description="Fictional fork",
        is_fork=True, parent_repository_id=90, network_root_repository_id=90,
    )
    second["repositories"] += [added, fork]
    second["searches"][0]["repository_ids"] = sorted(set(second["searches"][0]["repository_ids"]) | {120})
    second["roots"][0]["found_repository_ids"] = sorted(set(second["roots"][0].get("found_repository_ids") or []) | {100, 130})
    return first, second


def _partial(run, run_at):
    partial = copy.deepcopy(run)
    partial["run_at"] = run_at
    partial["collection_report"] = {"complete": False, "stopped": "budget", "error_count": 2, "dropped_items": 0, "not_fetched": 3, "requests_made": 10}
    return partial


class ExportState(ExportTestCase):
    def setUp(self):
        super().setUp()
        first, second = _baseline_and_later()
        self.first, self.second = self.put("1.json", first), self.put("2.json", second)

    def state_bytes(self, name):
        with open(os.path.join(self.data, name), "rb") as fh:
            return fh.read()

    def test_saved_state_plus_next_run_equals_both_runs_from_scratch(self):
        from collector import store

        code, out, err = self.export([self.first], "data/a", ["--state-out", "data/state.json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["written"], ["data/state.json", "data/a/feed.json", "data/a/manifest.json"])
        code, out, err = self.export([self.second], "data/b", ["--state-in", "data/state.json", "--state-out", "data/state2.json"])
        self.assertEqual(code, 0, err)
        code, out, err = self.export([self.first, self.second], "data/c", ["--state-out", "data/scratch-state.json"])
        self.assertEqual(code, 0, err)
        b, c = self.read("data/b"), self.read("data/c")
        self.assertEqual(b[2], c[2])
        self.assertEqual(b[3], c[3])
        self.assertEqual(self.state_bytes("state2.json"), self.state_bytes("scratch-state.json"))
        # The state file is the store's canonical form and loads back as it is.
        loaded = store.load_state(os.path.join(self.data, "state2.json"))
        self.assertEqual(store.dumps(loaded).encode("utf-8"), self.state_bytes("state2.json"))
        # Events across the two exports: the new repository and the new fork are announced, the baseline is quiet.
        self.assertSiteShape(b[0])
        found = {(e["type"], e["repo_id"]) for e in b[0]["events"]}
        self.assertIn(("repository_discovered", "r120"), found)
        self.assertIn(("repository_discovered", "r130"), found)
        self.assertIn(("fork_discovered", "r130"), found)
        self.assertEqual(self.read("data/a")[0]["events"], [])
        # --state-in equal to --state-out with --overwrite: the usual atomic replace, same result.
        code, out, err = self.export([self.second], "data/d", ["--state-in", "data/state.json", "--state-out", "data/state.json", "--overwrite"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("data/d")[2], c[2])
        self.assertEqual(self.state_bytes("state.json"), self.state_bytes("scratch-state.json"))
        self.assertEqual(sorted(n for n in os.listdir(self.data) if n.endswith(".tmp")), [])

    def test_write_order_is_state_feed_manifest(self):
        from collector import cli

        real_write = cli._write_bytes
        order = []

        def recording(data, out_path, *args, **kwargs):
            order.append(os.path.basename(out_path))
            return real_write(data, out_path, *args, **kwargs)

        with unittest.mock.patch.object(cli, "_write_bytes", recording):
            code, out, err = self.export([self.first], "data/site", ["--state-out", "data/state.json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(order, ["state.json", "feed.json", "manifest.json"])

    def test_failed_feed_write_keeps_the_state_and_the_next_export_catches_up(self):
        from collector import cli

        self.assertEqual(self.export([self.first], "data/site", ["--state-out", "data/state.json"])[0], 0)
        old_feed = self.read()[2]
        real_write = cli.write_run_file

        def failing(document, out_path, *args, **kwargs):
            if os.path.basename(out_path) == "feed.json":
                raise OSError(28, "No space left on device")
            return real_write(document, out_path, *args, **kwargs)

        argv = ["--state-in", "data/state.json", "--state-out", "data/state.json", "--overwrite"]
        with unittest.mock.patch.object(cli, "write_run_file", failing):
            code, out, err = self.export([self.second], "data/site", argv)
        self.assertEqual(code, 1)
        self.assertEqual(self.read()[2], old_feed)  # the published pair is the previous one
        # The state already holds the run: repeating it is refused, not applied twice.
        code, out, err = self.export([self.second], "data/site", argv)
        self.assertEqual(code, 2)
        self.assertIn("not newer than the --state-in last run", err)
        third = load_run("new_activity", 3)
        third["run_at"] = "2026-11-09T00:00:00Z"
        code, out, err = self.export([self.put("3.json", third)], "data/site", argv)
        self.assertEqual(code, 0, err)
        found = {(e["type"], e["repo_id"]) for e in self.read()[0]["events"]}
        self.assertIn(("fork_discovered", "r130"), found)

    def assertRefused(self, argv_runs, out_dir, needle, extra=(), code=2):
        before = _snapshot(self.tmp.name)
        got, out, err = self.export(argv_runs, out_dir, extra)
        self.assertEqual(got, code, (extra, err))
        self.assertIn(needle, err)
        self.assertEqual(out, "")
        self.assertNotIn(self.tmp.name, err)
        self.assertEqual(_snapshot(self.tmp.name), before)
        return err

    def test_runs_not_newer_than_the_state_are_refused(self):
        self.assertEqual(self.export([self.second], "data/site", ["--state-out", "data/state.json"])[0], 0)
        older = ["--state-in", "data/state.json", "--state-out", "data/next.json"]
        err = self.assertRefused([self.first], "data/next", "not newer than the --state-in last run (2026-11-06T12:00:00Z)", older)
        self.assertIn("data/1.json has run_at 2026-11-05T00:00:00Z", err)
        self.assertRefused([self.second], "data/next", "not newer than the --state-in last run", older)
        # One old run among newer ones is refused too.
        third = self.put("3.json", load_run("new_activity", 3))
        self.assertRefused([third, self.first], "data/next", "data/1.json has run_at", older)

    def test_unusable_state_files_are_errors(self):
        from collector import store

        def put_state(name, content):
            with open(os.path.join(self.data, name), "w", encoding="utf-8") as fh:
                fh.write(content)
            return "data/" + name

        good = store.empty_state()
        old = dict(good, schema_version=1)
        missing = copy.deepcopy(good)
        del missing["collection"]["last_run"]
        wrong_type = dict(good, last_run_at=5)
        for name, content, needle in (
            ("garbage.json", "{not json", "not valid JSON"),
            ("list.json", "[]", "schema_version None is not supported"),
            ("old.json", json.dumps(old), "schema_version 1 is not supported"),
            ("future.json", json.dumps(dict(good, schema_version=99)), "schema_version 99 is not supported"),
            ("missing.json", json.dumps(missing), "last_run"),
            ("wrong.json", json.dumps(wrong_type), "last_run_at"),
            ("dup.json", '{"schema_version": 2, "schema_version": 2}', "duplicate"),
        ):
            with self.subTest(name=name):
                path = put_state(name, content)
                err = self.assertRefused([self.first], "data/site", needle, ["--state-in", path, "--state-out", "data/out.json"], code=1)
                self.assertIn("--state-in data/%s is not a usable state" % name, err)

    def test_state_path_refusals(self):
        self.assertEqual(self.export([self.first], "data/site", ["--state-out", "data/state.json"])[0], 0)
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(os.path.join(self.data, "state.json"), os.path.join(self.data, "link.json"))
        os.symlink(elsewhere, os.path.join(self.data, "linkdir"))
        for extra, needle in (
            (["--state-in", "data/absent.json"], "does not exist"),
            (["--state-in", "data/link.json"], "symbolic links are not accepted"),
            (["--state-in", "data"], ".json state file"),
            (["--state-in", os.path.join(self.tmp.name, "x.json")], "outside the data/ directory"),
            (["--state-in", "https://example.invalid/state.json"], "URLs"),
            (["--state-in", self.second], "--state-in is also named in --runs"),
            (["--state-out", "data/link.json", "--overwrite"], "--state-out path contains a symbolic link"),
            (["--state-out", "data/linkdir/state.json"], "--state-out path contains a symbolic link"),
            (["--state-out", "data/state.json"], "--state-out exists; pass --overwrite to replace it"),
            (["--state-in", "data/state.json", "--state-out", "data/state.json"], "--state-out exists; pass --overwrite"),
            (["--state-out", self.second], "--state-out exists; pass --overwrite"),
            (["--state-out", self.second, "--overwrite"], "--state-out would replace one of the --runs files"),
            (["--state-out", "data/next/feed.json", "--overwrite"], "--state-out is the same file as a --out-dir output"),
            (["--state-out", "state.json"], "--state-out must lie under the data/ directory"),
            (["--state-out", "data/state.txt"], "--state-out must be a .json file"),
            (["--state-out", "data/" + "s" * 250 + ".json"], "--state-out has a component longer than 200 bytes"),
            (["--state-out", "data/a\nb.json"], "--state-out contains a control character"),
        ):
            with self.subTest(extra=extra):
                self.assertRefused([self.second], "data/next", needle, extra)
        self.assertRefused([self.second], "data/site", "--out-dir would replace the --state-in file", ["--state-in", "data/site/feed.json", "--overwrite"])
        self.assertEqual(os.listdir(elsewhere), [])

    def test_collection_fields_for_complete_and_partial_runs(self):
        code, out, err = self.export([self.first], "data/a", ["--state-out", "data/state.json"])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            self.read("data/a")[0]["collection"],
            {"complete": True, "stopped": None, "not_fetched": 0, "error_count": 0, "last_run_at": "2026-11-05T00:00:00Z"},
        )
        partial = self.put("p.json", _partial(load_run("new_activity", 2), "2026-11-06T00:00:00Z"))
        code, out, err = self.export([partial], "data/b", ["--state-in", "data/state.json"])
        self.assertEqual(code, 0, err)
        feed = self.read("data/b")[0]
        self.assertEqual(
            feed["collection"],
            {"complete": False, "stopped": "budget", "not_fetched": 3, "error_count": 2, "last_run_at": "2026-11-06T00:00:00Z"},
        )
        self.assertEqual(feed["collection"]["last_run_at"], feed["generated_at"])
        self.assertSiteShape(feed)

    def test_stale_after_minutes(self):
        code, out, err = self.export([self.first], "data/none")
        self.assertEqual(code, 0, err)
        self.assertNotIn("stale_after_minutes", self.read("data/none")[0])
        for value in (1, 100000, 90):
            with self.subTest(value=value):
                out_dir = "data/ok%d" % value
                code, out, err = self.export([self.first], out_dir, ["--stale-after-minutes", str(value)])
                self.assertEqual(code, 0, err)
                feed, manifest, feed_bytes, _ = self.read(out_dir)
                self.assertEqual(feed["stale_after_minutes"], value)
                self.assertEqual(manifest["version"], hashlib.sha256(feed_bytes).hexdigest()[:16])
        for bad in ("0", "-5", "100001", "10" * 20):
            with self.subTest(bad=bad):
                self.assertRefused([self.first], "data/bad", "--stale-after-minutes must be between 1 and 100000", ["--stale-after-minutes", bad])
        for bad in ("abc", "1.5", ""):
            with self.subTest(bad=bad):
                self.assertRefused([self.first], "data/bad", "", ["--stale-after-minutes", bad])
        # Checked before anything is read: an invalid run file is not reached.
        broken = self.put("broken.json", {"unexpected": True})
        self.assertRefused([broken], "data/bad", "--stale-after-minutes must be between", ["--stale-after-minutes", "0"])

    def test_state_bytes_identical_across_hash_seeds(self):
        script = (
            "import sys, unittest.mock\n"
            "sys.path.insert(0, %r)\n"
            "from collector import cli\n"
            "repo = sys.argv[1]\n"
            "with unittest.mock.patch.object(cli, 'REPO_ROOT', repo), unittest.mock.patch.object(cli, 'DATA_ROOT', repo + '/data'):\n"
            "    sys.exit(cli.main(sys.argv[2:]))\n" % REPO_ROOT
        )
        outputs = []
        for i, seed in enumerate(("0", "1", "4242", "random")):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            steps = (
                [self.first, "--out-dir", "data/s%d/a" % i, "--state-out", "data/s%d/state1.json" % i],
                [self.second, "--out-dir", "data/s%d/b" % i, "--state-in", "data/s%d/state1.json" % i, "--state-out", "data/s%d/state2.json" % i],
            )
            for step in steps:
                proc = subprocess.run(
                    [sys.executable, "-B", "-c", script, self.repo, "export", "--runs"] + step,
                    cwd=self.tmp.name, env=env, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(tuple(self.state_bytes("s%d/state%d.json" % (i, n)) for n in (1, 2)) + (self.read("data/s%d/b" % i)[2],))
        for other in outputs[1:]:
            self.assertEqual(other, outputs[0])
        self.assertIn(b'"fork_discovered_done": true', outputs[0][1])

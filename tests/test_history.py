"""history: feed.json -> the feed-history directory (monthly events, change-only snapshots, index), offline."""

import copy
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from helpers import CollectorTestCase, load_run, scenario_files

from collector import cli, history

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_FEED = os.path.join(REPO_ROOT, "site", "data", "feed.json")


def repo(n, stars=1, commits=0, category="game", archived=False, fork=False, last=None, description="a project", name=None):
    full = name or "owner%d/name%d" % (n, n)
    return {
        "id": "r%d" % n, "repo": full, "owner": full.split("/")[0], "url": "https://github.com/" + full,
        "description": description, "game": None, "game_inferred": True, "game_inherited": False, "category": category,
        "toolkit": None, "fork": fork, "parent": None, "root": None, "ahead": None, "behind": None,
        "comparison_state": None, "stars": stars, "commits_30d": commits, "first_seen_at": "2026-01-01T00:00:00Z",
        "last_commit_at": last, "archived": archived, "collection_failing": False,
    }


def event(n, kind, observed, source=None, summary="something happened"):
    return {"id": "repo-%d:%s" % (n, kind), "type": "repository_discovered", "repo_id": "r%d" % n, "source_at": source,
            "observed_at": observed, "summary": summary}


def feed(run_at, repos, events=(), stopped=None):
    return {
        "generated_at": run_at,
        "collection": {"complete": stopped is None, "stopped": stopped, "not_fetched": 0, "error_count": 0, "last_run_at": run_at},
        "repos": list(repos),
        "events": list(events),
        "stale_after_minutes": 1500,
    }


def tree(root):
    """{relative path: bytes} of every file under root (directories implied), for before/after comparisons."""
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = fh.read()
    return out


def lines(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if path.endswith(".gz"):
        data = gzip.decompress(data)
    return [json.loads(line) for line in data.decode("ascii").splitlines()]


class HistoryTestCase(CollectorTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        self.data = os.path.join(self.repo, "data")
        self.hist = os.path.join(self.data, "history")
        os.makedirs(self.data)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(cli, "REPO_ROOT", self.repo), unittest.mock.patch.object(cli, "DATA_ROOT", self.data):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def put(self, name, document):
        path = os.path.join(self.data, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(document, fh)
        return "data/" + name

    def apply(self, document, extra=(), name="feed.json"):
        return self.cli("history", "--feed", self.put(name, document), "--history-dir", "data/history", *extra)

    def snapshots(self, month):
        return lines(os.path.join(self.hist, "snapshots", "%s.jsonl" % month))

    def events(self, month):
        return lines(os.path.join(self.hist, "events", "%s.jsonl" % month))

    def index(self):
        with open(os.path.join(self.hist, "index.json"), encoding="utf-8") as fh:
            return json.load(fh)


RUN1 = "2026-11-10T06:17:00Z"
RUN2 = "2026-11-10T12:17:00Z"
RUN3 = "2026-11-10T18:17:00Z"


class FirstRun(HistoryTestCase):
    def test_layout(self):
        code, out, _ = self.apply(feed(RUN1, [repo(2), repo(1)], [event(1, "repository_discovered", RUN1)]))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(tree(self.hist)), ["README.md", "events/2026-11.jsonl", "index.json", "snapshots/2026-11.jsonl"])
        snap = self.snapshots("2026-11")
        self.assertEqual(snap[0], {"run_at": RUN1, "complete": True, "stopped": None, "not_fetched": 0, "repos": 2})
        self.assertEqual([line["id"] for line in snap[1:]], ["r1", "r2"])  # id order, full lines on first sight
        self.assertEqual(set(snap[1]), set(history.SNAPSHOT_FIELDS))
        self.assertEqual(self.events("2026-11"), [event(1, "repository_discovered", RUN1)])
        index = self.index()
        self.assertEqual(index["format_version"], 1)
        self.assertEqual(index["last_run_at"], RUN1)
        self.assertEqual(index["runs"], 1)
        self.assertEqual(index["months"], ["2026-11"])
        self.assertEqual(index["files"]["snapshots/2026-11.jsonl"]["lines"], 3)
        with open(os.path.join(self.hist, "snapshots", "2026-11.jsonl"), "rb") as fh:
            raw = fh.read()
        self.assertEqual(index["files"]["snapshots/2026-11.jsonl"]["bytes"], len(raw))
        self.assertEqual(index["files"]["snapshots/2026-11.jsonl"]["sha256"], hashlib.sha256(raw).hexdigest())
        summary = json.loads(out)
        self.assertEqual(summary["files"]["snapshots/2026-11.jsonl"], len(raw))
        self.assertEqual(summary["total_bytes"], index["total_bytes"])
        self.assertFalse(summary["over_budget"])
        self.assertEqual(summary["written"][-1], "index.json")

    def test_a_baseline_feed_without_events_writes_snapshots_only(self):
        self.assertEqual(self.apply(feed(RUN1, [repo(1)]))[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.hist, "events")))
        self.assertEqual(self.index()["files"].keys(), {"snapshots/2026-11.jsonl"})

    def test_an_existing_git_checkout_is_accepted(self):
        os.makedirs(os.path.join(self.hist, ".git", "objects"))
        with open(os.path.join(self.hist, ".git", "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/feed-history\n")
        self.assertEqual(self.apply(feed(RUN1, [repo(1)]))[0], 0)
        with open(os.path.join(self.hist, ".git", "HEAD")) as fh:
            self.assertEqual(fh.read(), "ref: refs/heads/feed-history\n")


class RunOrder(HistoryTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.apply(feed(RUN2, [repo(1)], [event(1, "repository_discovered", RUN2)]))[0], 0)
        self.before = tree(self.hist)

    def test_the_same_run_again_is_a_no_op(self):
        code, out, err = self.apply(feed(RUN2, [repo(1, stars=99), repo(2)], [event(2, "repository_discovered", RUN2)]))
        self.assertEqual(code, 0)
        self.assertIn("already holds the run", err)
        self.assertTrue(json.loads(out)["unchanged"])
        self.assertEqual(tree(self.hist), self.before)

    def test_an_older_run_is_refused(self):
        code, _, err = self.apply(feed(RUN1, [repo(1)]))
        self.assertEqual(code, 2)
        self.assertIn("older than the history's last run", err)
        self.assertEqual(tree(self.hist), self.before)

    def test_a_newer_run_is_applied(self):
        self.assertEqual(self.apply(feed(RUN3, [repo(1)]))[0], 0)
        self.assertEqual(self.index()["runs"], 2)
        self.assertEqual(self.index()["last_run_at"], RUN3)


class ChangeOnlySnapshots(HistoryTestCase):
    def test_unchanged_repositories_write_nothing(self):
        self.apply(feed(RUN1, [repo(1), repo(2)]))
        self.apply(feed(RUN2, [repo(1), repo(2)]))
        snap = self.snapshots("2026-11")
        self.assertEqual(len(snap), 4)  # header, r1, r2, header
        self.assertEqual(snap[3]["run_at"], RUN2)

    def test_a_changed_value_writes_one_full_line(self):
        self.apply(feed(RUN1, [repo(1), repo(2)]))
        code, out, _ = self.apply(feed(RUN2, [repo(1), repo(2, stars=5)]))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["snapshot_lines"], 1)
        snap = self.snapshots("2026-11")
        self.assertEqual(snap[-1], history.snapshot_record(repo(2, stars=5)))
        for field, value in (("commits_30d", 3), ("category", "framework"), ("archived", True), ("fork", None),
                             ("last_commit_at", "2026-11-09T00:00:00Z")):
            with self.subTest(field=field):
                changed = repo(1)
                changed[field] = value
                run_at = "2026-11-%02dT00:00:00Z" % (11 + list(history.SNAPSHOT_FIELDS).index(field))
                self.apply(feed(run_at, [changed, repo(2, stars=5)]))
                self.assertEqual(self.snapshots("2026-11")[-1], history.snapshot_record(changed))
                self.apply(feed(run_at.replace("T00", "T01"), [repo(1), repo(2, stars=5)]))  # back again: one more line

    def test_a_rename_is_a_change(self):
        self.apply(feed(RUN1, [repo(1)]))
        self.apply(feed(RUN2, [repo(1, name="newowner/newname")]))
        self.assertEqual(self.snapshots("2026-11")[-1]["repo"], "newowner/newname")

    def test_other_fields_are_not_tracked(self):
        self.apply(feed(RUN1, [repo(1)]))
        changed = repo(1, description="something else entirely")
        changed["ahead"] = 4
        self.apply(feed(RUN2, [changed]))
        self.assertEqual(len(self.snapshots("2026-11")), 3)

    def test_removed_and_added_again(self):
        self.apply(feed(RUN1, [repo(1), repo(2)]))
        self.apply(feed(RUN2, [repo(1)]))
        snap = self.snapshots("2026-11")
        self.assertEqual(snap[-2]["run_at"], RUN2)
        self.assertEqual(snap[-1], {"id": "r2", "repo": "owner2/name2", "removed": True})
        self.apply(feed(RUN3, [repo(1)]))  # still gone: nothing more
        self.assertEqual(self.snapshots("2026-11")[-1]["run_at"], RUN3)
        self.apply(feed("2026-11-11T00:17:00Z", [repo(1), repo(2)]))  # back with the same values: a full line
        self.assertEqual(self.snapshots("2026-11")[-1], history.snapshot_record(repo(2)))


class Months(HistoryTestCase):
    def test_month_boundaries(self):
        late = "2026-11-30T23:17:00Z"
        early = "2026-12-01T05:17:00Z"
        self.apply(feed(late, [repo(1)], [event(1, "repository_discovered", late)]))
        # A December event whose source is in October still goes to the month it was observed in.
        dec = [event(1, "repository_discovered", late), event(2, "release_published:1f", early, source="2026-10-01T00:00:00Z"),
               event(3, "repository_discovered", early)]
        self.apply(feed(early, [repo(1), repo(2), repo(3)], dec))
        self.assertEqual([e["id"] for e in self.events("2026-11")], ["repo-1:repository_discovered"])
        self.assertEqual([e["id"] for e in self.events("2026-12")], ["repo-2:release_published:1f", "repo-3:repository_discovered"])
        self.assertEqual(self.snapshots("2026-11")[0]["run_at"], late)
        self.assertEqual(self.snapshots("2026-12")[0]["run_at"], early)
        # December's snapshot carries only the repositories new since November.
        self.assertEqual([line.get("id") for line in self.snapshots("2026-12")[1:]], ["r2", "r3"])
        self.assertEqual(self.index()["months"], ["2026-11", "2026-12"])

    def test_events_are_sorted_by_source_or_observed_time_then_id(self):
        events = [event(3, "repository_discovered", RUN1), event(2, "release_published:aa", RUN1, source="2026-11-09T00:00:00Z"),
                  event(1, "repository_discovered", RUN1)]
        self.apply(feed(RUN1, [repo(1), repo(2), repo(3)], events))
        self.assertEqual([e["id"] for e in self.events("2026-11")],
                         ["repo-2:release_published:aa", "repo-1:repository_discovered", "repo-3:repository_discovered"])


class Dedup(HistoryTestCase):
    def test_events_are_stored_once(self):
        first = [event(1, "repository_discovered", RUN1)]
        self.apply(feed(RUN1, [repo(1), repo(2)], first))
        code, out, _ = self.apply(feed(RUN2, [repo(1), repo(2)], first + [event(2, "repository_discovered", RUN2)]))
        self.assertEqual(json.loads(out)["events_added"], 1)
        self.assertEqual([e["id"] for e in self.events("2026-11")], ["repo-1:repository_discovered", "repo-2:repository_discovered"])
        # Across a month boundary too: the November event in a December feed is not added again.
        self.apply(feed("2026-12-01T00:17:00Z", [repo(1), repo(2)], first))
        self.assertFalse(os.path.exists(os.path.join(self.hist, "events", "2026-12.jsonl")))

    def test_a_changed_summary_does_not_replace_the_stored_event(self):
        self.apply(feed(RUN1, [repo(1)], [event(1, "repository_discovered", RUN1, summary="first")]))
        self.apply(feed(RUN2, [repo(1)], [event(1, "repository_discovered", RUN1, summary="second")]))
        self.assertEqual(self.events("2026-11")[0]["summary"], "first")


class LegacyImport(HistoryTestCase):
    def setUp(self):
        super().setUp()
        shutil.copy(SITE_FEED, os.path.join(self.data, "legacy.json"))
        with open(SITE_FEED, encoding="utf-8") as fh:
            self.legacy = json.load(fh)

    def imp(self, *extra):
        return self.cli("history", "--import-feed", "data/legacy.json", "--history-dir", "data/history", *extra)

    def test_import_marks_events_and_writes_no_snapshot(self):
        code, out, _ = self.imp("--label", "legacy")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["events_added"], len(self.legacy["events"]))
        stored = [line for name in sorted(os.listdir(os.path.join(self.hist, "events"))) for line in lines(os.path.join(self.hist, "events", name))]
        self.assertEqual(len(stored), len(self.legacy["events"]))
        self.assertTrue(all(line["source"] == "legacy" for line in stored))
        self.assertEqual({line["id"] for line in stored}, {e["id"] for e in self.legacy["events"]})
        self.assertFalse(os.path.exists(os.path.join(self.hist, "snapshots")))
        self.assertIsNone(self.index()["last_run_at"])
        self.assertEqual(self.index()["runs"], 0)
        before = tree(self.hist)
        code, out, _ = self.imp("--label", "legacy")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["events_added"], 0)
        self.assertEqual(tree(self.hist), before)

    def test_a_run_after_the_import_keeps_the_legacy_line(self):
        self.imp("--label", "legacy")
        legacy_event = dict(self.legacy["events"][0])
        repo_id = int(legacy_event["repo_id"][1:])
        run_at = "2026-11-20T00:17:00Z"
        self.apply(feed(run_at, [repo(repo_id)], [dict(legacy_event, summary="from the run")]))
        stored = [line for line in self.events(legacy_event["observed_at"][:7]) if line["id"] == legacy_event["id"]]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["source"], "legacy")

    def test_option_rules(self):
        self.assertEqual(self.imp()[0], 2)
        self.assertEqual(self.imp("--label", "Legacy")[0], 2)
        self.assertEqual(self.imp("--label", "x" * 33)[0], 2)
        self.assertEqual(self.cli("history", "--feed", self.put("f.json", feed(RUN1, [repo(1)])), "--label", "legacy",
                                  "--history-dir", "data/history")[0], 2)
        self.assertEqual(self.cli("history", "--history-dir", "data/history")[0], 2)
        self.assertEqual(self.cli("history", "--feed", "data/legacy.json", "--import-feed", "data/legacy.json", "--history-dir", "data/history")[0], 2)
        self.assertFalse(os.path.exists(self.hist))

    def test_an_old_feed_cannot_be_applied_as_a_run(self):
        code, _, err = self.cli("history", "--feed", "data/legacy.json", "--history-dir", "data/history")
        self.assertEqual(code, 1)
        self.assertIn("no collection object", err)
        self.assertFalse(os.path.exists(self.hist))


class Compaction(HistoryTestCase):
    def fill(self):
        for month in ("2026-07", "2026-08", "2026-09", "2026-10", "2026-11"):
            run_at = month + "-15T06:17:00Z"
            n = int(month[5:])
            self.apply(feed(run_at, [repo(1, stars=n), repo(n)], [event(n, "repository_discovered", run_at)]))

    def test_gzip_round_trip_and_index(self):
        self.fill()
        plain = tree(self.hist)
        code, out, _ = self.apply(feed("2026-11-16T06:17:00Z", [repo(1, stars=11), repo(11)]), extra=("--compact-older-than-months", "2"))
        self.assertEqual(code, 0)
        summary = json.loads(out)
        names = ["events/2026-07.jsonl", "events/2026-08.jsonl", "snapshots/2026-07.jsonl", "snapshots/2026-08.jsonl"]
        self.assertEqual(summary["compacted"], names)
        self.assertEqual(summary["removed"], names)
        after = tree(self.hist)
        index = self.index()
        for name in names:
            self.assertNotIn(name, after)
            gz = name + ".gz"
            self.assertEqual(gzip.decompress(after[gz]), plain[name])
            self.assertTrue(index["files"][gz]["compressed"])
            self.assertEqual(index["files"][gz]["bytes"], len(after[gz]))
            self.assertEqual(index["files"][gz]["sha256"], hashlib.sha256(after[gz]).hexdigest())
            self.assertNotIn(name, index["files"])
        self.assertIn("events/2026-09.jsonl", after)
        self.assertEqual(index["total_bytes"], sum(len(v) for k, v in after.items() if k.startswith(("events/", "snapshots/"))))
        # A later run reads the compacted months: r1 keeps its value, so no line for it; another compaction is a no-op.
        code, out, _ = self.apply(feed("2026-11-17T06:17:00Z", [repo(1, stars=11), repo(11)]), extra=("--compact-older-than-months", "2"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["snapshot_lines"], 0)
        self.assertEqual(json.loads(out)["compacted"], [])

    def test_a_late_event_rewrites_a_compacted_month(self):
        self.fill()
        self.apply(feed("2026-11-16T06:17:00Z", [repo(1, stars=11)]), extra=("--compact-older-than-months", "2"))
        late = event(50, "repository_discovered", "2026-07-20T00:00:00Z")
        self.apply(feed("2026-11-17T06:17:00Z", [repo(1, stars=11), repo(50)], [late]))
        self.assertIn(late, lines(os.path.join(self.hist, "events", "2026-07.jsonl.gz")))

    def test_compaction_is_off_by_default_and_bounded(self):
        self.fill()
        self.assertTrue(all(not name.endswith(".gz") for name in tree(self.hist)))
        for value in ("0", "1201", "x"):
            with self.subTest(value=value):
                self.assertEqual(self.apply(feed("2026-11-20T00:00:00Z", [repo(1)]), extra=("--compact-older-than-months", value))[0], 2)

    def test_gzip_output_is_deterministic(self):
        self.assertEqual(history.gzip_bytes(b"abc\n"), history.gzip_bytes(b"abc\n"))
        self.assertEqual(history.gzip_bytes(b"abc\n")[4:8], b"\0\0\0\0")  # mtime 0


class SizeLimits(HistoryTestCase):
    def test_a_file_over_the_limit_is_refused_and_nothing_is_written(self):
        self.apply(feed(RUN1, [repo(1)], [event(1, "repository_discovered", RUN1)]))
        before = tree(self.hist)
        size = len(before["snapshots/2026-11.jsonl"])
        with unittest.mock.patch.object(history, "MAX_FILE_BYTES", size + 50):
            code, _, err = self.apply(feed(RUN2, [repo(1, stars=2), repo(2)], [event(2, "repository_discovered", RUN2)]))
        self.assertEqual(code, 2)
        self.assertIn("over the per-file limit", err)
        self.assertIn("nothing was written", err)
        self.assertEqual(tree(self.hist), before)

    def test_the_budget_warns_but_writes(self):
        with unittest.mock.patch.object(history, "BUDGET_BYTES", 10):
            code, out, err = self.apply(feed(RUN1, [repo(1)]))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["over_budget"])
        self.assertIn("over the budget", err)


class AtomicWrites(HistoryTestCase):
    def test_files_go_through_the_atomic_writer_and_the_index_is_last(self):
        calls = []
        real = cli._write_bytes

        def recording(data, path, overwrite=False, flag="--out"):
            calls.append((os.path.relpath(path, self.hist), overwrite, flag))
            return real(data, path, overwrite, flag)

        with unittest.mock.patch.object(cli, "_write_bytes", recording):
            self.apply(feed(RUN1, [repo(1)], [event(1, "repository_discovered", RUN1)]))
        self.assertEqual([c[0] for c in calls], ["events/2026-11.jsonl", "snapshots/2026-11.jsonl", "README.md", "index.json"])
        self.assertTrue(all(c[1] and c[2] == "--history-dir" for c in calls))

    def test_a_failed_write_is_found_by_the_next_run(self):
        self.apply(feed(RUN1, [repo(1)], [event(1, "repository_discovered", RUN1)]))
        good = tree(self.hist)
        real = cli._write_bytes

        def failing(data, path, overwrite=False, flag="--out"):
            if path.endswith(os.path.join("snapshots", "2026-11.jsonl")):
                raise OSError("disk full")
            return real(data, path, overwrite, flag)

        with unittest.mock.patch.object(cli, "_write_bytes", failing):
            code, _, err = self.apply(feed(RUN2, [repo(1, stars=3), repo(2)], [event(2, "repository_discovered", RUN2)]))
        self.assertEqual(code, 1)
        self.assertIn("OSError", err)
        broken = tree(self.hist)
        self.assertEqual(broken["index.json"], good["index.json"])
        self.assertEqual(broken["snapshots/2026-11.jsonl"], good["snapshots/2026-11.jsonl"])
        self.assertFalse(any(name.endswith(".tmp") for name in broken))
        code, _, err = self.apply(feed(RUN3, [repo(1, stars=3)]))
        self.assertEqual(code, 1)
        self.assertIn("not a usable history", err)
        self.assertEqual(tree(self.hist), broken)


class PathSafety(HistoryTestCase):
    def setUp(self):
        super().setUp()
        self.feed_arg = self.put("feed.json", feed(RUN1, [repo(1)]))

    def refused(self, *argv):
        code, _, err = self.cli("history", *argv)
        self.assertEqual(code, 2, err)
        self.assertIn("refused", err)

    def test_history_dir_rules(self):
        outside = os.path.join(self.tmp.name, "elsewhere")
        os.mkdir(outside)
        self.refused("--feed", self.feed_arg, "--history-dir", outside)
        self.refused("--feed", self.feed_arg, "--history-dir", "data")
        self.refused("--feed", self.feed_arg, "--history-dir", "data/../history")
        self.refused("--feed", self.feed_arg, "--history-dir", "data/hi\x07st")
        os.symlink(outside, os.path.join(self.data, "linked"))
        self.refused("--feed", self.feed_arg, "--history-dir", "data/linked")
        self.refused("--feed", self.feed_arg, "--history-dir", "data/linked/sub")
        with open(os.path.join(self.data, "plain"), "w") as fh:
            fh.write("x")
        self.refused("--feed", self.feed_arg, "--history-dir", "data/plain")
        self.assertEqual(os.listdir(outside), [])

    def test_feed_rules(self):
        outside = os.path.join(self.tmp.name, "feed.json")
        with open(outside, "w") as fh:
            json.dump(feed(RUN1, [repo(1)]), fh)
        self.refused("--feed", outside, "--history-dir", "data/history")
        self.refused("--feed", "https://example.com/feed.json", "--history-dir", "data/history")
        os.symlink(outside, os.path.join(self.data, "link.json"))
        self.refused("--feed", "data/link.json", "--history-dir", "data/history")
        with open(os.path.join(self.data, "feed.txt"), "w") as fh:
            fh.write("{}")
        self.refused("--feed", "data/feed.txt", "--history-dir", "data/history")
        self.refused("--feed", "data/missing.json", "--history-dir", "data/history")
        self.assertEqual(self.cli("history", "--feed", self.feed_arg, "--history-dir", "data/history")[0], 0)
        before = tree(self.hist)
        code, _, err = self.cli("history", "--feed", "data/history/index.json", "--history-dir", "data/history")
        self.assertEqual(code, 2)
        self.assertIn("--feed lies inside --history-dir", err)
        self.assertEqual(tree(self.hist), before)
        with unittest.mock.patch.object(history, "MAX_FEED_BYTES", 10):
            self.refused("--feed", self.feed_arg, "--history-dir", "data/other")

    def test_a_linked_history_dir_is_refused_before_it_is_read(self):
        self.assertEqual(self.cli("history", "--feed", self.feed_arg, "--history-dir", "data/real")[0], 0)
        os.symlink(os.path.join(self.data, "real"), os.path.join(self.data, "linked"))
        # The same run again would be a no-op that writes nothing: the link must still be refused.
        code, _, err = self.cli("history", "--feed", self.feed_arg, "--history-dir", "data/linked")
        self.assertEqual(code, 2)
        self.assertIn("--history-dir path contains a symbolic link", err)

    def test_history_contents_rules(self):
        self.assertEqual(self.cli("history", "--feed", self.feed_arg, "--history-dir", "data/history")[0], 0)
        target = os.path.join(self.hist, "snapshots", "2026-11.jsonl")
        moved = os.path.join(self.tmp.name, "moved.jsonl")
        shutil.move(target, moved)
        os.symlink(moved, target)
        before = tree(self.hist)
        self.refused("--feed", self.put("f2.json", feed(RUN2, [repo(1)])), "--history-dir", "data/history")
        os.unlink(target)
        shutil.move(moved, target)
        for extra in ("notes.txt", "events/2026-11.json", "snapshots/2026-11.jsonl.tmp", "events/x"):
            with self.subTest(extra=extra):
                path = os.path.join(self.hist, *extra.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fh:
                    fh.write("x\n")
                self.refused("--feed", self.put("f2.json", feed(RUN2, [repo(1)])), "--history-dir", "data/history")
                os.unlink(path)
        os.rmdir(os.path.join(self.hist, "events"))
        os.symlink(self.tmp.name, os.path.join(self.hist, "events"))
        self.refused("--feed", self.put("f2.json", feed(RUN2, [repo(1)])), "--history-dir", "data/history")
        os.unlink(os.path.join(self.hist, "events"))
        self.assertEqual(sorted(tree(self.hist)), sorted(before))
        self.assertEqual(self.cli("history", "--feed", self.put("f2.json", feed(RUN2, [repo(1)])), "--history-dir", "data/history")[0], 0)


class ExitCodes(HistoryTestCase):
    def test_invalid_feeds_are_errors(self):
        cases = {
            "not json": "{not json",
            "list": [],
            "unknown key": dict(feed(RUN1, [repo(1)]), extra=1),
            "unknown repo key": feed(RUN1, [dict(repo(1), email="x")]),
            "bad url": feed(RUN1, [dict(repo(1), url="https://example.com/owner1/name1")]),
            "duplicate repo": feed(RUN1, [repo(1), repo(1)]),
            "duplicate event": feed(RUN1, [repo(1)], [event(1, "repository_discovered", RUN1)] * 2),
            "orphan event": feed(RUN1, [repo(1)], [event(2, "repository_discovered", RUN1)]),
            "event from the future": feed(RUN1, [repo(1)], [event(1, "repository_discovered", RUN2)]),
            "collection mismatch": dict(feed(RUN1, [repo(1)]), generated_at=RUN2),
            "float": feed(RUN1, [dict(repo(1), stars=1.5)]),
        }
        for label, document in cases.items():
            with self.subTest(case=label):
                path = os.path.join(self.data, "bad.json")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(document if isinstance(document, str) else json.dumps(document))
                code, _, err = self.cli("history", "--feed", "data/bad.json", "--history-dir", "data/history")
                self.assertEqual(code, 1, label)
                self.assertTrue(err.startswith("error: data/bad.json is not a usable feed"), err)
                self.assertNotIn(self.repo, err)
                self.assertFalse(os.path.exists(self.hist))

    def test_a_tampered_history_is_an_error(self):
        self.apply(feed(RUN1, [repo(1), repo(2)], [event(1, "repository_discovered", RUN1)]))
        good = tree(self.hist)
        edits = {
            "snapshots/2026-11.jsonl": lambda b: b.replace(b'"stars":1', b'"stars":7', 1),
            "events/2026-11.jsonl": lambda b: b.replace(b"{", b"{ ", 1),
            "index.json": lambda b: b.replace(b'"runs": 1', b'"runs": 2'),
        }
        for name, edit in edits.items():
            with self.subTest(name=name):
                path = os.path.join(self.hist, *name.split("/"))
                with open(path, "wb") as fh:
                    fh.write(edit(good[name]))
                code, _, err = self.apply(feed(RUN2, [repo(1)]))
                self.assertEqual(code, 1)
                self.assertIn("not a usable history", err)
                with open(path, "wb") as fh:
                    fh.write(good[name])
        os.unlink(os.path.join(self.hist, "index.json"))
        self.assertEqual(self.apply(feed(RUN2, [repo(1)]))[0], 1)

    def test_a_line_out_of_canonical_form_is_an_error_even_with_a_matching_index(self):
        self.apply(feed(RUN1, [repo(1)], [event(1, "repository_discovered", RUN1)]))
        name = "events/2026-11.jsonl"
        path = os.path.join(self.hist, *name.split("/"))
        with open(path, "rb") as fh:
            data = fh.read().replace(b"{", b"{ ", 1)
        with open(path, "wb") as fh:
            fh.write(data)
        index = self.index()
        index["files"][name].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        index["total_bytes"] = sum(entry["bytes"] for entry in index["files"].values())
        with open(os.path.join(self.hist, "index.json"), "wb") as fh:
            fh.write(history.document_bytes(index))
        code, _, err = self.apply(feed(RUN2, [repo(1)]))
        self.assertEqual(code, 1)
        self.assertIn("not in canonical form", err)

    def test_a_history_of_another_format_is_an_error(self):
        self.apply(feed(RUN1, [repo(1)]))
        path = os.path.join(self.hist, "index.json")
        with open(path, "rb") as fh:
            data = fh.read()
        with open(path, "wb") as fh:
            fh.write(data.replace(b'"format_version": 1', b'"format_version": 2'))
        code, _, err = self.apply(feed(RUN2, [repo(1)]))
        self.assertEqual(code, 1)
        self.assertIn("format_version 2 is not supported", err)


class Determinism(HistoryTestCase):
    SEQUENCE = [
        feed("2026-10-31T18:17:00Z", [repo(3), repo(1), repo(2)], [event(1, "repository_discovered", "2026-10-31T18:17:00Z")]),
        feed("2026-11-01T00:17:00Z", [repo(1, stars=4), repo(3)], [event(3, "repository_discovered", "2026-11-01T00:17:00Z"),
                                                                    event(1, "repository_discovered", "2026-10-31T18:17:00Z")]),
        feed("2026-11-01T06:17:00Z", [repo(1, stars=4), repo(2), repo(3)], [], stopped="budget"),
    ]

    def run_sequence(self, hist):
        for i, document in enumerate(self.SEQUENCE):
            code, _, err = self.cli("history", "--feed", self.put("seq-%d.json" % i, document), "--history-dir", hist)
            self.assertEqual(code, 0, err)
        return tree(os.path.join(self.repo, hist))

    def test_same_inputs_same_bytes(self):
        first = self.run_sequence("data/a")
        second = self.run_sequence("data/b")
        self.assertEqual(first, second)
        for name, data in first.items():
            self.assertTrue(data.endswith(b"\n"), name)
            data.decode("ascii")
        index = json.loads(first["index.json"])
        self.assertEqual(first["index.json"], (json.dumps(index, indent=1, sort_keys=True) + "\n").encode())

    def test_hash_seed_does_not_matter(self):
        expected = self.run_sequence("data/c")
        script = (
            "import sys, unittest.mock\n"
            "sys.path.insert(0, %r)\n"
            "from collector import cli\n"
            "repo = sys.argv[1]\n"
            "with unittest.mock.patch.object(cli, 'REPO_ROOT', repo), unittest.mock.patch.object(cli, 'DATA_ROOT', repo + '/data'):\n"
            "    for i in range(3):\n"
            "        rc = cli.main(['history', '--feed', 'data/seq-%%d.json' %% i, '--history-dir', 'data/d'])\n"
            "        assert rc == 0, rc\n" % REPO_ROOT
        )
        for seed in ("0", "12345"):
            with self.subTest(seed=seed):
                shutil.rmtree(os.path.join(self.data, "d"), ignore_errors=True)
                env = dict(os.environ, PYTHONHASHSEED=seed)
                subprocess.run([sys.executable, "-B", "-c", script, self.repo], check=True, env=env, capture_output=True)
                self.assertEqual(tree(os.path.join(self.data, "d")), expected)


class NoPrivateText(HistoryTestCase):
    HOSTILE_SUMMARY = "v1 by someone@example.org, ghp_abcdefghijklmnopqrstuvwxyz0123456789 see http://evil.example/x and https://github.com/a/b"

    def hostile_feed(self):
        events = [event(1, "release_published:1a", RUN1, summary=self.HOSTILE_SUMMARY)]
        return feed(RUN1, [repo(1, description="mail me at someone@example.org or ghp_abcdefghijklmnopqrstuvwxyz0123")], events)

    def test_no_email_token_or_foreign_url_is_stored(self):
        code, _, err = self.apply(self.hostile_feed())
        self.assertEqual(code, 0, err)
        for name, data in tree(self.hist).items():
            with self.subTest(name=name):
                text = data.decode("ascii")
                self.assertNotIn("@", text)
                self.assertNotIn("ghp_", text)
                self.assertNotIn("http://", text)
                self.assertNotIn("example", text)
                self.assertNotIn("mail me", text)
        summary = self.events("2026-11")[0]["summary"]
        self.assertEqual(summary, "v1 by [removed], [removed] see [removed] and https://github.com/a/b")
        self.assertEqual(set(self.snapshots("2026-11")[1]), set(history.SNAPSHOT_FIELDS))

    def test_the_final_scan_stops_a_leak(self):
        with unittest.mock.patch.object(history, "clean_summary", lambda value: value):
            code, _, err = self.apply(self.hostile_feed())
        self.assertEqual(code, 1)
        self.assertIn("nothing was written", err)
        self.assertNotIn("someone", err)
        self.assertFalse(os.path.exists(self.hist))

    def test_unsafe_text(self):
        self.assertIsNone(history.unsafe_text(b'{"summary":"fork of a/b first seen","url":"https://github.com/a/b"}'))
        for data in (b"x@y.io", b"github_pat_0123456789abcdef", b"ghs_0123456789ab", b"ftp://h/x", b"https://github.com.evil/x"):
            with self.subTest(data=data):
                self.assertIsNotNone(history.unsafe_text(data))


class RealFeeds(HistoryTestCase):
    def test_export_output_is_accepted(self):
        runs = []
        for i in range(len(scenario_files("new_activity"))):
            runs.append(self.put("run-%02d.json" % (i + 1), load_run("new_activity", i + 1)))
        for i, run in enumerate(runs):
            extra = ["--state-in", "data/state.json"] if i else []
            code, _, err = self.cli("export", "--runs", run, "--out-dir", "data/site", "--overwrite", "--state-out", "data/state.json", *extra)
            self.assertEqual(code, 0, err)
            code, out, err = self.cli("history", "--feed", "data/site/feed.json", "--history-dir", "data/history")
            self.assertEqual(code, 0, err)
        with open(os.path.join(self.data, "site", "feed.json"), encoding="utf-8") as fh:
            exported = json.load(fh)
        stored = {e["id"] for name in os.listdir(os.path.join(self.hist, "events")) for e in lines(os.path.join(self.hist, "events", name))}
        self.assertLessEqual({e["id"] for e in exported["events"]}, stored)
        self.assertEqual(self.index()["runs"], len(runs))

    def test_the_committed_feed_is_a_valid_import(self):
        with open(SITE_FEED, encoding="utf-8") as fh:
            document = json.load(fh)
        history.validate_feed(copy.deepcopy(document), require_collection=False)


if __name__ == "__main__":
    unittest.main()

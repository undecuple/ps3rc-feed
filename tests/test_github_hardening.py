"""Regression tests for the step 3 review findings (K1-K10).

No network: tests/helpers.py blocks every socket connect and name lookup at
import. The live transport is exercised only with a fake connection class.
"""

import copy
import http.client
import io
import json
import os
import tempfile
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from helpers import CollectorTestCase, run_runs
from test_github import RUN_AT, SENTINEL, SKY, _run_cli, _snapshot, api_commit, api_repo, api_release, config_for_network, make_client, ok, routes_for_network, search_url, sha

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMITS_100 = "GET /repos/game-org/skyfall-recomp/commits?page=1&per_page=100&sha=main&since=2026-10-06T00%3A00%3A00Z"
RELEASES_100 = "GET /repos/game-org/skyfall-recomp/releases?per_page=30"
README_100 = "GET /repos/game-org/skyfall-recomp/readme"


def collect_with(routes, config=None, **client_kw):
    from collector import collect

    client, transport = make_client(routes, **client_kw)
    run, report = collect.collect(client, config or config_for_network(), RUN_AT)
    return run, report, client, transport


def repo_100(run):
    return [r for r in run["repositories"] if r["repository_id"] == 100][0]


# --- a fake http.client connection ------------------------------------------------


class FakeHTTPResponse:
    def __init__(self, status=200, headers=None, body=b"{}", chunks=None, read_error=None):
        self.status = status
        self._headers = headers or {}
        self._body = body
        self._pos = 0
        self._chunks = chunks
        self._read_error = read_error
        self.reads = 0

    def getheaders(self):
        return list(self._headers.items())

    def read(self, amt=None):
        self.reads += 1
        if self._read_error is not None:
            raise self._read_error
        if self._chunks is not None:
            if not self._chunks:
                return b""
            if amt is None or amt >= len(self._chunks[0]):
                return self._chunks.pop(0)
            head, self._chunks[0] = self._chunks[0][:amt], self._chunks[0][amt:]
            return head
        if amt is None:
            amt = len(self._body) - self._pos
        chunk = self._body[self._pos : self._pos + amt]
        self._pos += len(chunk)
        return chunk


class FakeConnection:
    """Stands in for http.client.HTTPSConnection. Records what the transport sends."""

    instances = []
    next_response = None
    connect_error = None

    def __init__(self, host, port=None, timeout=None, context=None, **kw):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.extra = kw
        self.requests = []
        self.closed = False
        FakeConnection.instances.append(self)

    def request(self, method, url, body=None, headers=None):
        if FakeConnection.connect_error is not None:
            raise FakeConnection.connect_error
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {})})

    def getresponse(self):
        response = FakeConnection.next_response
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


def fake_live(response, **kw):
    from collector import github

    FakeConnection.instances = []
    FakeConnection.next_response = response
    FakeConnection.connect_error = None
    return github.LiveTransport(connection_factory=FakeConnection, **kw)


class K1TokenHygiene(CollectorTestCase):
    def test_secret_rejects_malformed_tokens_without_echoing_them(self):
        from collector import github

        for bad in ("SENTINEL\r", "SENTINEL\n", "SENT INEL", "SENTINELé", "", "x" * 4097, "tok:en", "a/b", "\tSENTINEL"):
            with self.assertRaises(ValueError, msg=repr(bad)) as ctx:
                github.Secret(bad)
            self.assertNotIn("SENTINEL", str(ctx.exception))
            self.assertNotIn(bad.strip() or "EMPTY", str(ctx.exception))
        token = github.Secret("ghp_" + "A" * 36)
        self.assertEqual(token.reveal(), "ghp_" + "A" * 36)
        self.assertNotIn("ghp_", repr(token) + str(token))

    def test_cli_refuses_a_malformed_token_without_echoing_it(self):
        from collector import github

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(config_for_network(), fh)
            with unittest.mock.patch.object(github, "LiveTransport", side_effect=AssertionError("live transport constructed")):
                for bad in (SENTINEL + "\r", SENTINEL + "\n", SENTINEL + " x", SENTINEL + "é"):
                    code, out, err = _run_cli(["collect", "--config", config_path, "--live", "--max-requests", "3", "--out", os.path.join(REPO_ROOT, "data", "never.json")], env={"GITHUB_TOKEN": bad})
                    self.assertEqual(code, 2, repr(bad))
                    self.assertIn("GITHUB_TOKEN", err)
                    self.assertNotIn("SENTINEL", out + err)

    def test_transport_exception_text_is_never_printed(self):
        from collector import github

        transport = fake_live(OSError("leak " + SENTINEL))
        client = github.Client(transport, token=github.Secret(SENTINEL), max_requests=3)
        with self.assertRaises(github.TransportFailed) as ctx:
            client.get_json("/repositories/90", None, expect="object")
        self.assertEqual(str(ctx.exception), "transport failure (OSError) for /repositories/90")
        self.assertNotIn(SENTINEL, str(ctx.exception) + repr(ctx.exception))
        self.assertEqual(ctx.exception.kind, "OSError")


class K2OneBadResponseDoesNotAbortTheRun(CollectorTestCase):
    def _with(self, key, response, **client_kw):
        routes = routes_for_network()
        routes[key] = response
        return collect_with(routes, **client_kw)

    def test_sub_request_failures_leave_the_field_null_and_the_run_continues(self):
        from collector import github, schema

        cases = [
            (README_100, {"status": 200, "headers": {}, "body": "A" * (github.MAX_BODY + 1)}, "readme_excerpt", "response"),
            (COMMITS_100, {"status": 200, "headers": {}, "body": "[" + "1," * (github.MAX_BODY // 2) + "1]"}, "commits", "response"),
            (RELEASES_100, ok({"message": "x"}), "releases", "response"),
            (COMMITS_100, {"status": 301, "headers": {}, "body": ""}, "commits", "response"),
            (RELEASES_100, {"status": 200, "headers": {}, "body": "<html>"}, "releases", "response"),
            (README_100, {"raise": "TimeoutError"}, "readme_excerpt", "transport"),
            (COMMITS_100, {"raise": "RemoteDisconnected"}, "commits", "transport"),
        ]
        for key, response, field, kind in cases:
            run, report, client, _ = self._with(key, response)
            schema.validate_run(copy.deepcopy(run))
            r100 = repo_100(run)
            self.assertEqual(r100["fetch_state"], "ok", key)
            self.assertIsNone(r100[field], key)
            self.assertIsNone(report["stopped"], key)
            self.assertEqual([e["reason"] for e in report["errors"]], [kind], key)
            self.assertEqual(report["error_count"], 1)
            self.assertIs(run["collection_report"]["complete"], False)
            self.assertEqual(run["collection_report"]["error_count"], 1)
            # The other repository is intact.
            self.assertEqual([r for r in run["repositories"] if r["repository_id"] == 90][0]["readme_excerpt"], "# Fictional\n")

    def test_metadata_failures_mark_the_repository_failed_and_continue(self):
        from collector import schema

        for response in (
            {"status": 301, "headers": {"location": "https://api.github.com/repositories/12"}, "body": ""},
            {"status": 200, "headers": {}, "body": "<html>"},
            {"status": 200, "headers": {}, "body": '{"a": 1, "a": 2}'},
            ok([1, 2]),
            ok({"id": 100, "full_name": SKY}),  # missing required owner is fine (nullable) but fork/archived null ok; test shape below
            {"raise": "ConnectionResetError"},
        ):
            run, report, _, _ = self._with("GET /repositories/100", response)
            schema.validate_run(copy.deepcopy(run))
            r100 = repo_100(run)
            if response == ok({"id": 100, "full_name": SKY}):
                self.assertEqual(r100["fetch_state"], "ok")
                continue
            self.assertEqual(r100["fetch_state"], "failed", response)
            self.assertIsNone(r100["stars"])
            self.assertIsNone(report["stopped"])
            self.assertEqual(len(run["repositories"]), 2)

    def test_missing_required_fields_are_shape_errors_and_dropped_items_are_counted(self):
        from collector import collect, schema

        with self.assertRaises(collect.ShapeError):
            collect._field({"a": 1}, "id", (int,), "/x")
        with self.assertRaises(collect.ShapeError):
            collect._field({"id": None}, "id", (int,), "/x")
        self.assertIsNone(collect._field({}, "description", (str,), "/x", nullable=True))
        with self.assertRaises(collect.ShapeError):
            collect._id({"id": 0}, "id", "/x")
        with self.assertRaises(collect.ShapeError):
            collect._id({"id": 2**53 + 1}, "id", "/x")
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = ok({"total_count": 3, "incomplete_results": False, "items": [api_repo(90, "frame-org/ps3recomp-example"), api_repo(100, SKY), {"full_name": "c/three"}]})
        routes[RELEASES_100] = ok([api_release(7001, "v0.1.0"), {"tag_name": "v9", "published_at": "2026-11-01T00:00:00Z", "draft": False}])
        routes["GET /repositories/100"] = ok(api_repo(100, SKY, fork=True, parent=90, source=90, stars=2**60))
        run, report, _, _ = collect_with(routes)
        schema.validate_run(copy.deepcopy(run))
        self.assertIs(run["searches"][0]["complete"], False)
        self.assertEqual(run["searches"][0]["repository_ids"], [90, 100])
        self.assertEqual(report["dropped_items"], 2)
        r100 = repo_100(run)
        self.assertEqual(r100["releases"], [{"id": 7001, "tag": "v0.1.0", "published_at": "2026-11-04T10:00:00Z"}])
        self.assertIsNone(r100["stars"])
        self.assertEqual(r100["fetch_state"], "ok")
        self.assertIn({"path": "/repositories/100", "reason": "range:stargazers_count"}, report["errors"])
        self.assertIsNone(report["stopped"])


class K3TransportFailuresAndNetworkStop(CollectorTestCase):
    def test_live_transport_maps_every_exception_to_transport_failed(self):
        from collector import github

        for exc in (
            OSError("x"),
            TimeoutError("x"),
            http.client.RemoteDisconnected("x"),
            http.client.IncompleteRead(b"partial"),
            http.client.InvalidURL("x"),
            ConnectionResetError("x"),
            __import__("ssl").SSLError("x"),
            RuntimeError("anything"),
        ):
            transport = fake_live(exc)
            with self.assertRaises(github.TransportFailed, msg=type(exc).__name__) as ctx:
                transport("GET", "https://api.github.com/repositories/90", {}, 5)
            self.assertEqual(str(ctx.exception), "transport failure (%s) for /repositories/90" % type(exc).__name__)
            self.assertNotIn("x", str(ctx.exception).replace("transport failure", "").replace("IncompleteRead", "").replace("InvalidURL", "").split(")")[1])

    def test_three_consecutive_transport_failures_stop_the_run_as_network(self):
        from collector import github, schema

        routes = routes_for_network()
        for key in ("GET /repositories/90", "GET /repositories/100"):
            routes[key] = {"raise": "TimeoutError"}
        routes["GET /repos/frame-org/ps3recomp-example/forks?page=1&per_page=100"] = {"raise": "OSError"}
        run, report, client, transport = collect_with(routes)
        schema.validate_run(copy.deepcopy(run))
        self.assertEqual(report["stopped"], "network")
        self.assertIs(client.network_failed, True)
        self.assertEqual(client.transport_failures, 3)
        self.assertEqual(len(transport.calls), 4)  # search, forks (fail 1), repo 90 (fail 2), repo 100 (fail 3)
        self.assertEqual(run["roots"][0]["forks_scan_complete"], False)
        by_id = {r["repository_id"]: r for r in run["repositories"]}
        self.assertEqual(by_id[90]["fetch_state"], "failed")
        self.assertEqual(by_id[100]["fetch_state"], "failed")
        self.assertIsNone(by_id[100]["stars"])
        self.assertEqual(run["collection_report"]["stopped"], "network")
        with self.assertRaises(github.NetworkFailed):
            client.get_json("/repositories/90", None, expect="object")
        self.assertEqual(len(transport.calls), 4)
        # A success in between resets the streak.
        routes = routes_for_network()
        routes[README_100] = {"raise": "OSError"}
        routes["GET /repos/frame-org/ps3recomp-example/readme"] = {"raise": "OSError"}
        run, report, client, _ = collect_with(routes)
        self.assertIsNone(report["stopped"])
        self.assertEqual(client.transport_failures, 2)
        # The metadata success of repository 100 reset the streak; only its README failure remains.
        self.assertEqual(client.consecutive_failures, 1)
        self.assertIs(client.network_failed, False)

    def test_cli_never_prints_a_traceback_or_exception_text(self):
        from collector import collect

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(config_for_network(), fh)
            with unittest.mock.patch.object(collect, "plan_requests", side_effect=RuntimeError("secret detail " + SENTINEL)):
                code, out, err = _run_cli(["collect", "--config", config_path])
            self.assertEqual(code, 1)
            self.assertEqual(err, "error: collection failed (RuntimeError)\n")
            self.assertEqual(out, "")


class K4TruncatedBodies(CollectorTestCase):
    def test_short_body_against_content_length_is_a_failure(self):
        from collector import github

        transport = fake_live(FakeHTTPResponse(200, {"Content-Length": "100"}, b"{" + b" " * 10))
        with self.assertRaises(github.TransportFailed) as ctx:
            transport("GET", "https://api.github.com/repositories/90", {}, 5)
        self.assertIn("IncompleteRead", str(ctx.exception))
        transport = fake_live(FakeHTTPResponse(200, {"Content-Length": "2"}, b"{}"))
        response = transport("GET", "https://api.github.com/repositories/90", {}, 5)
        self.assertEqual((response.status, response.body), (200, b"{}"))
        transport = fake_live(FakeHTTPResponse(200, {}, b"", read_error=http.client.IncompleteRead(b"x")))
        with self.assertRaises(github.TransportFailed):
            transport("GET", "https://api.github.com/repositories/90", {}, 5)

    def test_read_deadline_is_enforced_across_chunks(self):
        from collector import github

        clock = {"now": 0.0}

        def slow_read(amt=None):
            clock["now"] += 11.0
            return b"x"

        response = FakeHTTPResponse(200, {}, b"")
        response.read = slow_read
        transport = fake_live(response, deadline=20)
        with unittest.mock.patch.object(github.time, "monotonic", side_effect=lambda: clock["now"]):
            with self.assertRaises(github.TransportFailed) as ctx:
                transport("GET", "https://api.github.com/repositories/90", {}, 5)
        self.assertIn("TimeoutError", str(ctx.exception))


class K5PaginationKeepsFetchedPages(CollectorTestCase):
    def _two_pages(self, second):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = ok(
            {"total_count": 3, "incomplete_results": False, "items": [api_repo(90, "frame-org/ps3recomp-example"), api_repo(100, SKY)]},
            {"link": '<https://api.github.com/search/repositories?page=2&per_page=100&q=ps3recomp+in%3Aname>; rel="next"'},
        )
        routes[search_url("ps3recomp in:name", 2)] = second
        return routes

    def test_rate_limit_on_page_two_keeps_page_one(self):
        from collector import schema

        run, report, client, transport = collect_with(self._two_pages({"status": 403, "headers": {"x-ratelimit-remaining": "0"}, "body": "{}"}))
        schema.validate_run(copy.deepcopy(run))
        self.assertEqual(report["stopped"], "rate_limited")
        # An interrupted search keeps what page one listed and says where to resume.
        self.assertEqual(run["searches"], [{"query": "ps3recomp in:name", "complete": False, "repository_ids": [90, 100], "resume_cursor": "page=2"}])
        self.assertEqual(run["roots"], [])
        self.assertEqual(run["repositories"], [])
        self.assertEqual(report["not_fetched_repository_ids"], [90, 100])
        self.assertEqual(len(transport.calls), 2)

    def test_budget_of_one_keeps_page_one(self):
        run, report, client, _ = collect_with(self._two_pages(ok({"total_count": 3, "incomplete_results": False, "items": []})), max_requests=1)
        self.assertEqual(report["stopped"], "budget")
        self.assertEqual(run["searches"][0]["repository_ids"], [90, 100])
        self.assertIs(run["searches"][0]["complete"], False)
        self.assertEqual(report["not_fetched_repository_ids"], [90, 100])
        self.assertEqual(run["collection_report"]["not_fetched"], 2)

    def test_forks_walk_stopped_mid_way_keeps_found_ids_and_a_cursor(self):
        routes = routes_for_network()
        routes["GET /repos/frame-org/ps3recomp-example/forks?page=1&per_page=100"] = ok(
            [api_repo(100, SKY, fork=True, parent=90, source=90)],
            {"link": '<https://api.github.com/repos/frame-org/ps3recomp-example/forks?page=2&per_page=100>; rel="next"'},
        )
        routes["GET /repos/frame-org/ps3recomp-example/forks?page=2&per_page=100"] = {"raise": "TimeoutError"}
        run, report, _, _ = collect_with(routes)
        root = run["roots"][0]
        self.assertEqual(root["found_repository_ids"], [100])
        self.assertIs(root["forks_scan_complete"], False)
        self.assertEqual(root["resume_cursor"], "page=2")
        self.assertIsNone(report["stopped"])
        self.assertEqual(report["errors"][0]["reason"], "transport")
        self.assertEqual(sorted(r["repository_id"] for r in run["repositories"]), [90, 100])


class K6LiveWrite(CollectorTestCase):
    def _config(self, tmp):
        path = os.path.join(tmp, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config_for_network(), fh)
        return path

    def test_symlinked_data_dir_or_out_file_is_refused_and_nothing_is_written(self):
        from collector import cli, github

        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._config(tmp)
            victim = os.path.join(tmp, "victim.json")
            with open(victim, "w", encoding="utf-8") as fh:
                fh.write("untouched\n")
            fake_repo = os.path.join(tmp, "repo")
            os.makedirs(fake_repo)
            os.symlink(os.path.join(tmp, "outside"), os.path.join(fake_repo, "data"))
            os.makedirs(os.path.join(tmp, "outside"))
            with unittest.mock.patch.object(cli, "REPO_ROOT", fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", os.path.join(fake_repo, "data")):
                with unittest.mock.patch.object(github, "LiveTransport", side_effect=AssertionError("live transport constructed")):
                    code, out, err = _run_cli(["collect", "--config", config_path, "--live", "--max-requests", "5", "--out", os.path.join(fake_repo, "data", "t.json")], env={"GITHUB_TOKEN": SENTINEL})
                self.assertEqual(code, 2, err)
                self.assertIn("symbolic link", err)
            os.unlink(os.path.join(fake_repo, "data"))
            os.makedirs(os.path.join(fake_repo, "data"))
            os.symlink(victim, os.path.join(fake_repo, "data", "t.json"))
            with unittest.mock.patch.object(cli, "REPO_ROOT", fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", os.path.join(fake_repo, "data")):
                with unittest.mock.patch.object(github, "LiveTransport", side_effect=AssertionError("live transport constructed")):
                    code, out, err = _run_cli(["collect", "--config", config_path, "--live", "--max-requests", "5", "--out", os.path.join(fake_repo, "data", "t.json")], env={"GITHUB_TOKEN": SENTINEL})
                self.assertEqual(code, 2, err)
                self.assertIn("symbolic link", err)
            with open(victim, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "untouched\n")
            # A pre-existing .tmp symlink is never followed: the temp name is random and exclusive.
            os.unlink(os.path.join(fake_repo, "data", "t.json"))
            os.symlink(victim, os.path.join(fake_repo, "data", "t.json.tmp"))
            run, _ = run_runs([])
            with unittest.mock.patch.object(cli, "REPO_ROOT", fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", os.path.join(fake_repo, "data")):
                cli.write_run_file({"run_at": RUN_AT, "roots": [], "searches": [], "repositories": []}, os.path.join(fake_repo, "data", "t.json"))
            with open(victim, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "untouched\n")
            self.assertEqual(oct(os.stat(os.path.join(fake_repo, "data", "t.json")).st_mode & 0o777), "0o644")
            self.assertEqual(sorted(os.listdir(os.path.join(fake_repo, "data"))), ["t.json", "t.json.tmp"])

    def test_live_run_writes_the_report_and_exit_code_says_partial(self):
        from collector import cli, github, schema

        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._config(tmp)
            fake_repo = os.path.join(tmp, "repo")
            os.makedirs(fake_repo)
            routes = routes_for_network()
            holder = {}

            def fake_live_factory(*args, **kwargs):
                holder["transport"] = github.RecordedTransport(routes)
                return holder["transport"]

            out_path = os.path.join(fake_repo, "data", "t.json")
            with unittest.mock.patch.object(cli, "REPO_ROOT", fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", os.path.join(fake_repo, "data")):
                with unittest.mock.patch.object(github, "LiveTransport", fake_live_factory):
                    code, out, err = _run_cli(["collect", "--config", config_path, "--live", "--max-requests", "20", "--out", out_path, "--run-at", RUN_AT], env={"GITHUB_TOKEN": SENTINEL})
                    self.assertEqual(code, 0, err)
                    written = schema.load_run_file(out_path)
                    self.assertIs(written["collection_report"]["complete"], True)
                    self.assertEqual(written["collection_report"]["requests_made"], 10)
                    self.assertEqual(holder["transport"].calls[0]["headers"]["Authorization"], "Bearer " + SENTINEL)
                    self.assertNotIn(SENTINEL, out + err)
                    # A relative --out resolves against the repository root.
                    code, out, err = _run_cli(["collect", "--config", config_path, "--live", "--max-requests", "20", "--out", "data/rel.json", "--run-at", RUN_AT], env={"GITHUB_TOKEN": SENTINEL})
                    self.assertEqual(code, 0, err)
                    self.assertTrue(os.path.exists(os.path.join(fake_repo, "data", "rel.json")))
                    # Partial: budget stops the run -> report says so, exit 3, file still valid.
                    # The target exists from the first run, so --overwrite is required.
                    code, out, err = _run_cli(["collect", "--config", config_path, "--live", "--max-requests", "4", "--out", out_path, "--overwrite", "--run-at", RUN_AT], env={"GITHUB_TOKEN": SENTINEL})
                    self.assertEqual(code, 3, err)
                    written = schema.load_run_file(out_path)
                    self.assertEqual(written["collection_report"]["stopped"], "budget")
                    self.assertIs(written["collection_report"]["complete"], False)
                    self.assertEqual(json.loads(out)["stopped"], "budget")
            state, events = run_runs([written])
            self.assertEqual(events, [[]])

    def test_out_must_be_json_and_max_requests_bounded(self):
        from collector import github

        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._config(tmp)
            with unittest.mock.patch.object(github, "LiveTransport", side_effect=AssertionError("live transport constructed")):
                for argv, needle in (
                    (["--out", os.path.join(REPO_ROOT, "data", "t.txt"), "--max-requests", "5"], ".json"),
                    (["--out", os.path.join(REPO_ROOT, "data", "t.json"), "--max-requests", "0"], "between 1 and"),
                    (["--out", os.path.join(REPO_ROOT, "data", "t.json"), "--max-requests", "-4"], "between 1 and"),
                    (["--out", os.path.join(REPO_ROOT, "data", "t.json"), "--max-requests", "5001"], "between 1 and"),
                    (["--out", "../outside/t.json", "--max-requests", "5"], "data/"),
                    (["--out", "t.json", "--max-requests", "5"], "data/"),
                ):
                    code, out, err = _run_cli(["collect", "--config", config_path, "--live"] + argv, env={"GITHUB_TOKEN": SENTINEL})
                    self.assertEqual(code, 2, (argv, err))
                    self.assertIn(needle, err, argv)
                code, out, err = _run_cli(["collect", "--config", config_path, "--max-requests", "0"], env={})
                self.assertEqual(code, 2)


class K7Anonymous(CollectorTestCase):
    def test_anonymous_ignores_the_environment_token(self):
        from collector import cli, github

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(config_for_network(), fh)
            fake_repo = os.path.join(tmp, "repo")
            os.makedirs(fake_repo)
            holder = {}

            def fake_live_factory(*args, **kwargs):
                holder["transport"] = github.RecordedTransport(routes_for_network())
                return holder["transport"]

            with unittest.mock.patch.object(cli, "REPO_ROOT", fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", os.path.join(fake_repo, "data")):
                with unittest.mock.patch.object(github, "LiveTransport", fake_live_factory):
                    code, out, err = _run_cli(["collect", "--config", config_path, "--live", "--anonymous", "--max-requests", "20", "--out", os.path.join(fake_repo, "data", "a.json"), "--run-at", RUN_AT], env={"GITHUB_TOKEN": SENTINEL})
            self.assertEqual(code, 0, err)
            for call in holder["transport"].calls:
                self.assertNotIn("Authorization", call["headers"])
            self.assertNotIn(SENTINEL, out + err)
            self.assertIs(json.loads(out)["client"]["authenticated"], False)


class K8LinkNextAndUrlRules(CollectorTestCase):
    def test_next_url_must_keep_the_query_apart_from_page(self):
        from collector import github

        base = "/search/repositories"
        query = "page=1&per_page=100&q=ps3recomp+in%3Aname"
        github.check_next_url("https://api.github.com/search/repositories?page=2&per_page=100&q=ps3recomp+in%3Aname", base, query)
        github.check_next_url("https://api.github.com/search/repositories?q=ps3recomp+in%3Aname&per_page=100&page=2", base, query)
        for bad in (
            "https://api.github.com/search/repositories?page=2&per_page=100&q=evil",
            "https://api.github.com/search/repositories?page=2&per_page=1&q=ps3recomp+in%3Aname",
            "https://api.github.com/search/repositories?page=2&q=ps3recomp+in%3Aname",
            "https://api.github.com/search/repositories?page=2&per_page=100&q=ps3recomp+in%3Aname&extra=1",
            "https://api.github.com/search/repositories?per_page=100&q=ps3recomp+in%3Aname",
            "https://api.github.com/search/repositories?page=x&per_page=100&q=ps3recomp+in%3Aname",
            "https://api.github.com/search/repositories?page=2&page=3&per_page=100&q=ps3recomp+in%3Aname",
        ):
            with self.assertRaises(github.TransportRefused, msg=bad):
                github.check_next_url(bad, base, query)
        # Through collect: a next URL with a changed query is not followed and the search is incomplete.
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = ok(
            {"total_count": 1, "incomplete_results": False, "items": [api_repo(90, "frame-org/ps3recomp-example")]},
            {"link": '<https://api.github.com/search/repositories?q=evil&per_page=1&page=7>; rel="next"'},
        )
        routes["GET /search/repositories?q=evil&per_page=1&page=7"] = ok({"total_count": 1, "incomplete_results": False, "items": [api_repo(100, SKY)]})
        run, report, client, _ = collect_with(routes)
        self.assertEqual([l["query"] for l in client.log if l["path"] == "/search/repositories"], ["page=1&per_page=100&q=ps3recomp+in%3Aname"])
        self.assertIs(run["searches"][0]["complete"], False)

    def test_check_url_refuses_non_printable_encoded_dots_and_empty_segments(self):
        from collector import github

        for bad in (
            "https://api.github.com/repositories/1\r\nX-Injected: 1",
            "https://api.github.com/repositories/1\n",
            "https://api.github.com/repositories/1 ",
            "https://api.github.com/repos/o/r/%2e%2e/admin",
            "https://api.github.com/repos/o%2fr/forks",
            "https://api.github.com/repos/o%5Cr/forks",
            "https://api.github.com/repos//forks",
            "https://api.github.com/repos/o/r/",
            "https://api.github.com/repos/./forks",
            "https://api.github.com/repos/../forks",
            "https://api.github.com/repositories/1é",
            "https://api.github.com/" + "a" * 2100,
        ):
            with self.assertRaises(github.TransportRefused, msg=repr(bad)):
                github.check_url(bad)
        self.assertEqual(github.check_url("https://api.github.com/repos/o/r/forks?page=1"), ("/repos/o/r/forks", "page=1"))


class K9LiveTransportBehaviour(CollectorTestCase):
    def test_request_line_and_headers(self):
        from collector import github

        transport = fake_live(FakeHTTPResponse(200, {"Content-Type": "application/json", "X-RateLimit-Remaining": "7"}, b'{"id": 90}'))
        client = github.Client(transport, token=github.Secret(SENTINEL), max_requests=3)
        with unittest.mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "NETRC": "/nonexistent"}):
            result = client.get_json("/repositories/90", {"per_page": 1}, expect="object")
        self.assertEqual(result.data, {"id": 90})
        self.assertEqual(result.headers["x-ratelimit-remaining"], "7")
        conn = FakeConnection.instances[0]
        self.assertEqual((conn.host, conn.port), ("api.github.com", 443))
        self.assertIsNotNone(conn.context)
        self.assertTrue(conn.closed)
        request = conn.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["url"], "/repositories/90?per_page=1")
        headers = request["headers"]
        self.assertEqual(headers["Authorization"], "Bearer " + SENTINEL)
        self.assertEqual(headers["Accept"], github.ACCEPT_JSON)
        self.assertEqual(headers["X-GitHub-Api-Version"], github.API_VERSION)
        self.assertNotIn("Accept-Encoding", headers)
        self.assertNotIn("Cookie", headers)
        plain = github.Client(fake_live(FakeHTTPResponse(200, {}, b"{}")), max_requests=3)
        plain.get_json("/repositories/90", None, expect="object")
        self.assertNotIn("Authorization", FakeConnection.instances[0].requests[0]["headers"])

    def test_redirect_is_returned_not_followed_and_read_stops_at_the_cap(self):
        from collector import github

        transport = fake_live(FakeHTTPResponse(302, {"Location": "https://api.github.com/repositories/91"}, b""))
        response = transport("GET", "https://api.github.com/repositories/90", {}, 5)
        self.assertEqual(response.status, 302)
        self.assertEqual(len(FakeConnection.instances), 1)
        self.assertEqual(len(FakeConnection.instances[0].requests), 1)
        client = github.Client(fake_live(FakeHTTPResponse(302, {"Location": "https://api.github.com/repositories/91"}, b"")), max_requests=3)
        with self.assertRaises(github.ResponseError):
            client.get_json("/repositories/90", None, expect="object")
        self.assertEqual(len(FakeConnection.instances), 1)
        # A body far beyond the cap: reading stops after cap + 1 bytes, not after the whole body.
        chunk = b"x" * github.READ_CHUNK
        response = FakeHTTPResponse(200, {}, b"", chunks=[chunk] * 1000)
        transport = fake_live(response, max_body=4 * github.READ_CHUNK)
        out = transport("GET", "https://api.github.com/repositories/90", {}, 5)
        self.assertEqual(len(out.body), 4 * github.READ_CHUNK + 1)
        self.assertLessEqual(response.reads, 6)
        client = github.Client(fake_live(FakeHTTPResponse(200, {}, b"", chunks=[chunk] * 1000), max_body=4 * github.READ_CHUNK), max_requests=3, max_body=4 * github.READ_CHUNK)
        with self.assertRaises(github.ResponseError):
            client.get_json("/repositories/90", None, expect="object")

    def test_non_allowlisted_url_is_refused_before_any_connection(self):
        from collector import github

        transport = fake_live(FakeHTTPResponse(200, {}, b"{}"))
        for url in ("http://api.github.com/repositories/1", "https://evil.example/repositories/1", "https://api.github.com:8443/repositories/1"):
            with self.assertRaises(github.TransportRefused, msg=url):
                transport("GET", url, {}, 5)
        with self.assertRaises(github.TransportRefused):
            transport("POST", "https://api.github.com/repositories/1", {}, 5)
        self.assertEqual(FakeConnection.instances, [])

    def test_default_factory_is_http_client_https_connection_and_guard_blocks_sockets(self):
        from collector import github

        transport = github.LiveTransport()
        with unittest.mock.patch.object(github.http.client, "HTTPSConnection", FakeConnection):
            FakeConnection.instances = []
            FakeConnection.next_response = FakeHTTPResponse(200, {}, b"{}")
            response = transport("GET", "https://api.github.com/repositories/90", {"Accept": "x"}, 5)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(FakeConnection.instances), 1)
        # Without the patch the real class is used and the test guard refuses the socket: mapped, not raised raw.
        with self.assertRaises(github.TransportFailed) as ctx:
            transport("GET", "https://api.github.com/repositories/90", {"Accept": "x"}, 1)
        self.assertEqual(ctx.exception.kind, "NetworkBlocked")


class K10Smaller(CollectorTestCase):
    def test_rate_limit_headers_are_bounded_integers_and_limits_are_sticky(self):
        from collector import github

        self.assertIsNone(github.header_int("garbage"))
        self.assertIsNone(github.header_int("-1"))
        self.assertIsNone(github.header_int("9" * 20))
        self.assertIsNone(github.header_int("1.5"))
        self.assertEqual(github.header_int(" 42 "), 42)
        routes = {
            "GET /repositories/1": ok(api_repo(1, "a/a"), {"x-ratelimit-remaining": "garbage", "x-ratelimit-reset": "soon", "retry-after": "never"}),
            "GET /repositories/2": ok(api_repo(2, "a/b"), {"x-ratelimit-remaining": "5", "x-ratelimit-reset": "1800000100"}),
            # A later, larger reset must not replace the earliest one recorded.
            "GET /repositories/3": ok(api_repo(3, "a/c"), {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800009999"}),
            "GET /repositories/4": ok(api_repo(4, "a/d"), {"x-ratelimit-remaining": "4000", "x-ratelimit-reset": "1800009999"}),
        }
        client, _ = make_client(routes)
        client.get_json("/repositories/1", None, expect="object")
        self.assertIs(client.rate_limited, False)
        self.assertIsNone(client.rate_limit_reset)
        self.assertIsNone(client.retry_after)
        client.get_json("/repositories/2", None, expect="object")
        client.get_json("/repositories/3", None, expect="object")
        self.assertIs(client.rate_limited, True)
        self.assertEqual(client.rate_limit_reset, 1800000100)
        with self.assertRaises(github.RateLimited):
            client.get_json("/repositories/4", None, expect="object")
        self.assertIs(client.rate_limited, True)
        self.assertEqual(client.rate_limit_reset, 1800000100)

    def test_config_errors_do_not_echo_keys_and_placeholders_normalise(self):
        from collector import collect, schema

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"queries": ["ok"], "roots": [], "SECRET_KEY_NAME": 1, "another_one": 2}, fh)
            with self.assertRaises(schema.SchemaError) as ctx:
                collect.load_config(path)
            self.assertEqual(str(ctx.exception), "config: 2 unknown field(s)")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"queries": ["ok"], "roots": [], "page_caps": {"SECRET_CAP": 1}}, fh)
            with self.assertRaises(schema.SchemaError) as ctx:
                collect.load_config(path)
            self.assertEqual(str(ctx.exception), "config.page_caps: 1 unknown field(s)")
        example = collect.load_config(collect.EXAMPLE_CONFIG_PATH)
        spaced = "  " + example["queries"][0].upper().replace(" ", "   ") + " "
        found = collect.config_uses_example_names(config_for_network(queries=[spaced]))
        self.assertEqual(found, [spaced])
        name = example["roots"][0]["full_name"]
        found = collect.config_uses_example_names(config_for_network(roots=[{"repository_id": 7, "full_name": name.upper()}]))
        self.assertEqual(found, [name.upper()])

    def test_ceiling_uses_distinct_ids(self):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = ok({"total_count": 3, "incomplete_results": False, "items": [api_repo(90, "frame-org/ps3recomp-example"), api_repo(100, SKY), api_repo(100, SKY)]})
        run, _, _, _ = collect_with(routes)
        self.assertEqual(run["searches"][0]["repository_ids"], [90, 100])
        self.assertIs(run["searches"][0]["complete"], False)
        routes[search_url("ps3recomp in:name")] = ok({"total_count": 2, "incomplete_results": False, "items": [api_repo(90, "frame-org/ps3recomp-example"), api_repo(100, SKY), api_repo(100, SKY)]})
        run, _, _, _ = collect_with(routes)
        self.assertIs(run["searches"][0]["complete"], True)

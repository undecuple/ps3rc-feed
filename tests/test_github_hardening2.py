"""Regression tests for the second step 3 review (L1-L10) and the mutation kills it asked for.

No network: tests/helpers.py blocks sockets at import; the live transport
is driven through fake connections only.
"""

import copy
import http.client
import json
import os
import random
import ssl
import stat
import tempfile
import unittest.mock

from helpers import CollectorTestCase, run_runs
from test_github import RUN_AT, SENTINEL, SKY, _run_cli, api_commit, api_release, api_repo, config_for_network, make_client, ok, routes_for_network, search_url, sha
from test_github_hardening import FakeConnection, FakeHTTPResponse, collect_with, fake_live, repo_100

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMITS_100 = "GET /repos/game-org/skyfall-recomp/commits?page=1&per_page=100&sha=main&since=2026-10-06T00%3A00%3A00Z"
RELEASES_100 = "GET /repos/game-org/skyfall-recomp/releases?per_page=30"
FORKS_90 = "GET /repos/frame-org/ps3recomp-example/forks?page=1&per_page=100"
SINCE = "2026-10-10T00%3A00%3A00Z"
DAY_RUN_AT = "2026-11-09T00:00:00Z"


def plain(i, name, **kw):
    data = {"id": i, "full_name": name, "owner": {"login": name.split("/")[0]}, "fork": False, "parent": None, "source": None, "archived": False, "stargazers_count": 1, "default_branch": "main", "description": "d"}
    data.update(kw)
    return data


def network(n=3, day=9):
    """n plain repositories found by one query, every sub-request answered; run_at day of November."""
    since = "2026-10-%02dT00%%3A00%%3A00Z" % (day + 1) if day < 31 else SINCE
    routes = {"GET /search/repositories?page=1&per_page=100&q=x": ok({"total_count": n, "incomplete_results": False, "items": [{"id": 10 + i, "full_name": "o/r%d" % i} for i in range(n)]})}
    for i in range(n):
        routes["GET /repositories/%d" % (10 + i)] = ok(plain(10 + i, "o/r%d" % i))
        routes["GET /repos/o/r%d/commits?page=1&per_page=100&sha=main&since=%s" % (i, since)] = ok([])
        routes["GET /repos/o/r%d/releases?per_page=30" % i] = ok([])
        routes["GET /repos/o/r%d/readme" % i] = ok("readme")
    return routes


def collect_plain(routes, run_at, **client_kw):
    from collector import collect, github

    client = github.Client(github.RecordedTransport(routes), **client_kw)
    run, report = collect.collect(client, {"queries": ["x"], "roots": []}, run_at)
    return run, report, client


class L1ListingsOverTheCaps(CollectorTestCase):
    def test_oversize_listings_are_capped_counted_and_incomplete(self):
        from collector import schema

        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = ok({"total_count": 10001, "incomplete_results": False, "items": [{"id": 100000 + i, "full_name": "o/s%d" % i} for i in range(10001)]})
        run, report, _, _ = collect_with(routes, config_for_network(max_repositories=1))
        self.assertIs(run["searches"][0]["complete"], False)
        self.assertEqual(len(run["searches"][0]["repository_ids"]), schema.MAX_IDS)
        self.assertGreaterEqual(report["dropped_items"], 1)
        routes = routes_for_network()
        routes[FORKS_90] = ok([{"id": 100000 + i, "full_name": "f/s%d" % i} for i in range(10001)])
        run, report, _, _ = collect_with(routes, config_for_network(max_repositories=1))
        self.assertIs(run["roots"][0]["forks_scan_complete"], False)
        self.assertEqual(len(run["roots"][0]["found_repository_ids"]), schema.MAX_IDS)
        routes = routes_for_network()
        routes[COMMITS_100] = ok([{"sha": sha(i + 1), "commit": {"committer": {"date": "2026-11-01T00:00:00Z"}}} for i in range(10001)])
        run, report, _, _ = collect_with(routes)
        self.assertIs(repo_100(run)["commits_complete"], False)
        self.assertEqual(len(repo_100(run)["commits"]), schema.MAX_COMMITS)
        routes = routes_for_network()
        routes[RELEASES_100] = ok([{"id": i + 1, "tag_name": "v", "published_at": "2026-11-01T00:00:00Z"} for i in range(1001)])
        run, report, _, _ = collect_with(routes)
        self.assertEqual(len(repo_100(run)["releases"]), schema.MAX_RELEASES)
        self.assertIn({"path": "/repos/game-org/skyfall-recomp/releases", "reason": "overflow"}, report["errors"])
        self.assertIs(run["collection_report"]["complete"], False)

    def test_randomised_hostile_pages_never_make_the_loader_raise(self):
        from collector import collect, github

        rng = random.Random(2026)

        def junk():
            return rng.choice([None, True, 0, -5, 2**60, "", "x" * 300, "<b>", [], {}, 1.5, "not/a/name/here", "o/r" + str(rng.randint(0, 50))])

        def hostile_repo(i):
            data = {}
            for key in ("id", "full_name", "owner", "fork", "parent", "source", "archived", "stargazers_count", "default_branch", "description"):
                if rng.random() < 0.3:
                    continue
                data[key] = {"id": i, "full_name": "o/r%d" % i, "owner": {"login": "o"}, "fork": rng.random() < 0.5, "parent": {"id": rng.randint(-3, 20)} if rng.random() < 0.5 else None, "source": None, "archived": False, "stargazers_count": rng.randint(-1, 10), "default_branch": "main", "description": "d"}[key] if rng.random() < 0.7 else junk()
            return data

        for _ in range(25):
            n = rng.randint(0, 30)
            items = [{"id": 10 + i, "full_name": "o/r%d" % i} if rng.random() < 0.7 else junk() for i in range(n)]
            routes = {"GET /search/repositories?page=1&per_page=100&q=x": ok({"total_count": rng.choice([n, 1200, -1, "x"]), "incomplete_results": rng.choice([True, False, "x"]), "items": items})}
            for i in range(n):
                routes["GET /repositories/%d" % (10 + i)] = ok(hostile_repo(10 + i)) if rng.random() < 0.8 else {"status": rng.choice([301, 403, 404, 451, 500]), "headers": {}, "body": "x"}
                routes["GET /repos/o/r%d/commits?page=1&per_page=100&sha=main&since=%s" % (i, SINCE)] = ok([{"sha": sha(rng.randint(1, 3)), "commit": {"committer": {"date": rng.choice(["2026-11-01T00:00:00Z", "bad", None])}}} if rng.random() < 0.7 else junk() for _ in range(rng.randint(0, 1500))])
                routes["GET /repos/o/r%d/releases?per_page=30" % i] = ok([{"id": rng.randint(-1, 5), "tag_name": rng.choice(["v", None, 7]), "published_at": rng.choice(["2026-11-01T00:00:00Z", None, "bad"])} for _ in range(rng.randint(0, 1200))])
                routes["GET /repos/o/r%d/readme" % i] = ok("x" * rng.randint(0, 40000))
            client = github.Client(github.RecordedTransport(routes), max_requests=5000)
            run, report = collect.collect(client, {"queries": ["x"], "roots": []}, DAY_RUN_AT)
            self.assertIn("collection_report", run)
        with unittest.mock.patch.object(collect.schema, "validate_run", side_effect=collect.schema.SchemaError("boom SENTINEL-DETAIL")):
            client = github.Client(github.RecordedTransport(network()), max_requests=50)
            with self.assertRaises(collect.InternalError) as ctx:
                collect.collect(client, {"queries": ["x"], "roots": []}, DAY_RUN_AT)
            self.assertEqual(str(ctx.exception), "internal error: the collector produced an invalid run; nothing was written")


class L2BudgetStopBeforeRequest(CollectorTestCase):
    def test_never_requested_repository_is_not_written_as_failed(self):
        run, report, client = collect_plain(network(3), DAY_RUN_AT, max_requests=5)
        self.assertEqual(report["stopped"], "budget")
        self.assertEqual([(r["repository_id"], r["fetch_state"]) for r in run["repositories"]], [(10, "ok")])
        self.assertEqual(report["not_fetched_repository_ids"], [11, 12])
        self.assertEqual(run["collection_report"]["not_fetched"], 2)
        self.assertEqual(len(client.log), 5)

    def test_day_loop_with_a_small_budget_raises_no_collection_stale(self):
        from collector import catalog, events, store

        state = store.empty_state()
        emitted = []
        for day in range(1, 6):
            run, report, _ = collect_plain(network(3, day), "2026-11-%02dT00:00:00Z" % day, max_requests=50 if day == 1 else 5)
            emitted += events.apply_run(state, run, events.Config())
        self.assertEqual([e["type"] for e in emitted if e["type"] == "collection_stale"], [])
        boundary = state["repositories"]["11"]
        self.assertEqual(boundary["collection_state"], "ok")
        self.assertEqual(boundary["consecutive_failures"], 0)
        self.assertEqual(boundary["last_successful_refresh_at"], "2026-11-01T00:00:00Z")
        self.assertEqual(boundary["last_attempted_refresh_at"], "2026-11-01T00:00:00Z")
        status = catalog.collection_status(state)
        self.assertEqual(status["failing"], [])
        self.assertEqual(status["not_refreshed"], [11, 12])
        entries = {c["id"]: c for c in catalog.build_catalog(state)}
        self.assertIs(entries[11]["collection_failing"], False)
        self.assertIs(entries[11]["refreshed_in_last_run"], False)
        self.assertIs(entries[10]["refreshed_in_last_run"], True)

    def test_a_request_that_was_really_refused_keeps_its_failure_state(self):
        routes = network(2)
        routes["GET /repositories/11"] = {"status": 403, "headers": {"x-ratelimit-remaining": "0"}, "body": {}}
        run, report, _ = collect_plain(routes, DAY_RUN_AT)
        self.assertEqual(report["stopped"], "rate_limited")
        self.assertEqual([(r["repository_id"], r["fetch_state"]) for r in run["repositories"]], [(10, "ok"), (11, "rate_limited")])


class L3PartialRunVisibleInStatus(CollectorTestCase):
    def test_status_shows_the_last_run_and_incomplete(self):
        from collector import catalog, events, schema, store

        state = store.empty_state()
        run, _, _ = collect_plain(network(3, 1), "2026-11-01T00:00:00Z")
        events.apply_run(state, run)
        status = catalog.collection_status(state)
        self.assertIs(status["incomplete"], False)
        self.assertEqual(status["last_run"], {"run_at": "2026-11-01T00:00:00Z", "complete": True, "stopped": None, "not_fetched_count": 0, "error_count": 0})
        run, _, _ = collect_plain(network(3, 2), "2026-11-02T00:00:00Z", max_requests=2)
        events.apply_run(state, run)
        status = catalog.collection_status(state)
        self.assertIs(status["incomplete"], True)
        self.assertEqual(status["last_run"]["stopped"], "budget")
        self.assertIs(status["last_run"]["complete"], False)
        # Budget 2: the search and repository 10's metadata; 11 and 12 were never requested.
        self.assertEqual(status["last_run"]["not_fetched_count"], 2)
        self.assertEqual(status["failing"], [])
        self.assertEqual(status["not_refreshed"], [11, 12])
        self.assertEqual(state["collection"]["last_run"]["last_run_at"], "2026-11-02T00:00:00Z")
        schema.validate_state(copy.deepcopy(state))
        # Fixture runs carry no report: treated as complete.
        state2, _ = run_runs([{"run_at": DAY_RUN_AT, "roots": [], "searches": [], "repositories": [run["repositories"][0]]}])
        self.assertIs(catalog.collection_status(state2)["last_run"]["complete"], True)
        # The stored report is bounded and typed.
        bad = copy.deepcopy(state)
        bad["collection"]["last_run"]["not_fetched"] = -1
        with self.assertRaises(schema.SchemaError):
            schema.validate_state(bad)


class FakeTimer:
    created = []

    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.started = False
        self.cancelled = False
        self.daemon = False
        FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class FakeSock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        pass


class L4DeadlineCoversEveryPhase(CollectorTestCase):
    def _transport(self, response, clock, **kw):
        from collector import github

        transport = fake_live(response, **kw)
        return transport, github

    def test_header_phase_deadline_watchdog_and_socket_timeouts(self):
        from collector import github

        clock = {"now": 0.0}
        response = FakeHTTPResponse(200, {"Content-Length": "2"}, b"{}")
        transport = fake_live(response, deadline=20)

        def slow_getresponse(self):
            clock["now"] += 25.0
            return response

        FakeTimer.created = []
        with unittest.mock.patch.object(github.time, "monotonic", side_effect=lambda: clock["now"]), unittest.mock.patch.object(github.threading, "Timer", FakeTimer), unittest.mock.patch.object(FakeConnection, "getresponse", slow_getresponse):
            with self.assertRaises(github.TransportFailed) as ctx:
                transport("GET", "https://api.github.com/repositories/90", {}, 5)
        self.assertIn("TimeoutError", str(ctx.exception))
        self.assertEqual(len(FakeTimer.created), 1)
        timer = FakeTimer.created[0]
        self.assertEqual(timer.interval, 20)
        self.assertTrue(timer.started and timer.cancelled)
        self.assertEqual(timer.function, github._abort_connection)
        # Socket timeouts are set from the remaining time before each read.
        clock["now"] = 0.0
        chunk = b"x" * 10
        response = FakeHTTPResponse(200, {"Content-Length": "30"}, b"", chunks=[chunk, chunk, chunk])
        original_read = response.read

        def ticking_read(amt=None):
            clock["now"] += 6.0
            return original_read(amt)

        response.read = ticking_read
        transport = fake_live(response, deadline=20)
        sock = FakeSock()
        original_init = FakeConnection.__init__

        def init_with_sock(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.sock = sock

        with unittest.mock.patch.object(github.time, "monotonic", side_effect=lambda: clock["now"]), unittest.mock.patch.object(FakeConnection, "__init__", init_with_sock):
            out = transport("GET", "https://api.github.com/repositories/90", {}, 5)
        self.assertEqual(out.body, chunk * 3)
        self.assertTrue(all(t <= 5 for t in sock.timeouts))
        self.assertEqual(sock.timeouts[0], 5)
        self.assertLess(sock.timeouts[-1], sock.timeouts[-2])
        # Watchdog action closes the socket and the connection.
        conn = FakeConnection("api.github.com", 443)
        conn.sock = FakeSock()
        github._abort_connection(conn)
        self.assertTrue(conn.closed)

    def test_header_bytes_cap_1xx_and_content_length_conflicts_are_failures(self):
        from collector import github

        big = FakeHTTPResponse(200, {"X-A": "a" * (github.MAX_HEADER_BYTES + 1), "Content-Length": "2"}, b"{}")
        with self.assertRaises(github.TransportFailed) as ctx:
            fake_live(big)("GET", "https://api.github.com/repositories/90", {}, 5)
        self.assertIn("LineTooLong", str(ctx.exception))
        for status in (100, 101, 102):
            with self.assertRaises(github.TransportFailed):
                fake_live(FakeHTTPResponse(status, {}, b""))("GET", "https://api.github.com/repositories/90", {}, 5)
        for headers in ([("Content-Length", "2"), ("Content-Length", "50")], [("Content-Length", "abc")]):
            response = FakeHTTPResponse(200, {}, b"{}")
            response.getheaders = lambda h=headers: list(h)
            with self.assertRaises(github.TransportFailed):
                fake_live(response)("GET", "https://api.github.com/repositories/90", {}, 5)
        response = FakeHTTPResponse(200, {}, b"{}")
        response.getheaders = lambda: [("Content-Length", "2"), ("Content-Length", "2")]
        self.assertEqual(fake_live(response)("GET", "https://api.github.com/repositories/90", {}, 5).body, b"{}")

    def test_duplicate_rate_limit_headers_keep_the_most_restrictive_value(self):
        from collector import github

        merged = github.merge_headers([("X-RateLimit-Remaining", "0"), ("X-RateLimit-Remaining", "4999"), ("X-RateLimit-Reset", "1800000500"), ("X-RateLimit-Reset", "1800000100"), ("Retry-After", "5"), ("Retry-After", "60"), ("Link", "<a>; rel=x"), ("Link", "<b>; rel=y")])
        self.assertEqual(merged["x-ratelimit-remaining"], "0")
        self.assertEqual(merged["x-ratelimit-reset"], "1800000100")
        self.assertEqual(merged["retry-after"], "60")
        self.assertEqual(merged["link"], "<a>; rel=x, <b>; rel=y")
        response = github.Response(200, [("X-RateLimit-Remaining", "4999"), ("X-RateLimit-Remaining", "0")], b"{}")
        client, _ = make_client({})
        client._note_rate_limit(response, "/x")
        self.assertIs(client.rate_limited, True)
        with self.assertRaises(github.LinkError):
            github.parse_link_next(github.merge_headers([("Link", "<https://api.github.com/x?page=2>; rel=next"), ("Link", "<https://api.github.com/x?page=3>; rel=next")])["link"])


class L5ConfigDuplicates(CollectorTestCase):
    def test_duplicate_queries_ids_and_names_are_refused_with_counts_only(self):
        from collector import collect, schema

        for bad in (
            {"queries": ["x", "x"], "roots": []},
            {"queries": ["x", "  X  "], "roots": []},
            {"queries": [], "roots": [{"repository_id": 10, "full_name": "o/r0"}, {"repository_id": 10, "full_name": "o/rr"}]},
            {"queries": [], "roots": [{"repository_id": 10, "full_name": "o/r0"}, {"repository_id": 11, "full_name": "O/R0"}]},
        ):
            with self.assertRaises(schema.SchemaError, msg=bad) as ctx:
                collect.validate_config(copy.deepcopy(bad))
            self.assertNotIn("x", str(ctx.exception).replace("config", "").replace("full_name", "").replace("repository_id", "").replace("duplicate", "").replace("query", ""))
        with self.assertRaises(schema.SchemaError) as ctx:
            collect.validate_config({"queries": ["ok"], "roots": [{"repository_id": 1, "full_name": "a/b", "SECRETFIELD": 1}]})
        self.assertEqual(str(ctx.exception), "config.roots[0]: 1 unknown field(s)")


class L6MinimalMetadataIsPartial(CollectorTestCase):
    def test_metadata_without_expected_fields_is_partial_not_complete(self):
        routes = network(1)
        routes["GET /repositories/10"] = ok({"id": 10, "full_name": "o/r0"})
        run, report, client = collect_plain(routes, DAY_RUN_AT)
        repo = run["repositories"][0]
        self.assertEqual(repo["fetch_state"], "ok")
        for field in ("stars", "default_branch", "commits", "commits_complete", "is_fork", "archived"):
            self.assertIsNone(repo[field], field)
        self.assertEqual(sorted(e["reason"] for e in report["errors"]), ["missing:archived", "missing:default_branch", "missing:fork", "missing:stargazers_count"])
        self.assertIs(run["collection_report"]["complete"], False)
        self.assertFalse(any("/commits" in l["path"] for l in client.log))


class L7LinkParsing(CollectorTestCase):
    def test_rel_forms_are_recognised_and_bad_headers_are_failures(self):
        from collector import github

        for header in ('<https://api.github.com/x?page=2>; rel=next', '<https://api.github.com/x?page=2>; rel="Next"', '<https://api.github.com/x?page=2>; rel="next last"', "<https://api.github.com/x?page=2>; rel='next'", '<https://api.github.com/x?page=2>; REL="NEXT"'):
            self.assertEqual(github.parse_link_next(header), "https://api.github.com/x?page=2", header)
        self.assertIsNone(github.parse_link_next('<https://api.github.com/x?page=1>; rel="prev", <https://api.github.com/x?page=9>; rel="last"'))
        self.assertIsNone(github.parse_link_next(None))
        for bad in ("", "   ", "garbage", "<https://api.github.com/x?page=2>; rel", "<https://api.github.com/x?page=2> rel=next", "<a>; rel=next, <b>; rel=next", "<" + "a" * 9000 + ">; rel=next", "<https://api.github.com/x?page=2>; rel=next, garbage"):
            with self.assertRaises(github.LinkError, msg=repr(bad)):
                github.parse_link_next(bad)

    def test_unparseable_link_jumps_and_loops_make_the_listing_incomplete(self):
        def root_routes(link, second=None):
            routes = network(1)
            routes[FORKS_90] = ok([{"id": 50, "full_name": "f/a"}], {"link": link} if link is not None else {})
            if second is not None:
                routes.update(second)
            routes["GET /repositories/90"] = ok(plain(90, "frame-org/ps3recomp-example"))
            return routes

        config = {"queries": [], "roots": [{"repository_id": 90, "full_name": "frame-org/ps3recomp-example"}], "max_repositories": 1}
        for name, link, second in (
            ("garbage", "garbage", None),
            ("loop", '<https://api.github.com/repos/frame-org/ps3recomp-example/forks?page=1&per_page=100>; rel="next"', None),
            ("jump", '<https://api.github.com/repos/frame-org/ps3recomp-example/forks?page=9&per_page=100>; rel="next"', {"GET /repos/frame-org/ps3recomp-example/forks?page=9&per_page=100": ok([{"id": 51, "full_name": "f/b"}])}),
            ("huge page", '<https://api.github.com/repos/frame-org/ps3recomp-example/forks?page=99999999999999&per_page=100>; rel="next"', None),
        ):
            run, report, client, _ = collect_with(root_routes(link, second), config)
            root = run["roots"][0]
            self.assertIs(root["forks_scan_complete"], False, name)
            self.assertEqual(root["found_repository_ids"], [50], name)
            self.assertEqual(root["resume_cursor"], "page=2", name)
            self.assertEqual([l["path"] for l in client.log if l["path"].endswith("/forks")], ["/repos/frame-org/ps3recomp-example/forks"], name)
            self.assertIn("response", [e["reason"] for e in report["errors"]], name)
        run, report, client, _ = collect_with(root_routes('<https://api.github.com/repos/frame-org/ps3recomp-example/forks?page=2&per_page=100>; rel=next', {"GET /repos/frame-org/ps3recomp-example/forks?page=2&per_page=100": ok([{"id": 51, "full_name": "f/b"}])}), config)
        self.assertEqual(run["roots"][0]["found_repository_ids"], [50, 51])
        self.assertIs(run["roots"][0]["forks_scan_complete"], True)


class L8WritePath(CollectorTestCase):
    def _fake_repo(self, tmp):
        fake_repo = os.path.join(tmp, "repo")
        os.makedirs(fake_repo)
        return fake_repo

    def test_swap_after_resolve_is_refused_and_the_victim_untouched(self):
        from collector import cli

        with tempfile.TemporaryDirectory() as tmp:
            fake_repo = self._fake_repo(tmp)
            outside = os.path.join(tmp, "outside")
            os.makedirs(os.path.join(outside, "sub"))
            victim = os.path.join(outside, "sub", "run.json")
            with open(victim, "w", encoding="utf-8") as fh:
                fh.write("OLD")
            run = {"run_at": RUN_AT, "roots": [], "searches": [], "repositories": []}
            with unittest.mock.patch.object(cli, "REPO_ROOT", fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", os.path.join(fake_repo, "data")):
                data = os.path.join(fake_repo, "data")
                os.makedirs(os.path.join(data, "sub"))
                out = cli.resolve_out_path("data/sub/run.json")
                # The network phase happened; now data/ is swapped for a link.
                os.rmdir(os.path.join(data, "sub"))
                os.rmdir(data)
                os.symlink(outside, data)
                with self.assertRaises(cli.RefusedInput):
                    cli.write_run_file(run, out)
                os.unlink(data)
                # data/ absent at resolve time, created as a link before the write.
                out = cli.resolve_out_path("data/new/run.json")
                os.symlink(outside, data)
                with self.assertRaises(cli.RefusedInput):
                    cli.write_run_file(run, out)
                os.unlink(data)
                # The target itself swapped for a link.
                os.makedirs(os.path.join(data, "sub"))
                out = cli.resolve_out_path("data/sub/run.json")
                os.symlink(victim, os.path.join(data, "sub", "run.json"))
                with self.assertRaises(cli.RefusedInput):
                    cli.write_run_file(run, out, overwrite=True)
                os.unlink(os.path.join(data, "sub", "run.json"))
                # The target swapped for a link after the first check, while the temp file is being written.
                real_fchmod = os.fchmod

                def swap_then_chmod(fd, mode):
                    os.symlink(victim, os.path.join(data, "sub", "run.json"))
                    return real_fchmod(fd, mode)

                with unittest.mock.patch.object(os, "fchmod", swap_then_chmod):
                    with self.assertRaises(cli.RefusedInput):
                        cli.write_run_file(run, out, overwrite=True)
                self.assertTrue(os.path.islink(os.path.join(data, "sub", "run.json")))
                self.assertEqual([n for n in os.listdir(os.path.join(data, "sub")) if n.endswith(".tmp")], [])
            with open(victim, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "OLD")
            self.assertEqual(sorted(os.listdir(os.path.join(outside, "sub"))), ["run.json"])

    def test_failures_leave_no_temp_file_and_keep_the_old_content(self):
        from collector import cli

        with tempfile.TemporaryDirectory() as tmp:
            fake_repo = self._fake_repo(tmp)
            data = os.path.join(fake_repo, "data")
            run = {"run_at": RUN_AT, "roots": [], "searches": [], "repositories": []}
            with unittest.mock.patch.object(cli, "REPO_ROOT", fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", data):
                out = os.path.join(data, "keep.json")
                cli.write_run_file(run, out)
                with open(out, "r", encoding="utf-8") as fh:
                    old = fh.read()
                with unittest.mock.patch.object(os, "fsync", side_effect=OSError(28, "No space left on device")):
                    with self.assertRaises(OSError):
                        cli.write_run_file(run, out, overwrite=True)
                self.assertEqual(sorted(os.listdir(data)), ["keep.json"])
                with open(out, "r", encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), old)
                with unittest.mock.patch.object(os, "rename", side_effect=OSError(5, "io")):
                    with self.assertRaises(OSError):
                        cli.write_run_file(run, out, overwrite=True)
                self.assertEqual(sorted(os.listdir(data)), ["keep.json"])
                # Without --overwrite an existing file is refused; a directory target is refused.
                with self.assertRaises(cli.RefusedInput):
                    cli.write_run_file(run, out)
                os.makedirs(os.path.join(data, "isdir.json"))
                with self.assertRaises(cli.RefusedInput):
                    cli.write_run_file(run, os.path.join(data, "isdir.json"), overwrite=True)
                with self.assertRaises(cli.RefusedInput):
                    cli.resolve_out_path("data/isdir.json")
                # Directory fsync happens on a directory descriptor; file mode is 0644 under a strict umask.
                calls = []
                real_fsync = os.fsync

                def recording_fsync(fd):
                    calls.append(stat.S_ISDIR(os.fstat(fd).st_mode))
                    return real_fsync(fd)

                old_umask = os.umask(0o077)
                try:
                    with unittest.mock.patch.object(os, "fsync", recording_fsync):
                        cli.write_run_file(run, os.path.join(data, "deep", "m.json"))
                finally:
                    os.umask(old_umask)
                self.assertIn(True, calls)
                self.assertIn(False, calls)
                self.assertEqual(os.stat(os.path.join(data, "deep", "m.json")).st_mode & 0o777, 0o644)
                self.assertEqual(os.stat(os.path.join(data, "deep")).st_mode & 0o777, 0o700)


class L9CliAndDocs(CollectorTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(config_for_network(), fh)
        self.fake_repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.fake_repo)

    def tearDown(self):
        self.tmp.cleanup()

    def _live(self, extra, env=None, routes=None):
        from collector import cli, github

        holder = {}

        def factory(*args, **kwargs):
            holder["transport"] = github.RecordedTransport(routes or routes_for_network())
            return holder["transport"]

        with unittest.mock.patch.object(cli, "REPO_ROOT", self.fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", os.path.join(self.fake_repo, "data")):
            with unittest.mock.patch.object(github, "LiveTransport", factory):
                code, out, err = _run_cli(["collect", "--config", self.config_path, "--live", "--max-requests", "20", "--run-at", RUN_AT] + extra, env=env or {"GITHUB_TOKEN": SENTINEL})
        return code, out, err, holder.get("transport")

    def test_live_with_recorded_control_characters_and_overwrite(self):
        code, out, err, transport = self._live(["--out", "data/a.json", "--recorded", "tests/fixtures/github_recorded/network.json"])
        self.assertEqual(code, 2)
        self.assertIn("--recorded", err)
        self.assertIsNone(transport)
        for bad in ("data/\x00.json", "data/a\nb.json", "data/a\tb.json", "data/a\x7fb.json"):
            code, out, err, transport = self._live(["--out", bad])
            self.assertEqual(code, 2, repr(bad))
            self.assertIn("control character", err)
            self.assertIsNone(transport)
        code, out, err, _ = self._live(["--out", "data/a.json"])
        self.assertEqual(code, 0, err)
        code, out, err, transport = self._live(["--out", "data/a.json"])
        self.assertEqual(code, 2)
        self.assertIn("--overwrite", err)
        self.assertIsNone(transport)
        code, out, err, _ = self._live(["--out", "data/a.json", "--overwrite"])
        self.assertEqual(code, 0, err)

    def test_placeholders_in_any_position_and_example_ids_are_refused(self):
        from collector import collect

        example = collect.load_config(collect.EXAMPLE_CONFIG_PATH)
        name = example["roots"][0]["full_name"]
        query = example["queries"][0]
        for cfg in (
            config_for_network(queries=["in:name " + query.split(" in:")[0]]),
            config_for_network(queries=["stuff " + name + " more"]),
            config_for_network(roots=[{"repository_id": example["roots"][0]["repository_id"], "full_name": "other-org/other"}]),
            config_for_network(roots=[{"repository_id": 777, "full_name": name.upper()}]),
        ):
            self.assertTrue(collect.config_uses_example_names(cfg), cfg)
            with open(self.config_path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
            code, out, err, transport = self._live(["--out", "data/p.json"])
            self.assertEqual(code, 2, cfg)
            self.assertIn("placeholder", err)
            self.assertIsNone(transport)
        self.assertEqual(collect.config_uses_example_names(config_for_network()), [])

    def test_config_errors_never_echo_file_content(self):
        for body in (
            '{"queries":[],"roots":[],"SECRETKEY":1,"SECRETKEY":2}',
            '{"queries":[],"roots":[{"repository_id":1,"full_name":"a/b","SECRETFIELD":1}]}',
            '{"queries":[],"roots":[],"max_requests":1.123456789}',
            "SECRET=hunter2\n",
            '{"queries":["SECRETQUERY http://x"],"roots":[]}',
        ):
            with open(self.config_path, "w", encoding="utf-8") as fh:
                fh.write(body)
            code, out, err = _run_cli(["collect", "--config", self.config_path])
            self.assertEqual(code, 1, body)
            self.assertNotIn("SECRET", out + err, body)
            self.assertNotIn("hunter2", out + err)
            self.assertNotIn("1.123", out + err)

    def test_roots_not_reached_after_a_search_stop_are_listed_as_not_fetched(self):
        routes = network(1)
        routes["GET /search/repositories?page=1&per_page=100&q=x"] = {"status": 403, "headers": {"x-ratelimit-remaining": "0"}, "body": {}}
        from collector import collect, github

        client = github.Client(github.RecordedTransport(routes), max_requests=50)
        run, report = collect.collect(client, {"queries": ["x"], "roots": [{"repository_id": 99, "full_name": "o/root"}]}, DAY_RUN_AT)
        self.assertEqual(report["stopped"], "rate_limited")
        self.assertEqual(report["not_fetched_repository_ids"], [99])
        self.assertEqual(run["roots"], [])
        self.assertEqual(run["collection_report"]["not_fetched"], 1)


class L10MutationKills(CollectorTestCase):
    def test_keyboard_interrupt_is_not_mapped(self):
        from collector import github

        class Interrupting:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                raise KeyboardInterrupt()

            def close(self):
                pass

        with self.assertRaises(KeyboardInterrupt):
            github.LiveTransport(connection_factory=Interrupting)("GET", "https://api.github.com/x", {}, 1)

    def test_content_length_boundary_is_exact(self):
        from collector import github

        with self.assertRaises(github.TransportFailed):
            fake_live(FakeHTTPResponse(200, {"Content-Length": "3"}, b"{}"))("GET", "https://api.github.com/x", {}, 1)
        self.assertEqual(fake_live(FakeHTTPResponse(200, {"Content-Length": "2"}, b"{}"))("GET", "https://api.github.com/x", {}, 1).body, b"{}")

    def test_any_server_answer_resets_the_failure_streak(self):
        from collector import github

        sequence = [OSError("a"), OSError("b"), github.Response(500, {}, b""), OSError("c"), OSError("d"), github.Response(500, {}, b"")]

        def transport(method, url, headers, timeout):
            item = sequence.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        client = github.Client(transport, max_requests=50)
        results = []
        for _ in range(6):
            try:
                results.append(client.get("/x").status)
            except github.GitHubError as exc:
                results.append(type(exc).__name__)
        self.assertEqual(results, ["TransportFailed", "TransportFailed", 500, "TransportFailed", "TransportFailed", 500])
        self.assertIs(client.network_failed, False)

    def test_tls_context_verifies_certificates_and_hostnames(self):
        from collector import github

        transport = fake_live(FakeHTTPResponse(200, {}, b"{}"))
        transport("GET", "https://api.github.com/repositories/90", {}, 5)
        context = FakeConnection.instances[0].context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(context.check_hostname, True)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        built = github.tls_context()
        self.assertEqual(built.verify_mode, ssl.CERT_REQUIRED)
        # A context built from the unverified constructor has no trust store even after
        # verify_mode is forced; the default context loads the system store.
        paths = ssl.get_default_verify_paths()
        if paths.cafile and os.path.exists(paths.cafile):
            self.assertGreater(context.cert_store_stats()["x509_ca"], 0)
            self.assertGreater(built.cert_store_stats()["x509_ca"], 0)

    def test_completeness_tracks_commit_listings_and_fork_drops(self):
        routes = routes_for_network()
        routes[COMMITS_100] = ok([api_commit(0xB, "2026-11-04T12:00:00Z")], {"link": '<https://api.github.com/repos/game-org/skyfall-recomp/commits?page=2&per_page=100&sha=main&since=2026-10-06T00%3A00%3A00Z>; rel="next"'})
        run, report, _, _ = collect_with(routes, config_for_network(page_caps={"commits": 1}))
        self.assertEqual(report["error_count"], 0)
        self.assertEqual(report["dropped_items"], 0)
        self.assertIs(repo_100(run)["commits_complete"], False)
        self.assertIs(run["collection_report"]["complete"], False)
        routes = routes_for_network()
        routes[FORKS_90] = ok([api_repo(100, SKY), {"full_name": "no/id"}])
        run, report, _, _ = collect_with(routes)
        self.assertIs(run["roots"][0]["forks_scan_complete"], False)
        self.assertEqual(report["dropped_items"], 1)

    def test_readme_is_requested_with_the_raw_media_type(self):
        from collector import github

        run, report, client, transport = collect_with(routes_for_network())
        readme_calls = [c for c in transport.calls if c["url"].endswith("/readme")]
        self.assertEqual(len(readme_calls), 2)
        for call in readme_calls:
            self.assertEqual(call["headers"]["Accept"], github.ACCEPT_RAW)
        others = [c for c in transport.calls if not c["url"].endswith("/readme")]
        for call in others:
            self.assertEqual(call["headers"]["Accept"], github.ACCEPT_JSON)

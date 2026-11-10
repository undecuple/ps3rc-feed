"""Step 3: read-only GitHub client, adapter to the run format, collect CLI.

Every test uses the recorded (in-memory) transport with fictional data. No
test, and nothing these tests import, opens a socket. The sentinel token
below must appear in exactly one place: the Authorization header the fake
transport receives.
"""

import copy
import io
import json
import os
import tempfile
import time
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from helpers import FIXTURES, CollectorTestCase, all_strings, canonical, run_runs

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTINEL = "SENTINEL-TOKEN-7f3a9c"
RUN_AT = "2026-11-05T00:00:00Z"
FRAME = "frame-org/ps3recomp-example"
SKY = "game-org/skyfall-recomp"


def sha(n):
    return "%040x" % n


def api_repo(rid, full_name, fork=False, parent=None, source=None, description="Fictional", archived=False, stars=5, default_branch="main", **extra):
    data = {
        "id": rid,
        "node_id": "ignored",
        "full_name": full_name,
        "owner": {"login": full_name.split("/")[0], "id": 1},
        "description": description,
        "fork": fork,
        "archived": archived,
        "stargazers_count": stars,
        "default_branch": default_branch,
        "html_url": "javascript:alert(1)",
        "private": False,
    }
    if parent is not None:
        data["parent"] = {"id": parent, "full_name": "ignored/ignored"}
    if source is not None:
        data["source"] = {"id": source, "full_name": "ignored/ignored"}
    data.update(extra)
    return data


def api_commit(n, date="2026-11-01T12:00:00Z", message="fictional change"):
    return {"sha": sha(n), "commit": {"message": message, "committer": {"date": date}, "author": {"date": date}}, "html_url": "javascript:x"}


def api_release(rid, tag, published="2026-11-04T10:00:00Z", draft=False):
    return {"id": rid, "tag_name": tag, "published_at": published, "draft": draft, "html_url": "javascript:x"}


def ok(body, headers=None):
    h = {"x-ratelimit-remaining": "4000", "x-ratelimit-reset": "1800000000"}
    h.update(headers or {})
    return {"status": 200, "headers": h, "body": body}


def search_url(query, page=1):
    from collector import github

    return "GET " + github.build_url("/search/repositories", {"q": query, "per_page": 100, "page": page})[len(github.API_BASE):]


def routes_for_network():
    """A small fictional network: framework 90, game fork 100, one query, one root."""
    from collector import github

    q = "ps3recomp in:name"
    routes = {
        search_url(q): ok({"total_count": 2, "incomplete_results": False, "items": [api_repo(90, FRAME), api_repo(100, SKY, fork=True, parent=90, source=90)]}),
        "GET /repos/frame-org/ps3recomp-example/forks?page=1&per_page=100": ok([api_repo(100, SKY, fork=True, parent=90, source=90)]),
        "GET /repositories/90": ok(api_repo(90, FRAME, description="Fictional framework")),
        "GET /repositories/100": ok(api_repo(100, SKY, fork=True, parent=90, source=90, description="Recomp of Skyfall (fictional)")),
        "GET /repos/frame-org/ps3recomp-example/commits?page=1&per_page=100&sha=main&since=2026-10-06T00%3A00%3A00Z": ok([api_commit(2, "2026-11-02T12:00:00Z"), api_commit(1, "2026-11-01T12:00:00Z")]),
        "GET /repos/game-org/skyfall-recomp/commits?page=1&per_page=100&sha=main&since=2026-10-06T00%3A00%3A00Z": ok([api_commit(0xB, "2026-11-04T12:00:00Z"), api_commit(0xA, "2026-11-03T12:00:00Z")]),
        "GET /repos/frame-org/ps3recomp-example/releases?per_page=30": ok([]),
        "GET /repos/game-org/skyfall-recomp/releases?per_page=30": ok([api_release(7001, "v0.1.0"), api_release(7002, "draft", draft=True)]),
        "GET /repos/frame-org/ps3recomp-example/readme": ok("# Fictional\n"),
        "GET /repos/game-org/skyfall-recomp/readme": ok("# Skyfall\n"),
    }
    assert github.API_BASE == "https://api.github.com"
    return routes


def config_for_network(**extra):
    cfg = {"queries": ["ps3recomp in:name"], "roots": [{"repository_id": 90, "full_name": FRAME}], "commit_window_days": 30, "max_requests": 50}
    cfg.update(extra)
    return cfg


def make_client(routes, **kw):
    from collector import github

    transport = github.RecordedTransport(routes)
    kw.setdefault("max_requests", 50)
    return github.Client(transport, **kw), transport


class ClientRequests(CollectorTestCase):
    def test_pagination_follows_validated_link_next(self):
        from collector import github

        routes = {
            "GET /repos/o/r/forks?page=1&per_page=100": ok([api_repo(1, "a/a")], {"link": '<https://api.github.com/repos/o/r/forks?page=2&per_page=100>; rel="next", <https://api.github.com/repos/o/r/forks?page=2&per_page=100>; rel="last"'}),
            "GET /repos/o/r/forks?page=2&per_page=100": ok([api_repo(2, "b/b")]),
        }
        client, transport = make_client(routes)
        pages = client.paginate("/repos/o/r/forks", {"per_page": 100}, cap=5)
        self.assertEqual([i["id"] for i in pages.items], [1, 2])
        self.assertIs(pages.complete, True)
        self.assertIsNone(pages.next_cursor)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(client.log[1], {"method": "GET", "path": "/repos/o/r/forks", "query": "page=2&per_page=100"})

    def test_hostile_link_next_is_refused_without_a_request(self):
        from collector import github

        for bad in (
            "https://evil.example/repos/o/r/forks?page=2",
            "https://api.github.com/repos/other/x/forks?page=2",
            "https://api.github.com/user?page=2",
            "http://api.github.com/repos/o/r/forks?page=2",
            "https://api.github.com:8443/repos/o/r/forks?page=2",
            "https://api.github.com@evil.example/repos/o/r/forks?page=2",
        ):
            routes = {"GET /repos/o/r/forks?page=1&per_page=100": ok([api_repo(1, "a/a")], {"link": '<%s>; rel="next"' % bad})}
            client, transport = make_client(routes)
            with self.assertRaises(github.TransportRefused, msg=bad):
                client.paginate("/repos/o/r/forks", {"per_page": 100}, cap=5)
            self.assertEqual(len(transport.calls), 1, bad)

    def test_page_cap_marks_incomplete_with_a_resume_cursor(self):
        routes = {}
        for page in (1, 2, 3, 4):
            routes["GET /repos/o/r/forks?page=%d&per_page=100" % page] = ok([api_repo(page, "a/a%d" % page)], {"link": '<https://api.github.com/repos/o/r/forks?page=%d&per_page=100>; rel="next"' % (page + 1)})
        client, transport = make_client(routes)
        pages = client.paginate("/repos/o/r/forks", {"per_page": 100}, cap=2)
        self.assertEqual([i["id"] for i in pages.items], [1, 2])
        self.assertIs(pages.complete, False)
        self.assertEqual(pages.next_cursor, "page=3")
        self.assertEqual(len(transport.calls), 2)

    def test_redirects_are_errors_and_never_followed(self):
        from collector import github

        routes = {"GET /repositories/90": {"status": 302, "headers": {"location": "https://api.github.com/repositories/91"}, "body": ""}, "GET /repositories/91": ok(api_repo(91, "x/y"))}
        client, transport = make_client(routes)
        with self.assertRaises(github.ResponseError):
            client.get_json("/repositories/90", None, expect="object")
        self.assertEqual(len(transport.calls), 1)

    def test_only_https_api_github_com_get_reaches_the_transport(self):
        from collector import github

        client, transport = make_client({})
        for url in (
            "http://api.github.com/repositories/1",
            "https://evil.example/repositories/1",
            "https://api.github.com:8443/repositories/1",
            "https://user:pw@api.github.com/repositories/1",
            "https://api.github.com.evil.example/repositories/1",
            "ftp://api.github.com/repositories/1",
            "https://api.github.com/repositories/1#frag",
            "//api.github.com/repositories/1",
        ):
            with self.assertRaises(github.TransportRefused, msg=url):
                client.get_url(url)
        with self.assertRaises(github.TransportRefused):
            client.request("POST", "https://api.github.com/repositories/1")
        with self.assertRaises(github.TransportRefused):
            client.request("DELETE", "https://api.github.com/repositories/1")
        with self.assertRaises(github.TransportRefused):
            client.get("repositories/1")
        with self.assertRaises(github.TransportRefused):
            client.get("/repositories/../user")
        self.assertEqual(transport.calls, [])
        self.assertEqual(client.requests_made, 0)

    def test_token_is_sent_only_as_the_authorization_header_and_never_leaks(self):
        from collector import github

        routes = {"GET /repositories/90": {"status": 500, "headers": {}, "body": "boom " + SENTINEL}}
        client, transport = make_client(routes, token=github.Secret(SENTINEL))
        result = client.get_json("/repositories/90", None, expect="object")
        self.assertEqual(result.status, 500)
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer " + SENTINEL)
        leaks = [repr(client), str(client), repr(client.token), str(client.token), json.dumps(client.log), json.dumps(client.summary())]
        bad_json = {"GET /repositories/90": {"status": 200, "headers": {}, "body": "not json " + SENTINEL}}
        client2, _ = make_client(bad_json, token=github.Secret(SENTINEL))
        with self.assertRaises(github.ResponseError) as ctx:
            client2.get_json("/repositories/90", None, expect="object")
        leaks.append(str(ctx.exception) + repr(ctx.exception))
        for text in leaks:
            self.assertNotIn(SENTINEL, text)
        plain, transport2 = make_client({"GET /repositories/90": ok(api_repo(90, FRAME))})
        plain.get_json("/repositories/90", None, expect="object")
        self.assertNotIn("Authorization", transport2.calls[0]["headers"])

    def test_rate_limit_stops_the_client_without_sleeping(self):
        from collector import github

        exhausted = ok(api_repo(90, FRAME), {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"})
        client, transport = make_client({"GET /repositories/90": exhausted, "GET /repositories/91": ok(api_repo(91, "a/b"))})
        self.assertEqual(client.get_json("/repositories/90", None, expect="object").status, 200)
        self.assertIs(client.rate_limited, True)
        with self.assertRaises(github.RateLimited):
            client.get_json("/repositories/91", None, expect="object")
        self.assertEqual(len(transport.calls), 1)
        for response in (
            {"status": 403, "headers": {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"}, "body": {"message": "rate limit"}},
            {"status": 429, "headers": {"retry-after": "60"}, "body": {"message": "slow down"}},
            {"status": 403, "headers": {"retry-after": "120"}, "body": {"message": "secondary limit"}},
        ):
            client, transport = make_client({"GET /repositories/90": response})
            started = time.monotonic()
            with self.assertRaises(github.RateLimited):
                client.get_json("/repositories/90", None, expect="object")
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertIs(client.rate_limited, True)
            self.assertEqual(client.summary()["retry_after"], response["headers"].get("retry-after"))
        plain_403 = {"GET /repositories/90": {"status": 403, "headers": {"x-ratelimit-remaining": "40"}, "body": {"message": "forbidden"}}}
        client, _ = make_client(plain_403)
        self.assertEqual(client.get_json("/repositories/90", None, expect="object").status, 403)
        self.assertIs(client.rate_limited, False)

    def test_not_found_gone_and_legal_block_are_statuses_not_errors(self):
        for status in (404, 410, 451):
            client, _ = make_client({"GET /repositories/90": {"status": status, "headers": {}, "body": {"message": "x"}}})
            result = client.get_json("/repositories/90", None, expect="object")
            self.assertEqual(result.status, status)
            self.assertIsNone(result.data)

    def test_oversize_invalid_duplicate_and_wrong_shape_bodies_are_errors(self):
        from collector import github

        # Valid JSON that is simply too large: only the size cap can refuse it.
        big = {"GET /repositories/90": {"status": 200, "headers": {}, "body": "[" + "1," * (github.MAX_BODY // 2) + "1]"}}
        client, _ = make_client(big)
        with self.assertRaises(github.ResponseError) as ctx:
            client.get_json("/repositories/90", None, expect="array")
        self.assertIn("exceeds", str(ctx.exception))
        client, _ = make_client({"GET /repos/o/r/readme": ok("# T\n" + "r" * 100000)})
        self.assertEqual(len(client.get_text("/repos/o/r/readme", None, max_chars=16384).data), 16384)
        for body in ("{not json", '{"a": 1, "a": 2}', '{"a": NaN}', "[1, 2]", "null", '"text"'):
            client, _ = make_client({"GET /repositories/90": {"status": 200, "headers": {}, "body": body}})
            with self.assertRaises(github.ResponseError) as ctx:
                client.get_json("/repositories/90", None, expect="object")
            self.assertIn("/repositories/90", str(ctx.exception))
        client, _ = make_client({"GET /repos/o/r/forks?page=1&per_page=100": ok({"items": []})})
        with self.assertRaises(github.ResponseError):
            client.paginate("/repos/o/r/forks", {"per_page": 100}, cap=2)

    def test_budget_is_a_hard_stop(self):
        from collector import github

        routes = {"GET /repositories/%d" % i: ok(api_repo(i, "a/r%d" % i)) for i in (1, 2, 3)}
        client, transport = make_client(routes, max_requests=2)
        client.get_json("/repositories/1", None, expect="object")
        client.get_json("/repositories/2", None, expect="object")
        with self.assertRaises(github.BudgetExhausted):
            client.get_json("/repositories/3", None, expect="object")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(client.requests_made, 2)


class CollectAdapter(CollectorTestCase):
    def _collect(self, routes=None, config=None, run_at=RUN_AT, **client_kw):
        from collector import collect

        client, transport = make_client(routes if routes is not None else routes_for_network(), **client_kw)
        run, report = collect.collect(client, config or config_for_network(), run_at)
        return run, report, client, transport

    def test_happy_path_validates_and_is_a_quiet_baseline(self):
        from collector import catalog, schema

        run, report, client, transport = self._collect()
        schema.validate_run(copy.deepcopy(run))
        self.assertEqual(run["run_at"], RUN_AT)
        self.assertEqual(run["searches"], [{"query": "ps3recomp in:name", "complete": True, "repository_ids": [90, 100]}])
        self.assertEqual(run["roots"], [{"repository_id": 90, "full_name": FRAME, "forks_scan_complete": True, "found_repository_ids": [100]}])
        by_id = {r["repository_id"]: r for r in run["repositories"]}
        self.assertEqual(sorted(by_id), [90, 100])
        self.assertEqual(by_id[100]["owner"], "game-org")
        self.assertIs(by_id[100]["is_fork"], True)
        self.assertEqual((by_id[100]["parent_repository_id"], by_id[100]["network_root_repository_id"]), (90, 90))
        self.assertEqual([c["sha"] for c in by_id[100]["commits"]], [sha(0xA), sha(0xB)])  # oldest first
        self.assertIs(by_id[100]["commits_complete"], True)
        self.assertEqual(by_id[100]["releases"], [{"id": 7001, "tag": "v0.1.0", "published_at": "2026-11-04T10:00:00Z"}])
        self.assertEqual(by_id[100]["readme_excerpt"], "# Skyfall\n")
        self.assertEqual(by_id[90]["stars"], 5)
        self.assertNotIn("html_url_claimed", by_id[90])
        self.assertNotIn("comparison", by_id[90])
        self.assertEqual(len(transport.calls), 10)
        self.assertIsNone(report["stopped"])
        # Twice the same recorded answers: baseline, then nothing.
        run2, _, _, _ = self._collect(run_at="2026-11-05T06:00:00Z")
        run2_fixed = copy.deepcopy(run2)
        state, events = run_runs([run, run2_fixed])
        self.assertEqual(events, [[], []])
        entry = [c for c in catalog.build_catalog(state) if c["id"] == 100][0]
        self.assertEqual(entry["discovery_roots"], [90])
        self.assertEqual(entry["discovery_queries"], ["ps3recomp in:name"])
        self.assertEqual(entry["source_url"], "https://github.com/" + SKY)
        self.assertEqual(entry["comparison_state"], "unavailable")

    def test_search_cap_ceiling_and_incomplete_results_mark_the_search_incomplete(self):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = ok({"total_count": 1200, "incomplete_results": False, "items": [api_repo(90, FRAME)]})
        run, _, _, _ = self._collect(routes)
        self.assertIs(run["searches"][0]["complete"], False)
        routes[search_url("ps3recomp in:name")] = ok({"total_count": 1, "incomplete_results": True, "items": [api_repo(90, FRAME)]})
        run, _, _, _ = self._collect(routes)
        self.assertIs(run["searches"][0]["complete"], False)
        routes[search_url("ps3recomp in:name")] = ok({"total_count": 1, "incomplete_results": False, "items": [api_repo(90, FRAME)]}, {"link": '<https://api.github.com/search/repositories?page=2&per_page=100&q=ps3recomp+in%3Aname>; rel="next"'})
        routes[search_url("ps3recomp in:name", 2)] = ok({"total_count": 1, "incomplete_results": False, "items": [api_repo(100, SKY)]}, {"link": '<https://api.github.com/search/repositories?page=3&per_page=100&q=ps3recomp+in%3Aname>; rel="next"'})
        run, _, _, _ = self._collect(routes, config_for_network(page_caps={"search": 2}))
        self.assertIs(run["searches"][0]["complete"], False)
        self.assertEqual(run["searches"][0]["repository_ids"], [90, 100])

    def test_forks_partial_scan_keeps_a_cursor(self):
        routes = routes_for_network()
        routes["GET /repos/frame-org/ps3recomp-example/forks?page=1&per_page=100"] = ok([api_repo(100, SKY, fork=True, parent=90, source=90)], {"link": '<https://api.github.com/repos/frame-org/ps3recomp-example/forks?page=2&per_page=100>; rel="next"'})
        run, _, _, _ = self._collect(routes, config_for_network(page_caps={"forks": 1}))
        root = run["roots"][0]
        self.assertIs(root["forks_scan_complete"], False)
        self.assertEqual(root["resume_cursor"], "page=2")
        self.assertEqual(root["found_repository_ids"], [100])

    def test_commit_page_cap_marks_commits_incomplete(self):
        routes = routes_for_network()
        key = "GET /repos/game-org/skyfall-recomp/commits?page=1&per_page=100&sha=main&since=2026-10-06T00%3A00%3A00Z"
        routes[key] = ok([api_commit(0xB, "2026-11-04T12:00:00Z")], {"link": '<https://api.github.com/repos/game-org/skyfall-recomp/commits?page=2&per_page=100&sha=main&since=2026-10-06T00%3A00%3A00Z>; rel="next"'})
        run, _, _, _ = self._collect(routes, config_for_network(page_caps={"commits": 1}))
        r100 = [r for r in run["repositories"] if r["repository_id"] == 100][0]
        self.assertIs(r100["commits_complete"], False)
        self.assertEqual(len(r100["commits"]), 1)

    def test_rate_limit_mid_run_stops_cleanly_with_partial_flags_not_zeros(self):
        from collector import schema

        routes = routes_for_network()
        routes["GET /repositories/100"] = {"status": 403, "headers": {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"}, "body": {"message": "limit"}}
        run, report, client, transport = self._collect(routes)
        schema.validate_run(copy.deepcopy(run))
        self.assertEqual(report["stopped"], "rate_limited")
        by_id = {r["repository_id"]: r for r in run["repositories"]}
        self.assertEqual(by_id[90]["fetch_state"], "ok")
        self.assertEqual(by_id[100]["fetch_state"], "rate_limited")
        for field in ("stars", "archived", "commits", "releases", "readme_excerpt", "description", "is_fork", "commits_complete"):
            self.assertIsNone(by_id[100][field], field)
        self.assertEqual(by_id[100]["full_name"], SKY)
        self.assertEqual(len(transport.calls), 7)
        state, events = run_runs([run])
        self.assertEqual(events, [[]])
        self.assertIsNone(state["repositories"]["100"]["stars"])
        # A rate limit while listing search results: search recorded incomplete, nothing invented.
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = {"status": 403, "headers": {"x-ratelimit-remaining": "0"}, "body": {"message": "limit"}}
        run, report, _, transport = self._collect(routes)
        schema.validate_run(copy.deepcopy(run))
        self.assertEqual(report["stopped"], "rate_limited")
        self.assertEqual(run["searches"], [{"query": "ps3recomp in:name", "complete": False, "repository_ids": []}])
        self.assertEqual(run["roots"], [])
        self.assertEqual(run["repositories"], [])
        self.assertEqual(len(transport.calls), 1)

    def test_budget_exhaustion_leaves_partial_flags_and_pending_ids(self):
        from collector import schema

        run, report, client, _ = self._collect(config=config_for_network(max_requests=4), max_requests=4)
        schema.validate_run(copy.deepcopy(run))
        self.assertEqual(report["stopped"], "budget")
        self.assertEqual(client.requests_made, 4)
        by_id = {r["repository_id"]: r for r in run["repositories"]}
        self.assertEqual(sorted(by_id), [90])
        self.assertEqual(by_id[90]["fetch_state"], "ok")
        self.assertIsNone(by_id[90]["releases"])
        self.assertIsNone(by_id[90]["readme_excerpt"])
        self.assertEqual(report["not_fetched_repository_ids"], [100])
        state, _ = run_runs([run])
        self.assertEqual(state["collection"]["pending_repository_ids"], [100])
        self.assertIs(state["repositories"]["90"]["partial"], True)

    def test_per_repository_statuses_map_without_claiming_a_reason(self):
        from collector import schema

        for status, expected in ((404, "not_found"), (410, "not_found"), (451, "blocked"), (403, "blocked"), (500, "failed"), (200, "ok")):
            routes = routes_for_network()
            if status != 200:
                routes["GET /repositories/100"] = {"status": status, "headers": {"x-ratelimit-remaining": "99"}, "body": {"message": "whatever"}}
            run, report, _, _ = self._collect(routes)
            schema.validate_run(copy.deepcopy(run))
            r100 = [r for r in run["repositories"] if r["repository_id"] == 100][0]
            self.assertEqual(r100["fetch_state"], expected, status)
            if status != 200:
                self.assertIsNone(r100["stars"])
                self.assertIsNone(r100["commits"])
                self.assertEqual(r100["full_name"], SKY)
                self.assertIsNone(report["stopped"])
        # Sub-request failures leave that field null and the repository partial, state ok.
        routes = routes_for_network()
        routes["GET /repos/game-org/skyfall-recomp/readme"] = {"status": 404, "headers": {}, "body": {"message": "no readme"}}
        routes["GET /repos/game-org/skyfall-recomp/releases?per_page=30"] = {"status": 500, "headers": {}, "body": "oops"}
        run, _, _, _ = self._collect(routes)
        r100 = [r for r in run["repositories"] if r["repository_id"] == 100][0]
        self.assertEqual(r100["fetch_state"], "ok")
        self.assertIsNone(r100["readme_excerpt"])
        self.assertIsNone(r100["releases"])
        self.assertIsNotNone(r100["commits"])

    def test_malformed_item_shapes_are_per_repository_failures(self):
        from collector import schema

        routes = routes_for_network()
        routes["GET /repositories/100"] = ok({"id": 100, "full_name": SKY, "owner": "not-an-object", "fork": "yes"})
        run, report, _, _ = self._collect(routes)
        schema.validate_run(copy.deepcopy(run))
        r100 = [r for r in run["repositories"] if r["repository_id"] == 100][0]
        self.assertEqual(r100["fetch_state"], "failed")
        self.assertEqual(report["errors"], [{"path": "/repositories/100", "reason": "shape"}])
        routes = routes_for_network()
        routes["GET /repos/game-org/skyfall-recomp/commits?page=1&per_page=100&sha=main&since=2026-10-06T00%3A00%3A00Z"] = ok([{"sha": "short", "commit": {}}, api_commit(0xA)])
        run, report, _, _ = self._collect(routes)
        r100 = [r for r in run["repositories"] if r["repository_id"] == 100][0]
        self.assertEqual([c["sha"] for c in r100["commits"]], [sha(0xA)])
        self.assertIs(r100["commits_complete"], False)

    def test_hostile_text_is_inert_through_the_adapter(self):
        from collector import catalog, schema

        with open(os.path.join(FIXTURES, "hostile_text", "01-baseline.json"), "r", encoding="utf-8") as fh:
            hostile = json.load(fh)["repositories"][0]
        routes = routes_for_network()
        routes["GET /repositories/100"] = ok(api_repo(100, SKY, fork=True, parent=90, source=90, description=hostile["description"], html_url=hostile["html_url_claimed"]))
        routes["GET /repos/game-org/skyfall-recomp/readme"] = ok(hostile["readme_excerpt"])
        commits = [{"sha": c["sha"], "commit": {"message": c["message"], "committer": {"date": c["committed_at"]}}} for c in hostile["commits"]]
        routes["GET /repos/game-org/skyfall-recomp/commits?page=1&per_page=100&sha=main&since=2026-10-06T00%3A00%3A00Z"] = ok(commits)
        routes["GET /repos/game-org/skyfall-recomp/releases?per_page=30"] = ok([api_release(7003, "<script>@everyone</script>")])
        run, report, _, _ = self._collect(routes)
        schema.validate_run(copy.deepcopy(run))
        r100 = [r for r in run["repositories"] if r["repository_id"] == 100][0]
        self.assertEqual(r100["description"], hostile["description"])  # stored as data
        self.assertLessEqual(len(r100["readme_excerpt"]), schema.MAX_README)
        self.assertNotIn("html_url_claimed", r100)
        run2 = copy.deepcopy(run)
        run2["run_at"] = "2026-11-06T00:00:00Z"
        for r in run2["repositories"]:
            if r["repository_id"] == 100:
                r["releases"].append({"id": 7004, "tag": "@here<b>", "published_at": "2026-11-06T00:00:00Z"})
        state, events = run_runs([run, run2])
        outputs = {"events": state["events"], "catalog": catalog.build_catalog(state)}
        text = json.dumps(outputs)
        for fragment in ("<script", "@everyone", "@here", "javascript:", "onerror=", "\\u202e"):
            self.assertNotIn(fragment, text, fragment)
        for s in all_strings(outputs):
            if "://" in s:
                self.assertTrue(s.startswith("https://github.com/"), s)
        self.assertEqual([e["type"] for e in events[1]], ["release_published"])

    def test_readme_and_description_are_bounded(self):
        from collector import schema

        routes = routes_for_network()
        routes["GET /repos/game-org/skyfall-recomp/readme"] = ok("# T\n" + "r" * 100000)
        routes["GET /repositories/100"] = ok(api_repo(100, SKY, fork=True, parent=90, source=90, description="d" * 10000))
        run, _, _, _ = self._collect(routes)
        r100 = [r for r in run["repositories"] if r["repository_id"] == 100][0]
        self.assertEqual(len(r100["readme_excerpt"]), schema.MAX_README)
        self.assertEqual(len(r100["description"]), schema.MAX_DESCRIPTION)
        self.assertEqual(r100["readme_excerpt"][:4], "# T\n")

    def test_comparison_is_not_requested_and_reported_unavailable(self):
        from collector import catalog

        run, _, client, _ = self._collect()
        self.assertFalse(any("/compare/" in r["path"] for r in client.log))
        state, _ = run_runs([run])
        self.assertEqual(catalog.build_catalog(state)[1]["comparison"]["state"], "unavailable")


class CollectConfig(CollectorTestCase):
    def test_example_config_loads_and_bad_configs_are_refused(self):
        from collector import collect, schema

        cfg = collect.load_config(collect.EXAMPLE_CONFIG_PATH)
        self.assertTrue(cfg["queries"])
        self.assertTrue(cfg["roots"])
        self.assertTrue(collect.config_uses_example_names(cfg))
        self.assertEqual(collect.config_uses_example_names(config_for_network()), [])
        root_only = config_for_network(roots=[{"repository_id": 1, "full_name": cfg["roots"][0]["full_name"].upper()}])
        self.assertEqual(collect.config_uses_example_names(root_only), [cfg["roots"][0]["full_name"].upper()])
        query_only = config_for_network(queries=[cfg["queries"][0]])
        self.assertEqual(collect.config_uses_example_names(query_only), [cfg["queries"][0]])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            for bad in (
                {"queries": ["http://x in:readme"], "roots": []},
                {"queries": ["ok"], "roots": [{"repository_id": 1, "full_name": "no-slash"}]},
                {"queries": ["ok"], "roots": [], "surprise": 1},
                {"queries": ["ok"], "roots": [], "max_requests": 0},
                {"queries": ["ok"], "roots": [], "commit_window_days": 0},
                {"queries": [], "roots": []},
            ):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(bad, fh)
                with self.assertRaises(schema.SchemaError, msg=bad):
                    collect.load_config(path)

    def test_plan_lists_the_requests_that_would_be_made(self):
        from collector import collect

        plan = collect.plan_requests(config_for_network())
        self.assertEqual(
            plan,
            [
                {"method": "GET", "path": "/search/repositories", "query": "page=1&per_page=100&q=ps3recomp+in%3Aname"},
                {"method": "GET", "path": "/repos/frame-org/ps3recomp-example/forks", "query": "page=1&per_page=100"},
                {"method": "GET", "path": "/repositories/90", "query": ""},
            ],
        )


def _run_cli(argv, env=None):
    from collector import cli

    out, err = io.StringIO(), io.StringIO()
    with unittest.mock.patch.dict(os.environ, env or {}, clear=False):
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _snapshot(root):
    snap = {}
    for dirpath, _, files in os.walk(root):
        if "/.git" in dirpath or "__pycache__" in dirpath:
            continue
        for f in files:
            p = os.path.join(dirpath, f)
            snap[p] = os.stat(p).st_mtime_ns
    return snap


class CollectCli(CollectorTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(config_for_network(), fh)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_prints_only_the_plan_and_writes_nothing(self):
        from collector import github

        before = _snapshot(REPO_ROOT)
        with unittest.mock.patch.object(github, "LiveTransport", side_effect=AssertionError("live transport constructed")):
            code, out, err = _run_cli(["collect", "--config", self.config_path], env={"GITHUB_TOKEN": SENTINEL})
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(set(payload), {"plan"})
        self.assertEqual(payload["plan"][0]["path"], "/search/repositories")
        self.assertNotIn(SENTINEL, out + err)
        self.assertEqual(_snapshot(REPO_ROOT), before)

    def test_recorded_transport_from_fixtures_prints_the_requests_made(self):
        from collector import github

        before = _snapshot(REPO_ROOT)
        recorded = os.path.join("tests", "fixtures", "github_recorded", "network.json")
        with unittest.mock.patch.object(github, "LiveTransport", side_effect=AssertionError("live transport constructed")):
            code, out, err = _run_cli(["collect", "--config", self.config_path, "--recorded", recorded, "--run-at", RUN_AT], env={"GITHUB_TOKEN": SENTINEL})
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(set(payload), {"plan"})
        self.assertEqual(len(payload["plan"]), 10)
        self.assertEqual(payload["plan"][0], {"method": "GET", "path": "/search/repositories", "query": "page=1&per_page=100&q=ps3recomp+in%3Aname"})
        self.assertNotIn(SENTINEL, out + err)
        self.assertEqual(_snapshot(REPO_ROOT), before)
        code, out, err = _run_cli(["collect", "--config", self.config_path, "--recorded", "/etc/hosts"])
        self.assertEqual(code, 2)
        self.assertIn("refused", err)

    def test_live_is_refused_without_its_guards_and_never_builds_the_transport(self):
        from collector import collect, github

        out_path = os.path.join(REPO_ROOT, "data", "test-never-written.json")
        example = os.path.join(self.tmp.name, "example.json")
        with open(example, "w", encoding="utf-8") as fh:
            json.dump(collect.load_config(collect.EXAMPLE_CONFIG_PATH), fh)
        cases = [
            (["collect", "--config", self.config_path, "--live", "--out", out_path], {"GITHUB_TOKEN": SENTINEL}, "max-requests"),
            (["collect", "--config", self.config_path, "--live", "--max-requests", "5", "--out", out_path], {"GITHUB_TOKEN": ""}, "GITHUB_TOKEN"),
            (["collect", "--config", self.config_path, "--live", "--max-requests", "5"], {"GITHUB_TOKEN": SENTINEL}, "--out"),
            (["collect", "--config", self.config_path, "--live", "--max-requests", "5", "--out", os.path.join(self.tmp.name, "x.json")], {"GITHUB_TOKEN": SENTINEL}, "data"),
            (["collect", "--config", example, "--live", "--max-requests", "5", "--out", out_path], {"GITHUB_TOKEN": SENTINEL}, "example"),
            (["collect", "--config", self.config_path, "--out", out_path], {"GITHUB_TOKEN": SENTINEL}, "--live"),
        ]
        before = _snapshot(REPO_ROOT)
        with unittest.mock.patch.object(github, "LiveTransport", side_effect=AssertionError("live transport constructed")):
            for argv, env, needle in cases:
                code, out, err = _run_cli(argv, env=env)
                self.assertEqual(code, 2, (argv, err))
                self.assertIn(needle, err, argv)
                self.assertNotIn(SENTINEL, out + err)
        self.assertEqual(_snapshot(REPO_ROOT), before)
        self.assertFalse(os.path.exists(out_path))

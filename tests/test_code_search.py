"""Code search queries: {"kind": "code", "q": ...} in the sources config.

Validation, the request plan, the separate code search rate-limit bucket and
page cap, de-duplication with the other listings, runs without a token, and
the discovery provenance recorded in the run file. Every response is recorded
and fictional; nothing opens a socket.
"""

import json
import tempfile
import time

from helpers import CollectorTestCase, run_runs
from test_github import RUN_AT, api_repo, make_client, ok, search_url
from test_sources_config import fetch_routes, search, write_config

CODE_LIMITED = {"status": 403, "headers": {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000", "x-ratelimit-resource": "code_search"}, "body": {"message": "limit"}}
SEARCH_LIMITED = {"status": 403, "headers": {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000", "x-ratelimit-resource": "search"}, "body": {"message": "limit"}}
LICENCE = "filename:licence-file.txt"


def code_url(query, page=1):
    from collector import github

    return "GET " + github.build_url("/search/code", {"q": query, "per_page": 100, "page": page})[len(github.API_BASE) :]


def code_item(rid, full_name, description="Fictional", path="licenses/licence-file.txt", **repo_extra):
    """A code search item as GitHub answers it: the file, and the repository holding it."""
    repository = {"id": rid, "node_id": "ignored", "full_name": full_name, "owner": {"login": full_name.split("/")[0]}, "description": description, "fork": False, "html_url": "javascript:alert(1)"}
    repository.update(repo_extra)
    return {"name": path.rsplit("/", 1)[-1], "path": path, "sha": "0" * 40, "html_url": "javascript:alert(1)", "repository": repository, "score": 1.0}


def code_search(query, items, total=None, headers=None):
    body = {"total_count": len(items) if total is None else total, "incomplete_results": False, "items": items}
    return {code_url(query): ok(body, dict({"x-ratelimit-resource": "code_search"}, **(headers or {})))}


def collect_code(config, routes, token=True, **client_kw):
    from collector import collect, github

    client_kw.setdefault("max_requests", 100)
    if token:
        client_kw["token"] = github.Secret("fictional_token")
    client, transport = make_client(routes, **client_kw)
    run, report = collect.collect(client, config, RUN_AT)
    return run, report, client, [entry["path"] for entry in client.log]


def code_query(q=LICENCE, **extra):
    return dict({"kind": "code", "q": q}, **extra)


class Validation(CollectorTestCase):
    BASE = {"queries": ["ok in:name"], "roots": []}

    def assertRefused(self, extra, needle=None):
        from collector import collect, schema

        config = dict(json.loads(json.dumps(self.BASE)), **extra)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(tmp, config)
            with self.assertRaises(schema.SchemaError, msg=extra) as ctx:
                collect.load_config(path)
        if needle is not None:
            self.assertIn(needle, str(ctx.exception))
        self.assertNotIn("SECRETKEY", str(ctx.exception))
        return str(ctx.exception)

    def test_code_queries_are_accepted_alongside_the_other_forms(self):
        from collector import collect

        config = collect.validate_config({"queries": ["a in:name", {"q": "b in:name", "require_any": ["t"]}, code_query(), code_query("filename:x.txt path:licenses", require_any=["port"])], "roots": []})
        self.assertEqual([collect.query_kind(q) for q in config["queries"]], ["repositories", "repositories", "code", "code"])
        self.assertIsNone(collect.query_terms(config["queries"][2]))
        self.assertEqual(collect.query_terms(config["queries"][3]), ["port"])
        self.assertEqual(config["page_caps"]["code"], 1)

    def test_code_queries_are_checked_strictly(self):
        for bad in (
            [code_query(SECRETKEY=1)],
            [code_query(kind="repositories")],
            [code_query(kind="CODE")],
            [code_query(kind=1)],
            [{"kind": "code"}],
            [{"kind": "code", "require_any": ["t"]}],
            [code_query(q="")],
            [code_query(q="x" * 257)],
            [code_query(q="filename:x\ntxt")],
            [code_query(q="filename:x\x00")],
            [code_query(q="http://x filename:y")],
            [code_query(q="javascript:x")],
            [code_query(require_any=[])],
            [code_query(require_any="t")],
            [code_query(require_any=["dup", "DUP"])],
            [code_query(require_any=["tab\there"])],
            ["filename:licence-file.txt", code_query()],
            [code_query(), code_query(q="FILENAME:licence-file.txt")],
        ):
            self.assertRefused({"queries": bad})

    def test_the_code_page_cap_is_1_to_3(self):
        from collector import collect

        for cap in (1, 2, 3):
            self.assertEqual(collect.validate_config(dict(self.BASE, page_caps={"code": cap}))["page_caps"]["code"], cap)
        for cap in (0, 4, "1", True, None):
            self.assertRefused({"page_caps": {"code": cap}}, "page_caps.code")

    def test_placeholders_in_code_queries_are_found(self):
        from collector import collect

        example = collect.load_config(collect.EXAMPLE_CONFIG_PATH)
        placeholder = [q for q in example["queries"] if collect.query_kind(q) == "code"][0]
        self.assertTrue(placeholder["q"].startswith("filename:"))
        tokens, _ = collect.example_placeholders()
        for token in tokens:
            self.assertIn("example", token)
        flagged = (
            code_query(placeholder["q"]),
            code_query("filename:" + placeholder["q"].split(":", 1)[1].upper() + ".txt"),
            code_query("path:" + placeholder["q"].split(":", 1)[1]),
            code_query(require_any=[placeholder["require_any"][0]]),
        )
        for query in flagged:
            config = collect.validate_config({"queries": [query], "roots": []})
            self.assertTrue(collect.config_uses_example_names(config), query)
        for query in (code_query(), code_query("filename:ps3recomp-LICENSE.txt"), "ps3recomp in:name fork:true"):
            self.assertEqual(collect.config_uses_example_names(collect.validate_config({"queries": [query], "roots": []})), [], query)


class Plan(CollectorTestCase):
    def test_a_code_query_is_planned_on_the_code_endpoint_only_with_a_token(self):
        from collector import collect

        config = collect.validate_config({"queries": ["a in:name", code_query()], "roots": []})
        self.assertEqual(
            collect.plan_requests(config),
            [
                {"method": "GET", "path": "/search/repositories", "query": "page=1&per_page=100&q=a+in%3Aname"},
                {"method": "GET", "path": "/search/code", "query": "page=1&per_page=100&q=filename%3Alicence-file.txt"},
            ],
        )
        self.assertEqual([p["path"] for p in collect.plan_requests(config, authenticated=False)], ["/search/repositories"])


class Collection(CollectorTestCase):
    def test_code_results_join_the_candidates_once_each(self):
        # 11 holds two matching files; 12 is listed by the code search and by a repository search.
        items = [code_item(11, "game-dev/port-a"), code_item(11, "game-dev/port-a", path="LICENSES/other.txt"), code_item(12, "game-dev/port-b")]
        routes = code_search(LICENCE, items)
        routes.update(search("port in:name", [api_repo(12, "game-dev/port-b"), api_repo(13, "game-dev/port-c")]))
        for rid, name in ((11, "game-dev/port-a"), (12, "game-dev/port-b"), (13, "game-dev/port-c")):
            routes.update(fetch_routes(rid, name))
        run, report, client, paths = collect_code({"queries": [code_query(), "port in:name"], "roots": []}, routes)
        self.assertEqual(run["searches"][0], {"query": LICENCE, "complete": True, "repository_ids": [11, 12], "kind": "code"})
        self.assertEqual(run["searches"][1], {"query": "port in:name", "complete": True, "repository_ids": [12, 13]})
        self.assertEqual([r["repository_id"] for r in run["repositories"]], [11, 12, 13])
        self.assertEqual(paths.count("/repositories/12"), 1)
        self.assertEqual(len(paths), 2 + 3 * 4)
        self.assertEqual(client.requests_made, run["collection_report"]["requests_made"])
        self.assertIs(run["collection_report"]["complete"], True)
        self.assertNotIn("skipped_searches", run["collection_report"])

    def test_total_count_counts_files_not_repositories(self):
        items = [code_item(11, "game-dev/port-a"), code_item(11, "game-dev/port-a", path="b.txt")]
        routes = code_search(LICENCE, items, total=3)
        routes.update(fetch_routes(11, "game-dev/port-a"))
        run, _, _, _ = collect_code({"queries": [code_query()], "roots": []}, routes)
        self.assertIs(run["searches"][0]["complete"], False)
        routes = code_search(LICENCE, items, total=2)
        routes.update(fetch_routes(11, "game-dev/port-a"))
        run, _, _, _ = collect_code({"queries": [code_query()], "roots": []}, routes)
        self.assertIs(run["searches"][0]["complete"], True)

    def test_an_item_without_a_usable_name_records_the_id_only(self):
        items = [code_item(15, "no slash here"), code_item(16, "game-dev/port-f")]
        routes = code_search(LICENCE, items)
        routes.update(fetch_routes(16, "game-dev/port-f"))
        run, report, _, paths = collect_code({"queries": [code_query()], "roots": []}, routes)
        self.assertEqual(run["searches"][0]["repository_ids"], [15, 16])
        self.assertNotIn("/repositories/15", paths)
        self.assertEqual(report["not_fetched_repository_ids"], [15])
        self.assertEqual((run["collection_report"]["not_fetched"], run["collection_report"]["dropped_items"]), (1, 0))
        self.assertIs(run["collection_report"]["complete"], False)
        # Named by another listing, the same id is fetched under that name.
        routes.update(search("port in:name", [api_repo(15, "game-dev/port-e")]))
        routes.update(fetch_routes(15, "game-dev/port-e"))
        run, report, _, paths = collect_code({"queries": [code_query(), "port in:name"], "roots": []}, routes)
        self.assertIn("/repositories/15", paths)
        self.assertEqual(report["not_fetched_repository_ids"], [])
        self.assertIs(run["collection_report"]["complete"], True)

    def test_items_without_a_repository_id_are_dropped(self):
        bad = [{"name": "x", "path": "x"}, {"repository": None}, {"repository": {"full_name": "a/b"}}, {"repository": {"id": 0, "full_name": "a/b"}}, {"repository": {"id": True, "full_name": "a/b"}}, "text"]
        routes = code_search(LICENCE, bad + [code_item(16, "game-dev/port-f")], total=len(bad) + 1)
        routes.update(fetch_routes(16, "game-dev/port-f"))
        run, report, _, _ = collect_code({"queries": [code_query()], "roots": []}, routes)
        self.assertEqual(run["searches"][0]["repository_ids"], [16])
        self.assertIs(run["searches"][0]["complete"], False)
        self.assertEqual(run["collection_report"]["dropped_items"], len(bad))

    def test_require_any_matches_the_repository_name_and_description(self):
        items = [code_item(11, "game-dev/kz-port", description=None), code_item(12, "util-dev/tools", description="A Killzone port"), code_item(13, "util-dev/misc", description="misc"), code_item(14, "no slash", description="misc")]
        routes = code_search(LICENCE, items)
        routes.update(fetch_routes(11, "game-dev/kz-port"))
        routes.update(fetch_routes(12, "util-dev/tools"))
        run, report, _, paths = collect_code({"queries": [code_query(require_any=["kz", "killzone"])], "roots": []}, routes)
        self.assertEqual(run["searches"][0]["repository_ids"], [11, 12])
        self.assertEqual(run["searches"][0]["filtered_out"], 2)
        self.assertNotIn("/repositories/13", paths)
        self.assertIs(run["collection_report"]["complete"], True)

    def test_deny_applies_to_code_results(self):
        routes = code_search(LICENCE, [code_item(11, "Spam-Owner/copy"), code_item(12, "game-dev/port-b")])
        routes.update(fetch_routes(12, "game-dev/port-b"))
        run, _, _, paths = collect_code({"queries": [code_query()], "roots": [], "deny": {"repositories": [], "owners": ["spam-owner"]}}, routes)
        self.assertEqual(run["searches"][0]["repository_ids"], [12])
        self.assertNotIn("/repositories/11", paths)
        self.assertEqual(run["collection_report"]["denied"], 1)

    def test_the_code_page_cap_bounds_the_walk(self):
        from collector import github

        link = '<https://api.github.com/search/code?page=%d&per_page=100&q=filename%%3Alicence-file.txt>; rel="next"'
        routes = {
            code_url(LICENCE): ok({"total_count": 3, "incomplete_results": False, "items": [code_item(11, "a/b")]}, {"link": link % 2}),
            code_url(LICENCE, 2): ok({"total_count": 3, "incomplete_results": False, "items": [code_item(12, "a/c")]}, {"link": link % 3}),
        }
        for rid, name in ((11, "a/b"), (12, "a/c")):
            routes.update(fetch_routes(rid, name))
        for cap, ids, cursor in ((None, [11], "page=2"), (2, [11, 12], "page=3")):
            config = {"queries": [code_query()], "roots": []}
            if cap is not None:
                config["page_caps"] = {"code": cap, "search": 10}
            run, _, _, paths = collect_code(config, routes)
            self.assertEqual(paths.count("/search/code"), cap or github.DEFAULT_PAGE_CAPS["code"])
            self.assertEqual((run["searches"][0]["repository_ids"], run["searches"][0]["resume_cursor"]), (ids, cursor))
            self.assertIs(run["searches"][0]["complete"], False)

    def test_code_requests_count_in_the_budget(self):
        routes = code_search(LICENCE, [code_item(11, "a/b")])
        routes.update(search("port in:name", []))
        run, report, client, paths = collect_code({"queries": [code_query(), "port in:name"], "roots": []}, routes, max_requests=1)
        self.assertEqual(paths, ["/search/code"])
        self.assertEqual(run["collection_report"]["stopped"], "budget")
        self.assertEqual(report["not_fetched_repository_ids"], [11])


class WithoutAToken(CollectorTestCase):
    def test_code_queries_are_skipped_and_the_run_is_incomplete(self):
        routes = code_search(LICENCE, [code_item(11, "a/b")])
        routes.update(search("port in:name", [api_repo(12, "game-dev/port-b")]))
        routes.update(fetch_routes(12, "game-dev/port-b"))
        run, report, client, paths = collect_code({"queries": [code_query(), "port in:name"], "roots": []}, routes, token=False)
        self.assertNotIn("/search/code", paths)
        self.assertEqual(run["searches"][0], {"query": LICENCE, "complete": False, "repository_ids": [], "kind": "code", "skipped": "no_token", "resume_cursor": "page=1"})
        self.assertEqual(run["searches"][1]["repository_ids"], [12])
        cr = run["collection_report"]
        self.assertEqual((cr["skipped_searches"], cr["stopped"], cr["error_count"], cr["not_fetched"]), (1, None, 0, 0))
        self.assertIs(cr["complete"], False)
        self.assertIs(report["complete"], False)
        state, _ = run_runs([run])
        self.assertIn(LICENCE, state["collection"]["incomplete_searches"])


class RateLimitBuckets(CollectorTestCase):
    def test_paths_map_to_their_buckets(self):
        from collector import github

        self.assertEqual(github.resource_for_path("/search/code"), "code_search")
        self.assertEqual(github.resource_for_path("/search/repositories"), "search")
        self.assertEqual(github.resource_for_path("/repositories/1"), "core")
        self.assertEqual(github._stop_reason(github.RateLimited("x", "code_search")), "code_search_rate_limited")
        self.assertEqual(github._stop_reason(github.RateLimited("x", "search")), "search_rate_limited")

    def test_code_bucket_exhaustion_leaves_repository_searches_and_fetches_running(self):
        routes = {code_url(LICENCE): CODE_LIMITED}
        routes.update(search("port in:name", [api_repo(12, "game-dev/port-b")]))
        routes.update(fetch_routes(12, "game-dev/port-b"))
        config = {"queries": [code_query(), "port in:name", code_query("filename:other.txt")], "roots": []}
        run, report, client, paths = collect_code(config, routes)
        self.assertEqual(paths.count("/search/code"), 1)
        self.assertIn("/search/repositories", paths)
        self.assertIn("/repositories/12", paths)
        self.assertEqual([s.get("resume_cursor") for s in run["searches"]], ["page=1", None, "page=1"])
        self.assertEqual([s["complete"] for s in run["searches"]], [False, True, False])
        self.assertEqual(run["collection_report"]["stopped"], "code_search_rate_limited")
        self.assertIs(client.bucket_exhausted("code_search"), True)
        self.assertIs(client.bucket_exhausted("search"), False)
        self.assertIs(client.rate_limited, False)

    def test_search_bucket_exhaustion_leaves_code_searches_running(self):
        routes = {search_url("port in:name"): SEARCH_LIMITED}
        routes.update(code_search(LICENCE, [code_item(11, "game-dev/port-a")]))
        routes.update(fetch_routes(11, "game-dev/port-a"))
        config = {"queries": ["port in:name", code_query(), "more in:name"], "roots": []}
        run, report, client, paths = collect_code(config, routes)
        self.assertEqual(paths.count("/search/repositories"), 1)
        self.assertEqual(paths.count("/search/code"), 1)
        self.assertEqual(run["searches"][1]["repository_ids"], [11])
        self.assertEqual(run["searches"][2]["resume_cursor"], "page=1")
        self.assertEqual(run["collection_report"]["stopped"], "search_rate_limited")
        self.assertIn("/repositories/11", paths)

    def test_remaining_zero_on_a_code_answer_stops_only_the_next_code_search(self):
        routes = code_search(LICENCE, [code_item(11, "game-dev/port-a")], headers={"x-ratelimit-remaining": "0"})
        routes.update(search("port in:name", []))
        routes.update(fetch_routes(11, "game-dev/port-a"))
        config = {"queries": [code_query(), code_query("filename:other.txt"), "port in:name"], "roots": []}
        run, _, _, paths = collect_code(config, routes)
        self.assertEqual(paths.count("/search/code"), 1)
        self.assertEqual(paths.count("/search/repositories"), 1)
        self.assertIs(run["searches"][0]["complete"], True)
        self.assertEqual(run["searches"][1]["resume_cursor"], "page=1")
        self.assertEqual(run["collection_report"]["stopped"], "code_search_rate_limited")


# The answer the first live run on the Actions token got for its first code search.
CODE_SECONDARY = {"status": 429, "headers": {"retry-after": "2", "x-ratelimit-resource": "code_search", "x-ratelimit-remaining": "10"}, "body": {"message": "slow down"}}
# The fixed jitter the tests inject: the wait before a second retry is max(retry-after, 2 x first wait) + 0.25.
JITTER = 0.25


def code_secondary(retry_after="2", status=429, remaining="10"):
    headers = {"x-ratelimit-resource": "code_search", "x-ratelimit-remaining": remaining}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return {"status": status, "headers": headers, "body": {"message": "slow down"}}


def code_ok(rid, full_name):
    return ok({"total_count": 1, "incomplete_results": False, "items": [code_item(rid, full_name)]}, {"x-ratelimit-resource": "code_search"})


class CodeSearchSecondaryLimit(CollectorTestCase):
    """A secondary limit on /search/code: wait (fake sleep, fixed jitter) and retry up to twice, then give up on code search only."""

    CONFIG = {"queries": ["first in:name", code_query(), "after in:name", code_query("filename:other.txt")], "roots": []}
    OTHER = "filename:other.txt"

    def routes(self, code_answers, other_answers=None):
        routes = {code_url(LICENCE): code_answers}
        routes.update(search("first in:name", [api_repo(12, "game-dev/port-b")]))
        routes.update(search("after in:name", [api_repo(13, "game-dev/port-c")]))
        routes.update(code_search(self.OTHER, [code_item(14, "game-dev/port-d")]))
        if other_answers is not None:
            routes[code_url(self.OTHER)] = other_answers
        for rid, name in ((11, "game-dev/port-a"), (12, "game-dev/port-b"), (13, "game-dev/port-c"), (14, "game-dev/port-d")):
            routes.update(fetch_routes(rid, name))
        return routes

    def collect(self, code_answers, config=None, other_answers=None, jitter=JITTER, **kw):
        sleeps = []
        run, report, client, paths = collect_code(config or self.CONFIG, self.routes(code_answers, other_answers), sleep=sleeps.append, jitter=lambda: jitter, **kw)
        return run, report, client, paths, sleeps

    def assertCodeSearchGivenUp(self, run, report, client, paths):
        self.assertEqual(run["searches"][1], {"query": LICENCE, "complete": False, "repository_ids": [], "kind": "code", "resume_cursor": "page=1"})
        # The later code query is not asked; the repository search after it is.
        self.assertEqual(run["searches"][3], {"query": self.OTHER, "complete": False, "repository_ids": [], "kind": "code", "resume_cursor": "page=1"})
        self.assertEqual([s["complete"] for s in run["searches"]], [True, False, True, False])
        self.assertEqual(paths.count("/search/repositories"), 2)
        self.assertEqual([r["repository_id"] for r in run["repositories"]], [12, 13])
        self.assertEqual([r["fetch_state"] for r in run["repositories"]], ["ok", "ok"])
        cr = run["collection_report"]
        self.assertIsNone(cr["stopped"])
        self.assertIs(cr["complete"], False)
        self.assertEqual(cr["error_count"], 0)
        self.assertEqual(report["code_search_limited"], 2)
        self.assertEqual(report["search_stops"], [])
        self.assertIs(client.rate_limited, False)
        self.assertIs(client.secondary_limited, False)
        self.assertIs(client.code_search_limited, True)
        self.assertIs(client.bucket_exhausted("code_search"), False)
        state, _ = run_runs([run])
        self.assertIn(LICENCE, state["collection"]["incomplete_searches"])

    def assertCodeSearchDone(self, run, report, client):
        self.assertEqual(run["searches"][1], {"query": LICENCE, "complete": True, "repository_ids": [11], "kind": "code"})
        self.assertEqual(run["searches"][3]["repository_ids"], [14])
        self.assertEqual([r["repository_id"] for r in run["repositories"]], [11, 12, 13, 14])
        self.assertIsNone(run["collection_report"]["stopped"])
        self.assertIs(run["collection_report"]["complete"], True)
        self.assertEqual(report["code_search_limited"], 0)
        self.assertIs(client.code_search_limited, False)

    def test_the_first_retry_succeeds(self):
        run, report, client, paths, sleeps = self.collect([CODE_SECONDARY, code_ok(11, "game-dev/port-a")])
        self.assertEqual(sleeps, [2])
        self.assertCodeSearchDone(run, report, client)
        self.assertEqual((client.code_search_retries, client.code_search_waited), (1, 2))
        self.assertEqual(client.summary()["code_search_waited"], 2)
        # The request log shows both attempts, the same URL twice, then the next search.
        code_query_string = code_url(LICENCE).split("?", 1)[1]
        self.assertEqual(client.log[1:4], [
            {"method": "GET", "path": "/search/code", "query": code_query_string},
            {"method": "GET", "path": "/search/code", "query": code_query_string},
            {"method": "GET", "path": "/search/repositories", "query": search_url("after in:name").split("?", 1)[1]},
        ])
        self.assertEqual(run["collection_report"]["requests_made"], 4 + 4 * 4 + 1)

    def test_the_second_retry_succeeds(self):
        run, report, client, paths, sleeps = self.collect([CODE_SECONDARY, CODE_SECONDARY, code_ok(11, "game-dev/port-a")])
        # Before retry 2: max(retry-after 2, 2 x 2) plus the jitter.
        self.assertEqual(sleeps, [2, 4 + JITTER])
        self.assertCodeSearchDone(run, report, client)
        self.assertEqual((client.code_search_retries, client.code_search_waited), (2, 6 + JITTER))
        # The request log shows all three attempts, the same URL each time, then the next search.
        code_query_string = code_url(LICENCE).split("?", 1)[1]
        self.assertEqual(client.log[1:5], [{"method": "GET", "path": "/search/code", "query": code_query_string}] * 3 + [
            {"method": "GET", "path": "/search/repositories", "query": search_url("after in:name").split("?", 1)[1]},
        ])
        self.assertEqual(run["collection_report"]["requests_made"], 5 + 4 * 4 + 1)

    def test_both_retries_fail(self):
        run, report, client, paths, sleeps = self.collect([CODE_SECONDARY, CODE_SECONDARY, CODE_SECONDARY, code_ok(11, "game-dev/port-a")])
        self.assertEqual(sleeps, [2, 4 + JITTER])
        self.assertEqual(paths[:5], ["/search/repositories", "/search/code", "/search/code", "/search/code", "/search/repositories"])
        # No fourth attempt, though the next answer would be a success.
        self.assertEqual(paths.count("/search/code"), 3)
        self.assertEqual(client.code_search_retries, 2)
        self.assertEqual(run["collection_report"]["requests_made"], 3 + 2 + 2 * 4)
        self.assertCodeSearchGivenUp(run, report, client, paths)

    def test_a_403_with_retry_after_is_retried_too(self):
        run, report, client, paths, sleeps = self.collect([code_secondary(status=403)] * 3)
        self.assertEqual(sleeps, [2, 4 + JITTER])
        self.assertCodeSearchGivenUp(run, report, client, paths)

    def test_a_retry_after_up_to_30_seconds_is_waited_for(self):
        # The second live run on the Actions token got retry-after 12.
        run, report, client, paths, sleeps = self.collect([code_secondary("12"), code_ok(11, "game-dev/port-a")])
        self.assertEqual(sleeps, [12])
        self.assertCodeSearchDone(run, report, client)
        run, report, client, paths, sleeps = self.collect([code_secondary("30"), code_ok(11, "game-dev/port-a")])
        self.assertEqual(sleeps, [30])
        self.assertCodeSearchDone(run, report, client)

    def test_a_retry_after_over_30_seconds_is_not_waited_for(self):
        run, report, client, paths, sleeps = self.collect([code_secondary("31"), code_ok(11, "game-dev/port-a")])
        self.assertEqual(sleeps, [])
        self.assertEqual(paths.count("/search/code"), 1)
        self.assertEqual(client.code_search_retries, 0)
        self.assertCodeSearchGivenUp(run, report, client, paths)
        # Nor on the second answer: one wait, then give up.
        run, report, client, paths, sleeps = self.collect([CODE_SECONDARY, code_secondary("31"), code_ok(11, "game-dev/port-a")])
        self.assertEqual(sleeps, [2])
        self.assertEqual(paths.count("/search/code"), 2)
        self.assertCodeSearchGivenUp(run, report, client, paths)

    def test_the_second_wait_follows_the_larger_of_retry_after_and_doubling(self):
        for answers, expected in (
            ([code_secondary("2"), code_secondary("10")], [2, 10 + JITTER]),
            ([code_secondary("12"), code_secondary("2")], [12, 24 + JITTER]),
            # Doubling past 30 is capped at 30, jitter included.
            ([code_secondary("20"), code_secondary("2")], [20, 30]),
            ([code_secondary("15"), code_secondary("2")], [15, 30]),
        ):
            with self.subTest(answers=[a["headers"]["retry-after"] for a in answers]):
                run, report, client, paths, sleeps = self.collect(answers + [code_ok(11, "game-dev/port-a")])
                self.assertEqual(sleeps, expected)
                self.assertCodeSearchDone(run, report, client)

    def test_a_missing_retry_after_waits_5_seconds(self):
        for garbled in (None, "soon"):
            with self.subTest(retry_after=garbled):
                run, report, client, paths, sleeps = self.collect([code_secondary(garbled), code_ok(11, "game-dev/port-a")])
                self.assertEqual(sleeps, [5])
                self.assertCodeSearchDone(run, report, client)
                # Missing again on the second answer: max(5, 2 x 5) plus the jitter.
                run, report, client, paths, sleeps = self.collect([code_secondary(garbled)] * 3)
                self.assertEqual(sleeps, [5, 10 + JITTER])
                self.assertEqual(paths.count("/search/code"), 3)
                self.assertCodeSearchGivenUp(run, report, client, paths)

    def test_the_jitter_comes_from_the_injected_function(self):
        from collector import github

        for jitter, added in ((0.75, 0.75), (0, 0), (5, 1), (-1, 0)):
            with self.subTest(jitter=jitter):
                _, _, _, _, sleeps = self.collect([CODE_SECONDARY] * 3, jitter=jitter)
                self.assertEqual(sleeps, [2, 4 + added])
        client, _ = make_client({})
        self.assertIs(client._jitter, github._default_jitter)
        values = [github._default_jitter() for _ in range(200)]
        self.assertTrue(all(0 <= v <= github.MAX_CODE_SEARCH_JITTER for v in values))
        self.assertGreater(len(set(values)), 1)

    def test_all_code_search_waits_of_a_run_stay_within_60_seconds(self):
        from collector import github

        self.assertEqual((github.MAX_CODE_SEARCH_WAIT, github.MAX_CODE_SEARCH_RETRIES, github.DEFAULT_CODE_SEARCH_WAIT, github.MAX_CODE_SEARCH_WAIT_TOTAL), (30, 2, 5, 60))
        first = [code_secondary("20"), code_secondary("2"), code_ok(11, "game-dev/port-a")]
        # 20 + 30 = 50 waited; the other query's 12 would pass 60: not waited for, that query only is incomplete.
        run, report, client, paths, sleeps = self.collect(first, other_answers=[code_secondary("12"), code_ok(14, "game-dev/port-d")])
        self.assertEqual(sleeps, [20, 30])
        self.assertEqual(client.code_search_waited, 50)
        self.assertEqual(paths.count("/search/code"), 4)
        self.assertEqual(run["searches"][1]["repository_ids"], [11])
        self.assertEqual(run["searches"][3], {"query": self.OTHER, "complete": False, "repository_ids": [], "kind": "code", "resume_cursor": "page=1"})
        self.assertEqual(report["code_search_limited"], 1)
        self.assertIsNone(run["collection_report"]["stopped"])
        self.assertEqual([r["repository_id"] for r in run["repositories"]], [11, 12, 13])
        # Exactly 60 is allowed.
        run, report, client, paths, sleeps = self.collect(first, other_answers=[code_secondary("10"), code_ok(14, "game-dev/port-d")])
        self.assertEqual(sleeps, [20, 30, 10])
        self.assertEqual(report["code_search_limited"], 0)
        # The bound also stops a second retry: 50 + 5 waited, the next 10.25 would pass 60.
        run, report, client, paths, sleeps = self.collect(first, other_answers=[code_secondary("5")] * 2 + [code_ok(14, "game-dev/port-d")])
        self.assertEqual(sleeps, [20, 30, 5])
        self.assertEqual(paths.count("/search/code"), 5)
        self.assertEqual(report["code_search_limited"], 1)

    def test_every_retry_counts_toward_the_budget(self):
        # Budget 4: first search, the code search and both retries; the run then stops on the budget.
        run, report, client, paths, sleeps = self.collect([CODE_SECONDARY] * 3, max_requests=4)
        self.assertEqual(paths, ["/search/repositories", "/search/code", "/search/code", "/search/code"])
        self.assertEqual(sleeps, [2, 4 + JITTER])
        self.assertEqual(run["collection_report"]["stopped"], "budget")
        # Budget 3: the second retry cannot be asked, so it is not waited for.
        run, report, client, paths, sleeps = self.collect([CODE_SECONDARY] * 3, max_requests=3)
        self.assertEqual(paths, ["/search/repositories", "/search/code", "/search/code"])
        self.assertEqual(sleeps, [2])
        self.assertEqual(client.code_search_retries, 1)
        self.assertEqual(run["collection_report"]["stopped"], "budget")
        # Budget 2: no retry can be asked, so nothing is waited for.
        run, report, client, paths, sleeps = self.collect([CODE_SECONDARY] * 3, max_requests=2)
        self.assertEqual(paths, ["/search/repositories", "/search/code"])
        self.assertEqual(sleeps, [])
        self.assertEqual(run["collection_report"]["stopped"], "budget")

    def test_a_limit_on_a_later_code_page_keeps_the_first_page(self):
        link = '<https://api.github.com/search/code?page=2&per_page=100&q=filename%3Alicence-file.txt>; rel="next"'
        first = ok({"total_count": 2, "incomplete_results": False, "items": [code_item(11, "game-dev/port-a")]}, {"link": link, "x-ratelimit-resource": "code_search"})
        routes = {code_url(LICENCE): first, code_url(LICENCE, 2): [CODE_SECONDARY] * 3}
        routes.update(search("after in:name", [api_repo(13, "game-dev/port-c")]))
        for rid, name in ((11, "game-dev/port-a"), (13, "game-dev/port-c")):
            routes.update(fetch_routes(rid, name))
        sleeps = []
        config = {"queries": [code_query(), "after in:name"], "roots": [], "page_caps": {"code": 2, "search": 10}}
        run, report, client, paths = collect_code(config, routes, sleep=sleeps.append, jitter=lambda: JITTER)
        self.assertEqual(sleeps, [2, 4 + JITTER])
        self.assertEqual(run["searches"][0], {"query": LICENCE, "complete": False, "repository_ids": [11], "kind": "code", "resume_cursor": "page=2"})
        self.assertEqual(paths[:5], ["/search/code"] * 4 + ["/search/repositories"])
        self.assertEqual([r["repository_id"] for r in run["repositories"]], [11, 13])
        self.assertIsNone(run["collection_report"]["stopped"])
        self.assertEqual(report["code_search_limited"], 1)

    def test_secondary_limits_elsewhere_still_stop_everything(self):
        cases = (
            ("/search/repositories", search_url("first in:name"), {"status": 429, "headers": {"retry-after": "2", "x-ratelimit-resource": "search", "x-ratelimit-remaining": "10"}, "body": {}}),
            ("/search/repositories", search_url("first in:name"), {"status": 429, "headers": {"x-ratelimit-resource": "search", "x-ratelimit-remaining": "10"}, "body": {}}),
            ("/repositories/12", "GET /repositories/12", {"status": 403, "headers": {"retry-after": "2", "x-ratelimit-remaining": "4000"}, "body": {}}),
        )
        for label, key, answer in cases:
            with self.subTest(path=label):
                routes = self.routes(ok({"total_count": 0, "incomplete_results": False, "items": []}, {"x-ratelimit-resource": "code_search"}))
                routes[key] = answer
                sleeps = []
                run, report, client, paths = collect_code(self.CONFIG, routes, sleep=sleeps.append)
                self.assertEqual(sleeps, [])
                self.assertEqual(paths.count(label), 1)
                self.assertEqual(run["collection_report"]["stopped"], "rate_limited")
                self.assertIs(client.secondary_limited, True)
                self.assertEqual(paths[-1], label)

    def test_remaining_zero_on_code_search_keeps_the_bucket_rule(self):
        run, report, client, paths, sleeps = self.collect([dict(CODE_LIMITED, status=429), CODE_SECONDARY])
        self.assertEqual(sleeps, [])
        self.assertEqual(paths.count("/search/code"), 1)
        self.assertEqual(run["collection_report"]["stopped"], "code_search_rate_limited")
        self.assertIs(client.bucket_exhausted("code_search"), True)
        self.assertEqual(report["code_search_limited"], 0)

    def test_the_client_refuses_code_search_after_giving_up(self):
        from collector import github

        sleeps = []
        client, transport = make_client({code_url(LICENCE): [CODE_SECONDARY]}, sleep=sleeps.append, jitter=lambda: JITTER)
        for _ in range(2):
            with self.assertRaises(github.CodeSearchLimited):
                client.get("/search/code", {"q": LICENCE, "per_page": 100, "page": 1})
        self.assertEqual((len(transport.calls), sleeps), (3, [2, 4 + JITTER]))
        self.assertIs(client.rate_limited, False)
        self.assertEqual(github._stop_reason(github.CodeSearchLimited("x")), "code_search_secondary_limited")
        self.assertIs(github.Client(transport)._sleep, time.sleep)


class RunFormat(CollectorTestCase):
    REPORT = {"complete": True, "stopped": None, "error_count": 0, "dropped_items": 0, "not_fetched": 0, "requests_made": 3}
    BASE = {"run_at": RUN_AT, "roots": [], "searches": [{"query": "x in:name", "complete": True, "repository_ids": [5]}], "repositories": []}

    def test_older_runs_load_and_code_entries_are_checked(self):
        from collector import schema

        schema.validate_run(dict(self.BASE, collection_report=self.REPORT))
        code = {"query": LICENCE, "complete": True, "repository_ids": [5, 6], "kind": "code"}
        skipped = {"query": "filename:b.txt", "complete": False, "repository_ids": [], "kind": "code", "skipped": "no_token", "resume_cursor": "page=1"}
        report = dict(self.REPORT, complete=False, stopped="code_search_rate_limited", skipped_searches=1)
        out = schema.validate_run(dict(self.BASE, searches=self.BASE["searches"] + [code, skipped], collection_report=report))
        self.assertEqual([s.get("kind") for s in out["searches"]], [None, "code", "code"])
        self.assertEqual(out["collection_report"]["skipped_searches"], 1)
        for bad in (
            dict(code, kind="repositories"),
            dict(code, skipped="no_token"),
            dict(skipped, kind=None),
            dict(skipped, repository_ids=[5]),
            dict(self.BASE["searches"][0], skipped="no_token"),
            dict(skipped, skipped="other"),
        ):
            bad = {k: v for k, v in bad.items() if v is not None}
            with self.assertRaises(schema.SchemaError, msg=bad):
                schema.validate_run(dict(self.BASE, searches=[bad]))
        for bad in (dict(report, skipped_searches=-1), dict(report, stopped="code_search")):
            with self.assertRaises(schema.SchemaError):
                schema.validate_run(dict(self.BASE, collection_report=bad))

    def test_a_run_with_code_provenance_applies_through_the_event_model(self):
        routes = code_search(LICENCE, [code_item(11, "game-dev/port-a")])
        routes.update(fetch_routes(11, "game-dev/port-a"))
        run, _, _, _ = collect_code({"queries": [code_query()], "roots": []}, routes)
        state, events = run_runs([run])
        self.assertEqual(events, [[]])
        self.assertEqual(state["collection"]["searches"][LICENCE]["repository_ids"], [11])
        self.assertIn(LICENCE, state["repositories"]["11"]["discovery_queries"])

"""Regression tests for the first live run's findings (A-E) and the test hardening it asked for.

No network: tests/helpers.py blocks sockets at import and per test; the
live path is driven through recorded transports only.
"""

import ast
import copy
import importlib
import os
import socket
import tempfile
import unittest.mock

import helpers
from helpers import CollectorTestCase, run_runs
from test_github import FRAME, RUN_AT, SENTINEL, SKY, _run_cli, api_commit, api_repo, config_for_network, make_client, ok, routes_for_network, search_url, sha
from test_github_hardening import collect_with, repo_100

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SINCE = "2026-10-06T00%3A00%3A00Z"
SKY_COMMITS_P1 = "GET /repos/game-org/skyfall-recomp/commits?page=1&per_page=100&sha=main&since=" + SINCE
FRAME_FORKS_P1 = "GET /repos/frame-org/ps3recomp-example/forks?page=1&per_page=100"
SKY_RELEASES = "GET /repos/game-org/skyfall-recomp/releases?per_page=30"
# The exact shape GitHub answers with: the repository spelled by id, the query reordered, a rel="last" alongside.
ID_LINK_QUERY = "sha=main&since=" + SINCE + "&per_page=100&page=2"
ID_LINK = (
    '<https://api.github.com/repositories/100/commits?%s>; rel="next", '
    '<https://api.github.com/repositories/100/commits?sha=main&since=%s&per_page=100&page=3>; rel="last"' % (ID_LINK_QUERY, SINCE)
)
ID_P2 = "GET /repositories/100/commits?" + ID_LINK_QUERY
SEARCH_LIMITED = {"status": 403, "headers": {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000", "x-ratelimit-resource": "search"}, "body": {"message": "limit"}}
CORE_LIMITED = {"status": 403, "headers": {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000", "x-ratelimit-resource": "core"}, "body": {"message": "limit"}}


def two_query_config(**extra):
    return config_for_network(queries=["ps3recomp in:name", "ps3 recomp in:description"], **extra)


class A_RepositoriesIdLinks(CollectorTestCase):
    def test_recorded_github_link_shape_is_followed_and_the_walk_completes(self):
        routes = routes_for_network()
        routes[SKY_COMMITS_P1] = ok([api_commit(0xB, "2026-11-04T12:00:00Z")], {"link": ID_LINK})
        routes[ID_P2] = ok([api_commit(0xA, "2026-11-03T12:00:00Z")])
        run, report, _, transport = collect_with(routes)
        r100 = repo_100(run)
        self.assertIs(r100["commits_complete"], True)
        self.assertEqual([c["sha"] for c in r100["commits"]], [sha(0xA), sha(0xB)])
        self.assertEqual(report["error_count"], 0)
        self.assertEqual(report["errors"], [])
        paths = [c["url"].split("api.github.com", 1)[1] for c in transport.calls]
        self.assertIn("/repositories/100/commits?" + ID_LINK_QUERY, paths)
        self.assertIs(run["collection_report"]["complete"], True)

    def test_forks_walk_follows_the_id_spelling_too(self):
        routes = routes_for_network()
        link = '<https://api.github.com/repositories/90/forks?per_page=100&page=2>; rel="next"'
        routes[FRAME_FORKS_P1] = ok([api_repo(100, SKY, fork=True, parent=90, source=90)], {"link": link})
        routes["GET /repositories/90/forks?per_page=100&page=2"] = ok([api_repo(101, "other-org/skyfall-two", fork=True, parent=90, source=90)])
        routes["GET /repositories/101"] = ok(api_repo(101, "other-org/skyfall-two", fork=True, parent=90, source=90))
        run, report, _, _ = collect_with(routes)
        self.assertIs(run["roots"][0]["forks_scan_complete"], True)
        self.assertEqual(run["roots"][0]["found_repository_ids"], [100, 101])
        self.assertNotIn({"path": "/repos/frame-org/ps3recomp-example/forks", "reason": "response"}, report["errors"])

    def test_another_id_in_the_link_is_refused_and_never_requested(self):
        routes = routes_for_network()
        routes[SKY_COMMITS_P1] = ok([api_commit(0xB, "2026-11-04T12:00:00Z")], {"link": ID_LINK.replace("/repositories/100/", "/repositories/101/")})
        routes["GET /repositories/101/commits?" + ID_LINK_QUERY] = ok([api_commit(0xA, "2026-11-03T12:00:00Z")])
        run, report, _, transport = collect_with(routes)
        r100 = repo_100(run)
        self.assertIs(r100["commits_complete"], False)
        self.assertEqual([c["sha"] for c in r100["commits"]], [sha(0xB)])
        self.assertIn({"path": "/repos/game-org/skyfall-recomp/commits", "reason": "response"}, report["errors"])
        self.assertEqual([c for c in transport.calls if "/repositories/101/" in c["url"]], [])

    def test_equivalence_rules(self):
        from collector import github

        accepted = (
            ("https://api.github.com/repositories/100/commits?page=2&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, 100),
            ("https://api.github.com/repositories/100/forks?page=2&per_page=100", "/repos/o/r/forks", "page=1&per_page=100", 2, 100),
            ("https://api.github.com/repositories/100/releases?page=2&per_page=30", "/repos/o/r/releases", "page=1&per_page=30", 2, 100),
            ("https://api.github.com/repos/o/r/commits?page=2&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, None),
        )
        self.assertEqual(len(accepted), 4)
        for args in accepted:
            self.assertEqual(github.check_next_url(*args), args[0], args)
        refused = (
            ("https://api.github.com/repositories/101/commits?page=2&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, 100),
            ("https://api.github.com/repositories/0100/commits?page=2&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, 100),
            ("https://api.github.com/repositories/100/releases?page=2&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, 100),
            ("https://api.github.com/repositories/100/commits/extra?page=2&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, 100),
            ("https://api.github.com/repositories/100/commits?page=2&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, None),
            ("https://api.github.com/repositories/100/commits?page=2&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, "100"),
            ("https://api.github.com/repositories/100/commits?page=2&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, True),
            ("https://api.github.com/repositories/100/commits?page=3&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, 100),
            ("https://api.github.com/repositories/100/commits?page=2&per_page=50", "/repos/o/r/commits", "page=1&per_page=100", 2, 100),
            ("https://api.github.com.evil.example/repositories/100/commits?page=2&per_page=100", "/repos/o/r/commits", "page=1&per_page=100", 2, 100),
            ("https://api.github.com/repositories/100/commits?page=2&per_page=100", "/search/repositories", "page=1&per_page=100", 2, 100),
        )
        self.assertEqual(len(refused), 11)
        for args in refused:
            with self.assertRaises(github.TransportRefused, msg=args):
                github.check_next_url(*args)
        self.assertIs(github.equivalent_path("/repositories/100", "/repositories/100", 100), True)
        self.assertIs(github.equivalent_path("/repositories/100", "/repositories/100/commits", 100), False)


class B_PerResourceRateLimits(CollectorTestCase):
    def test_search_bucket_exhaustion_continues_on_core_endpoints(self):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = SEARCH_LIMITED
        run, report, client, transport = collect_with(routes, two_query_config())
        self.assertEqual(report["stopped"], "search_rate_limited")
        self.assertEqual(
            run["searches"],
            [
                {"query": "ps3recomp in:name", "complete": False, "repository_ids": [], "resume_cursor": "page=1"},
                {"query": "ps3 recomp in:description", "complete": False, "repository_ids": [], "resume_cursor": "page=1"},
            ],
        )
        self.assertEqual(len([c for c in transport.calls if "/search/" in c["url"]]), 1)
        self.assertEqual(run["roots"][0]["found_repository_ids"], [100])
        self.assertIs(run["roots"][0]["forks_scan_complete"], True)
        self.assertEqual([r["fetch_state"] for r in run["repositories"]], ["ok", "ok"])
        self.assertEqual(report["not_fetched_repository_ids"], [])
        self.assertEqual(run["collection_report"]["stopped"], "search_rate_limited")
        self.assertIs(run["collection_report"]["complete"], False)
        self.assertIs(client.rate_limited, False)
        self.assertIs(client.bucket_exhausted("search"), True)
        state, events = run_runs([run])
        self.assertEqual(state["collection"]["last_run"]["stopped"], "search_rate_limited")
        self.assertIs(state["collection"]["last_run"]["complete"], False)
        self.assertEqual(events, [[]])  # a first run is the baseline
        self.assertEqual(sorted(state["repositories"]), ["100", "90"])

    def test_search_exhaustion_on_page_two_keeps_page_one_and_a_cursor(self):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = ok(
            {"total_count": 3, "incomplete_results": False, "items": [api_repo(90, FRAME), api_repo(100, SKY)]},
            {"link": '<https://api.github.com/search/repositories?page=2&per_page=100&q=ps3recomp+in%3Aname>; rel="next"'},
        )
        routes[search_url("ps3recomp in:name", 2)] = SEARCH_LIMITED
        run, report, _, transport = collect_with(routes, two_query_config())
        self.assertEqual(report["stopped"], "search_rate_limited")
        self.assertEqual(run["searches"][0], {"query": "ps3recomp in:name", "complete": False, "repository_ids": [90, 100], "resume_cursor": "page=2"})
        self.assertEqual(run["searches"][1]["resume_cursor"], "page=1")
        self.assertEqual(len([c for c in transport.calls if "/search/" in c["url"]]), 2)
        self.assertEqual(sorted(r["repository_id"] for r in run["repositories"]), [90, 100])

    def test_remaining_zero_on_a_successful_search_stops_the_next_search_before_it_is_requested(self):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = ok(
            {"total_count": 2, "incomplete_results": False, "items": [api_repo(90, FRAME), api_repo(100, SKY)]},
            {"x-ratelimit-remaining": "0", "x-ratelimit-resource": "search"},
        )
        run, report, client, transport = collect_with(routes, two_query_config())
        self.assertEqual(report["stopped"], "search_rate_limited")
        self.assertIs(run["searches"][0]["complete"], True)
        self.assertNotIn("resume_cursor", run["searches"][0])
        self.assertEqual(run["searches"][1], {"query": "ps3 recomp in:description", "complete": False, "repository_ids": [], "resume_cursor": "page=1"})
        self.assertEqual(len([c for c in transport.calls if "/search/" in c["url"]]), 1)
        self.assertEqual([r["fetch_state"] for r in run["repositories"]], ["ok", "ok"])
        self.assertEqual(client.limits["search"]["remaining"], 0)
        self.assertEqual(client.limits["core"]["remaining"], 4000)

    def test_core_exhaustion_still_stops_the_whole_run(self):
        routes = routes_for_network()
        routes["GET /repositories/90"] = CORE_LIMITED
        run, report, client, _ = collect_with(routes)
        self.assertEqual(report["stopped"], "rate_limited")
        self.assertEqual([(r["repository_id"], r["fetch_state"]) for r in run["repositories"]], [(90, "rate_limited")])
        self.assertEqual(report["not_fetched_repository_ids"], [100])
        self.assertIs(client.rate_limited, True)

    def test_a_core_stop_after_a_search_stop_is_reported_as_the_core_stop(self):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = SEARCH_LIMITED
        routes["GET /repositories/90"] = CORE_LIMITED
        run, report, _, _ = collect_with(routes)
        self.assertEqual(report["stopped"], "rate_limited")
        self.assertEqual(run["collection_report"]["stopped"], "rate_limited")
        self.assertEqual(run["searches"][0]["resume_cursor"], "page=1")

    def test_secondary_limit_on_a_search_stops_the_whole_run(self):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = {"status": 403, "headers": {"retry-after": "60", "x-ratelimit-resource": "search", "x-ratelimit-remaining": "25"}, "body": {}}
        run, report, client, transport = collect_with(routes)
        self.assertEqual(report["stopped"], "rate_limited")
        self.assertEqual(run["roots"], [])
        self.assertEqual(run["repositories"], [])
        self.assertEqual(report["not_fetched_repository_ids"], [90])
        self.assertEqual(len(transport.calls), 1)
        self.assertIs(client.secondary_limited, True)
        self.assertEqual(client.retry_after, 60)

    def test_an_unidentified_limit_counts_as_core(self):
        from collector import github

        client, _ = make_client({})
        client._note_rate_limit(github.Response(200, {"x-ratelimit-remaining": "0"}, b"{}"), "/search/repositories")
        self.assertIs(client.rate_limited, True)
        self.assertEqual(sorted(client.exhausted), ["core"])

    def test_client_refuses_an_exhausted_bucket_before_the_transport_and_keeps_the_other(self):
        from collector import github

        routes = {"GET /search/repositories?page=1&per_page=100&q=x": ok({"items": []}, {"x-ratelimit-remaining": "0", "x-ratelimit-resource": "search"}), "GET /repositories/90": ok(api_repo(90, FRAME))}
        client, transport = make_client(routes)
        client.get_json("/search/repositories", {"q": "x", "per_page": 100, "page": 1})
        self.assertEqual(client.limits, {"search": {"remaining": 0, "reset": 1800000000}})
        with self.assertRaises(github.RateLimited) as ctx:
            client.get_json("/search/repositories", {"q": "y", "per_page": 100, "page": 1})
        self.assertEqual(ctx.exception.resource, "search")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(client.get_json("/repositories/90").status, 200)
        self.assertEqual(client.summary()["exhausted_resources"], ["search"])
        self.assertIs(client.summary()["rate_limited"], False)
        self.assertEqual(github._stop_reason(ctx.exception), "search_rate_limited")
        self.assertEqual(github._stop_reason(github.RateLimited("x")), "rate_limited")

    def test_schema_knows_the_new_stop_reason_and_the_search_cursor(self):
        from collector import schema

        self.assertIn("search_rate_limited", schema.STOP_REASONS)
        base = {"run_at": RUN_AT, "roots": [], "searches": [{"query": "q", "complete": False, "repository_ids": [], "resume_cursor": "page=2"}], "repositories": []}
        report = {"complete": False, "stopped": "search_rate_limited", "error_count": 0, "dropped_items": 0, "not_fetched": 0, "requests_made": 1}
        out = schema.validate_run(dict(base, collection_report=report))
        self.assertEqual(out["collection_report"]["stopped"], "search_rate_limited")
        self.assertEqual(out["searches"][0]["resume_cursor"], "page=2")
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(dict(base, collection_report=dict(report, stopped="search_limited")))
        hostile = copy.deepcopy(base)
        hostile["searches"][0]["resume_cursor"] = "javascript:alert(1)//<x>"
        out = schema.validate_run(hostile)
        self.assertIsNone(out["searches"][0]["resume_cursor"])
        self.assertEqual(out["rejected_fields"], [{"path": "searches[0].resume_cursor", "reason": "invalid_cursor"}])
        state, _ = run_runs([dict(base, collection_report=report)])
        self.assertEqual(state["collection"]["last_run"]["stopped"], "search_rate_limited")
        schema.validate_state(state)


class C_EmptyRepositories(CollectorTestCase):
    def test_409_on_commits_is_an_empty_complete_listing(self):
        routes = routes_for_network()
        routes[SKY_COMMITS_P1] = {"status": 409, "headers": {"x-ratelimit-remaining": "4000"}, "body": {"message": "Git Repository is empty."}}
        run, report, _, _ = collect_with(routes)
        r100 = repo_100(run)
        self.assertEqual(r100["commits"], [])
        self.assertIs(r100["commits_complete"], True)
        self.assertEqual(r100["fetch_state"], "ok")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["error_count"], 0)
        self.assertIs(run["collection_report"]["complete"], True)
        state, _ = run_runs([run])
        self.assertIs(state["repositories"]["100"]["partial"], False)

    def test_409_elsewhere_stays_a_failure(self):
        routes = routes_for_network()
        routes[SKY_RELEASES] = {"status": 409, "headers": {}, "body": {}}
        run, report, _, _ = collect_with(routes)
        self.assertIsNone(repo_100(run)["releases"])
        self.assertIn({"path": "/repos/game-org/skyfall-recomp/releases", "reason": "status"}, report["errors"])
        routes = routes_for_network()
        routes["GET /repositories/100"] = {"status": 409, "headers": {}, "body": {}}
        run, report, _, _ = collect_with(routes)
        self.assertEqual(repo_100(run)["fetch_state"], "failed")
        self.assertIsNone(repo_100(run)["commits"])


class D_CodePathFixes(CollectorTestCase):
    def test_pages_without_items_after_a_stop_raise_the_stop(self):
        from collector import collect, github

        for reason, expect_roots in (("rate_limited", 0), ("budget", 0), ("network", 0), ("search_rate_limited", 1)):

            class Stopping(github.Client):
                def paginate(self, path, params, cap, expect="array", items_key=None, repository_id=None):
                    if path.startswith("/search/"):
                        return github.Pages(None, False, None, None, reason)
                    return github.Client.paginate(self, path, params, cap, expect, items_key, repository_id)

            client = Stopping(github.RecordedTransport(routes_for_network()), max_requests=50)
            run, report = collect.collect(client, config_for_network(), RUN_AT)
            self.assertEqual(report["stopped"], reason, reason)
            self.assertEqual(len(run["roots"]), expect_roots, reason)
            self.assertEqual(run["searches"][0]["complete"], False)

    def test_effective_budget_is_the_smaller_of_flag_and_config(self):
        from collector import cli, github

        with tempfile.TemporaryDirectory() as tmp:
            fake_repo = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(fake_repo, "data"))
            seen = []

            def factory(*args, **kwargs):
                return github.RecordedTransport(routes_for_network())

            real_client = github.Client

            def spy(*args, **kwargs):
                client = real_client(*args, **kwargs)
                seen.append(client)
                return client

            for config_cap, flag, expected in ((3, 50, 3), (50, 2, 2), (50, 50, 50)):
                config_path = os.path.join(tmp, "config-%d-%d.json" % (config_cap, flag))
                with open(config_path, "w", encoding="utf-8") as fh:
                    import json

                    json.dump(config_for_network(max_requests=config_cap), fh)
                out_path = os.path.join(fake_repo, "data", "run-%d-%d.json" % (config_cap, flag))
                with unittest.mock.patch.object(cli, "REPO_ROOT", fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", os.path.join(fake_repo, "data")):
                    with unittest.mock.patch.object(github, "LiveTransport", factory), unittest.mock.patch.object(github, "Client", spy):
                        code, out, err = _run_cli(["collect", "--config", config_path, "--live", "--max-requests", str(flag), "--out", out_path], env={"GITHUB_TOKEN": SENTINEL})
                self.assertIn(code, (0, 3), err)
                self.assertEqual(seen[-1].max_requests, expected, (config_cap, flag))
                self.assertLessEqual(seen[-1].requests_made, expected)
                import json

                self.assertEqual(json.loads(out)["effective_max_requests"], expected)
            self.assertEqual(len(seen), 3)
            self.assertEqual(seen[0].requests_made, 3)
        code, help_text, _ = _run_cli(["collect", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("smaller of this and the config", help_text)
        self.assertIn("POSIX", cli.build_parser().format_help())

    def test_validate_run_returns_a_normalised_copy_and_leaves_its_argument_alone(self):
        from collector import schema

        data = {"run_at": RUN_AT, "roots": [{"repository_id": 90, "full_name": FRAME, "forks_scan_complete": False, "found_repository_ids": [], "resume_cursor": "javascript:alert(1)"}], "searches": [], "repositories": []}
        original = copy.deepcopy(data)
        out = schema.validate_run(data)
        self.assertEqual(data, original)
        self.assertIsNot(out, data)
        self.assertIsNone(out["roots"][0]["resume_cursor"])
        self.assertEqual(out["rejected_fields"][0]["reason"], "invalid_cursor")
        self.assertNotIn("rejected_fields", data)

    def test_live_writes_are_refused_off_posix(self):
        from collector import cli

        with unittest.mock.patch.object(cli.os, "name", "nt"):
            with self.assertRaises(cli.RefusedInput) as ctx:
                cli.resolve_out_path("data/x.json")
            self.assertIn("POSIX", str(ctx.exception))
            with self.assertRaises(cli.RefusedInput) as ctx:
                cli.write_run_file({}, os.path.join(REPO_ROOT, "data", "x.json"))
            self.assertIn("POSIX", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(REPO_ROOT, "data", "x.json")))
        self.assertIn("POSIX", cli.__doc__)

    def test_paths_are_defined_once_and_refuse_helper_is_gone(self):
        from collector import cli, collect, paths

        self.assertFalse(hasattr(cli, "_refuse"))
        self.assertEqual(cli.REPO_ROOT, paths.REPO_ROOT)
        self.assertEqual(collect.REPO_ROOT, paths.REPO_ROOT)
        self.assertEqual(os.path.realpath(paths.REPO_ROOT), os.path.realpath(REPO_ROOT))
        for name in ("cli.py", "collect.py"):
            with open(os.path.join(REPO_ROOT, "collector", name), "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=name)
            assigned = [t.id for node in tree.body if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Name)]
            self.assertNotIn("REPO_ROOT", assigned, name)
            self.assertNotIn("DATA_ROOT", assigned, name)
        self.assertLess(len(cli.__doc__.splitlines()), 16)

    def test_package_exports_match_the_modules(self):
        import collector

        modules = sorted(f[:-3] for f in os.listdir(os.path.join(REPO_ROOT, "collector")) if f.endswith(".py") and f != "__init__.py")
        self.assertEqual(sorted(collector.__all__), modules)
        for name in modules:
            importlib.import_module("collector." + name)
        self.assertNotIn("site-draft", collector.model.__doc__)
        self.assertNotIn(".local", collector.model.__doc__)
        self.assertIn("LiveTransport", collector.__doc__)


class E_Hardening(CollectorTestCase):
    def test_commits_are_output_oldest_first(self):
        from collector import collect

        items = [api_commit(3, "2026-11-03T12:00:00Z"), api_commit(2, "2026-11-02T12:00:00Z"), api_commit(1, "2026-11-01T12:00:00Z")]
        commits, dropped = collect.adapt_commits(items, "/x")
        self.assertEqual(dropped, 0)
        dates = [c["committed_at"] for c in commits]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual([c["sha"] for c in commits], [sha(1), sha(2), sha(3)])
        run, _, _, _ = collect_with(routes_for_network())
        for repo in run["repositories"]:
            dates = [c["committed_at"] for c in repo["commits"]]
            self.assertEqual(len(dates), 2)
            self.assertEqual(dates, sorted(dates))

    def test_403_without_retry_after_is_blocked_and_the_run_continues(self):
        routes = routes_for_network()
        routes["GET /repositories/90"] = {"status": 403, "headers": {"x-ratelimit-remaining": "4000"}, "body": {"message": "forbidden"}}
        run, report, client, _ = collect_with(routes)
        self.assertIsNone(report["stopped"])
        self.assertEqual([(r["repository_id"], r["fetch_state"]) for r in run["repositories"]], [(90, "blocked"), (100, "ok")])
        self.assertIs(client.rate_limited, False)

    def test_403_with_retry_after_stops_the_run(self):
        routes = routes_for_network()
        routes["GET /repositories/90"] = {"status": 403, "headers": {"x-ratelimit-remaining": "4000", "retry-after": "30"}, "body": {"message": "secondary"}}
        run, report, client, transport = collect_with(routes)
        self.assertEqual(report["stopped"], "rate_limited")
        self.assertEqual([(r["repository_id"], r["fetch_state"]) for r in run["repositories"]], [(90, "rate_limited")])
        self.assertEqual(report["not_fetched_repository_ids"], [100])
        self.assertIs(client.secondary_limited, True)
        self.assertEqual(client.retry_after, 30)
        self.assertEqual(len(transport.calls), 3)

    def test_network_guard_is_in_force_and_restorable(self):
        with self.assertRaises(helpers.NetworkBlocked):
            socket.getaddrinfo("api.github.com", 443)
        with self.assertRaises(helpers.NetworkBlocked):
            socket.create_connection(("127.0.0.1", 9))
        with socket.socket() as sock, self.assertRaises(helpers.NetworkBlocked):
            sock.connect(("127.0.0.1", 9))

        def marker(*args, **kwargs):
            raise AssertionError("marker reached")

        saved = socket.getaddrinfo
        socket.getaddrinfo = marker
        try:
            guard = helpers.network_blocked()
            with guard:
                with self.assertRaises(helpers.NetworkBlocked):
                    socket.getaddrinfo("api.github.com", 443)
            self.assertIs(socket.getaddrinfo, marker)
        finally:
            socket.getaddrinfo = saved
        self.assertIs(socket.getaddrinfo, helpers._blocked)
        self.assertIs(socket.socket.connect, helpers._blocked)


class F_ReviewFixes(CollectorTestCase):
    def test_deeply_nested_run_is_a_schema_error_not_a_crash(self):
        from collector import schema

        deep = []
        for _ in range(5000):
            deep = [deep]
        with self.assertRaises(schema.SchemaError):
            schema.validate_run({"run_at": deep, "roots": [], "searches": [], "repositories": []})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "deep.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"run_at": ' + "[" * 600 + "]" * 600 + "}")
            with self.assertRaises(schema.SchemaError):
                schema.load_run_file(path)

    def test_429_with_requests_left_is_a_secondary_limit_and_stops_the_run(self):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = {"status": 429, "headers": {"x-ratelimit-resource": "search", "x-ratelimit-remaining": "25"}, "body": {}}
        run, report, client, transport = collect_with(routes, two_query_config())
        self.assertEqual(report["stopped"], "rate_limited")
        self.assertIs(client.secondary_limited, True)
        self.assertEqual(run["roots"], [])
        self.assertEqual(len(transport.calls), 1)

    def test_429_with_the_search_bucket_empty_stops_only_the_searches(self):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = dict(SEARCH_LIMITED, status=429)
        run, report, client, _ = collect_with(routes, two_query_config())
        self.assertEqual(report["stopped"], "search_rate_limited")
        self.assertIs(client.secondary_limited, False)
        self.assertEqual([r["fetch_state"] for r in run["repositories"]], ["ok", "ok"])
        self.assertEqual([s["resume_cursor"] for s in run["searches"]], ["page=1", "page=1"])

    def test_a_search_limit_outside_the_searches_ends_the_run_as_rate_limited(self):
        routes = routes_for_network()
        routes["GET /repositories/90"] = SEARCH_LIMITED
        run, report, _, _ = collect_with(routes)
        self.assertEqual(report["stopped"], "rate_limited")
        self.assertEqual(run["collection_report"]["stopped"], "rate_limited")
        self.assertEqual([(r["repository_id"], r["fetch_state"]) for r in run["repositories"]], [(90, "rate_limited")])
        self.assertEqual(report["not_fetched_repository_ids"], [100])

    def test_a_search_header_on_a_later_core_page_still_ends_the_run(self):
        from collector import github

        routes = routes_for_network()
        link = '<https://api.github.com/repositories/90/forks?per_page=100&page=2>; rel="next"'
        routes[FRAME_FORKS_P1] = ok([api_repo(100, SKY, fork=True, parent=90, source=90)], {"link": link})
        routes["GET /repositories/90/forks?per_page=100&page=2"] = SEARCH_LIMITED
        run, report, client, transport = collect_with(routes)
        self.assertEqual(report["stopped"], "rate_limited")
        self.assertEqual(run["collection_report"]["stopped"], "rate_limited")
        self.assertEqual(run["repositories"], [])
        self.assertEqual([c["url"].split("api.github.com", 1)[1].split("?", 1)[0] for c in transport.calls][1:], ["/repos/frame-org/ps3recomp-example/forks", "/repositories/90/forks"])
        self.assertIs(client.rate_limited, True)
        # The label follows the path: core for a non-search path, the header's bucket for a search path.
        client, _ = make_client({})
        with self.assertRaises(github.RateLimited) as ctx:
            client._note_rate_limit(github.Response(403, {"x-ratelimit-remaining": "0", "x-ratelimit-resource": "search"}, b"{}"), "/repositories/90/commits")
        self.assertEqual(ctx.exception.resource, "core")
        client, _ = make_client({})
        with self.assertRaises(github.RateLimited) as ctx:
            client._note_rate_limit(github.Response(403, {"x-ratelimit-remaining": "0", "x-ratelimit-resource": "search"}, b"{}"), "/search/repositories")
        self.assertEqual(ctx.exception.resource, "search")

    def test_a_search_cut_by_its_bucket_before_any_item_gets_a_cursor(self):
        from collector import collect, github

        class Stopping(github.Client):
            def paginate(self, path, params, cap, expect="array", items_key=None, repository_id=None):
                if path.startswith("/search/"):
                    return github.Pages(None, False, None, None, "search_rate_limited")
                return github.Client.paginate(self, path, params, cap, expect, items_key, repository_id)

        client = Stopping(github.RecordedTransport(routes_for_network()), max_requests=50)
        run, report = collect.collect(client, config_for_network(), RUN_AT)
        self.assertEqual(run["searches"][0]["resume_cursor"], "page=1")
        self.assertEqual(report["errors"], [])

    def test_a_forks_walk_that_fails_on_page_one_is_noted(self):
        routes = routes_for_network()
        routes[FRAME_FORKS_P1] = ok({"not": "a list"})
        run, report, _, _ = collect_with(routes)
        self.assertIs(run["roots"][0]["forks_scan_complete"], False)
        self.assertIn({"path": "/repos/frame-org/ps3recomp-example/forks", "reason": "response"}, report["errors"])
        self.assertIs(run["collection_report"]["complete"], False)

    def test_searches_after_the_bucket_ran_out_are_not_asked_for(self):
        from collector import collect, github

        asked = []

        class Counting(github.Client):
            def paginate(self, path, params, cap, expect="array", items_key=None, repository_id=None):
                if path.startswith("/search/"):
                    asked.append(params["q"])
                return github.Client.paginate(self, path, params, cap, expect, items_key, repository_id)

        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = SEARCH_LIMITED
        client = Counting(github.RecordedTransport(routes), max_requests=50)
        run, _ = collect.collect(client, two_query_config(), RUN_AT)
        self.assertEqual(asked, ["ps3recomp in:name"])
        self.assertEqual(run["searches"][1]["resume_cursor"], "page=1")

    def test_a_short_repos_path_has_no_id_spelling(self):
        from collector import github

        self.assertIs(github.equivalent_path("/repos/o", "/repositories/5", 5), False)
        self.assertIs(github.equivalent_path("/repos/o/r", "/repositories/5", 5), True)

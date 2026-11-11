"""Plain messages for the person running the collector: a rejected token, a budget stop, a dry run, a missing state.

Every token here is a fictional string. No network: tests/helpers.py blocks
sockets, and the live CLI runs use a RecordedTransport in place of the live one.
"""

import json
import os
import tempfile
import unittest.mock

from helpers import CollectorTestCase
from test_github import RUN_AT, _run_cli, config_for_network, make_client, ok, routes_for_network, search_url
from test_github_hardening import collect_with

TOKEN = "ghp_FAKErejectedToken0123456789"
UNAUTHORIZED = {"status": 401, "headers": {"x-ratelimit-remaining": "59"}, "body": {"message": "Bad credentials"}}
TOKEN_LINE = "token rejected by GitHub (401); check or regenerate the token"


class RejectedTokenStopsTheRun(CollectorTestCase):
    def test_client_raises_on_401_and_refuses_every_later_request(self):
        from collector import github

        routes = {"GET /repositories/90": UNAUTHORIZED, "GET /repositories/100": ok({})}
        client, transport = make_client(routes, token=github.Secret(TOKEN))
        with self.assertRaises(github.Unauthorized) as ctx:
            client.get_json("/repositories/90")
        self.assertEqual(github._stop_reason(ctx.exception), "unauthorized")
        with self.assertRaises(github.Unauthorized):
            client.get_json("/repositories/100")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_401_on_the_first_search_ends_the_run(self):
        from collector import github

        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = UNAUTHORIZED
        run, report, _, transport = collect_with(routes, token=github.Secret(TOKEN))
        self.assertEqual(report["stopped"], "unauthorized")
        self.assertEqual(run["collection_report"]["stopped"], "unauthorized")
        self.assertIs(run["collection_report"]["complete"], False)
        self.assertEqual(run["roots"], [])
        self.assertEqual(len(transport.calls), 1)

    def test_401_on_a_later_page_or_fetch_ends_the_run(self):
        routes = routes_for_network()
        routes["GET /repositories/100"] = UNAUTHORIZED
        run, report, client, transport = collect_with(routes)
        self.assertEqual(report["stopped"], "unauthorized")
        self.assertIs(client.unauthorized, True)
        self.assertEqual([(r["repository_id"], r["fetch_state"]) for r in run["repositories"]], [(90, "ok"), (100, "failed")])
        self.assertIs(run["collection_report"]["complete"], False)
        self.assertTrue(transport.calls[-1]["url"].endswith("/repositories/100"))

    def test_the_stop_reason_is_known_to_the_schema_and_the_export(self):
        from collector import schema
        from helpers import run_runs

        self.assertIn("unauthorized", schema.STOP_REASONS)
        run, _, _, _ = collect_with(dict(routes_for_network(), **{"GET /repositories/100": UNAUTHORIZED}))
        state, _ = run_runs([run])
        self.assertEqual(state["collection"]["last_run"]["stopped"], "unauthorized")


class CliMessages(CollectorTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(config_for_network(), fh)
        self.fake_repo = os.path.join(self.tmp.name, "repo")
        self.data_root = os.path.join(self.fake_repo, "data")
        os.makedirs(self.data_root)

    def tearDown(self):
        self.tmp.cleanup()

    def live(self, routes, extra, env=None):
        from collector import cli, github

        with unittest.mock.patch.object(cli, "REPO_ROOT", self.fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", self.data_root):
            with unittest.mock.patch.object(github, "LiveTransport", lambda *a, **k: github.RecordedTransport(routes)):
                return _run_cli(["collect", "--config", self.config_path, "--live", "--run-at", RUN_AT, "--out", "data/run.json"] + extra, env=env or {"GITHUB_TOKEN": TOKEN})

    def test_a_rejected_token_prints_one_plain_line_and_exits_partial(self):
        routes = routes_for_network()
        routes[search_url("ps3recomp in:name")] = UNAUTHORIZED
        code, out, err = self.live(routes, ["--max-requests", "20"])
        self.assertEqual(code, 3, err)
        self.assertIn(TOKEN_LINE + "\n", err)
        self.assertEqual(json.loads(out)["stopped"], "unauthorized")
        self.assertNotIn(TOKEN, out + err)
        with open(os.path.join(self.data_root, "run.json"), encoding="utf-8") as fh:
            self.assertNotIn(TOKEN, fh.read())

    def test_a_budget_stop_names_the_budget_and_the_repositories_left(self):
        code, out, err = self.live(routes_for_network(), ["--max-requests", "3"])
        self.assertEqual(code, 3, err)
        summary = json.loads(out)
        self.assertEqual(summary["stopped"], "budget")
        self.assertIn("stopped: request budget of 3 reached; %d repositories not fetched\n" % len(summary["not_fetched_repository_ids"]), err)
        self.assertNotIn(TOKEN_LINE, err)

    def test_a_complete_run_prints_no_stop_line(self):
        code, _, err = self.live(routes_for_network(), ["--max-requests", "20"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("stopped:", err)
        self.assertNotIn("401", err)

    def test_a_dry_run_prints_the_plan_size_and_budget_on_stderr_only(self):
        code, out, err = _run_cli(["collect", "--config", self.config_path])
        self.assertEqual(code, 0, err)
        plan = json.loads(out)["plan"]
        self.assertIn("%d requests planned; budget 50\n" % len(plan), err)
        code, out, err = _run_cli(["collect", "--config", self.config_path, "--max-requests", "7"])
        self.assertEqual(code, 0, err)
        self.assertIn("%d requests planned; budget 7\n" % len(json.loads(out)["plan"]), err)

    def test_a_missing_previous_state_names_the_flag_and_how_to_make_one(self):
        from collector import cli

        with unittest.mock.patch.object(cli, "REPO_ROOT", self.fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", self.data_root):
            code, out, err = _run_cli(["collect", "--config", self.config_path, "--previous-state", "data/absent.json"])
            self.assertEqual(code, 2)
            self.assertIn("refused: --previous-state: path does not exist", err)
            self.assertIn("run export --state-out first", err)
            self.assertEqual(out, "")
            code, _, err = _run_cli(["export", "--runs", "data/absent.json", "--out-dir", "data/site"])
            self.assertEqual(code, 2)
            self.assertIn("refused: --runs: path does not exist", err)
            self.assertNotIn("--state-out first", err)


if __name__ == "__main__":
    unittest.main()

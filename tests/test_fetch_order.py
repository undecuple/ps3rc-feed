"""Fetch order: which candidates a run fetches when max_repositories cuts the list.

Roots and `include` entries first; then candidates never attempted before,
newest id first; then known candidates, oldest last attempt first. The
"known and when" comes from the state the export keeps (collect
--previous-state). Every response is recorded and fictional; nothing opens a
socket.
"""

import io
import json
import os
import random
import tempfile
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from helpers import CollectorTestCase
from test_github import SENTINEL, api_commit, api_repo, make_client, ok, search_url

RUN_1 = "2026-11-05T00:00:00Z"
RUN_2 = "2026-11-06T00:00:00Z"
RUN_3 = "2026-11-07T00:00:00Z"
ROOT = {"repository_id": 20, "full_name": "good-org/root"}
INCLUDE = {"repository_id": 30, "full_name": "good-org/extra"}
LISTED = list(range(21, 28))
# Roots and include take two of the six places; the seven listed ids share the other four.
CONFIG = {"queries": ["port in:name"], "roots": [ROOT], "include": [INCLUDE], "max_repositories": 6}


def fetch_routes(rid, full_name, run_at):
    from collector import collect

    since = collect._since(run_at, collect.DEFAULT_COMMIT_WINDOW_DAYS).replace(":", "%3A")
    return {
        "GET /repositories/%d" % rid: ok(api_repo(rid, full_name, description="Fictional %d" % rid, stars=rid)),
        "GET /repos/%s/commits?page=1&per_page=100&sha=main&since=%s" % (full_name, since): ok([api_commit(rid)]),
        "GET /repos/%s/releases?per_page=30" % full_name: ok([]),
        "GET /repos/%s/readme" % full_name: ok("# Fictional\n"),
    }


def routes(run_at):
    items = [api_repo(rid, "port-org/p%d" % rid) for rid in LISTED]
    table = {search_url("port in:name"): ok({"total_count": len(items), "incomplete_results": False, "items": items})}
    table["GET /repos/good-org/root/forks?page=1&per_page=100"] = ok([])
    for rid in LISTED:
        table.update(fetch_routes(rid, "port-org/p%d" % rid, run_at))
    table.update(fetch_routes(20, "good-org/root", run_at))
    table.update(fetch_routes(30, "good-org/extra", run_at))
    return table


def fetched(client):
    """Repository ids in the order their metadata was requested."""
    return [int(e["path"].split("/")[2]) for e in client.log if e["path"].startswith("/repositories/")]


def collect_run(run_at, last_attempted=None, config=None):
    from collector import collect

    client, _ = make_client(routes(run_at), max_requests=100)
    run, report = collect.collect(client, dict(config or CONFIG), run_at, last_attempted)
    return run, report, client


class FetchOrderRules(CollectorTestCase):
    def test_roots_and_include_first_then_new_newest_first_then_oldest_attempt(self):
        from collector import collect

        known = {5: "2026-11-02T00:00:00Z", 6: "2026-11-01T00:00:00Z", 4: "2026-11-02T00:00:00Z", 1: "2026-11-01T00:00:00Z"}
        order = collect.fetch_order({1, 2, 3, 4, 5, 6, 7, 8, 50, 40}, {50, 40}, known)
        # 40, 50 configured; 8, 7, 3, 2 never attempted (newest first); then 1 and 6 (Nov 1, by id), 4 and 5 (Nov 2, by id).
        self.assertEqual(order, [40, 50, 8, 7, 3, 2, 1, 6, 4, 5])

    def test_without_a_previous_state_the_order_is_newest_id_first(self):
        from collector import collect

        self.assertEqual(collect.fetch_order({3, 9, 1, 7}, {7}, None), [7, 9, 3, 1])
        self.assertEqual(collect.fetch_order({3, 9, 1, 7}, set(), {}), [9, 7, 3, 1])

    def test_a_known_root_or_include_keeps_its_place(self):
        from collector import collect

        # Recently attempted configured ids still come first; ids no longer candidates are ignored.
        known = {7: "2026-11-09T00:00:00Z", 3: "2026-11-01T00:00:00Z", 99: "2026-01-01T00:00:00Z"}
        self.assertEqual(collect.fetch_order([3, 7, 9], {7}, known), [7, 9, 3])

    def test_the_order_does_not_depend_on_input_order(self):
        from collector import collect

        rng = random.Random(7)
        ids = list(range(1, 60))
        known = {rid: "2026-11-%02dT00:00:00Z" % rng.randint(1, 4) for rid in ids if rid % 3}
        expected = collect.fetch_order(set(ids), {10, 20}, known)
        for _ in range(20):
            rng.shuffle(ids)
            shuffled = dict(sorted(known.items(), key=lambda kv: rng.random()))
            self.assertEqual(collect.fetch_order(list(ids), {20, 10}, shuffled), expected)

    def test_last_attempted_comes_from_every_stored_observation(self):
        from collector import collect, events, store

        state = store.empty_state()
        run, _, _ = collect_run(RUN_1)
        events.apply_run(state, run)
        self.assertEqual(collect.last_attempted_from_state(state), {rid: RUN_1 for rid in (20, 24, 25, 26, 27, 30)})
        self.assertEqual(collect.last_attempted_from_state(store.empty_state()), {})


class FetchOrderInACollection(CollectorTestCase):
    def test_first_run_fetches_configured_then_newest_and_reports_the_rest(self):
        run, report, client = collect_run(RUN_1)
        self.assertEqual(fetched(client), [20, 30, 27, 26, 25, 24])
        self.assertEqual(report["not_fetched_repository_ids"], [21, 22, 23])
        self.assertTrue(report["candidates_truncated"])
        cr = run["collection_report"]
        # A cut by max_repositories is partial exactly as before: not complete, no stop reason, the count recorded.
        self.assertEqual((cr["complete"], cr["stopped"], cr["not_fetched"], cr["requests_made"]), (False, None, 3, 2 + 4 * 6))
        self.assertEqual([r["repository_id"] for r in run["repositories"]], [20, 24, 25, 26, 27, 30])
        # Every listed id stays in the search listing, fetched or not.
        self.assertEqual(run["searches"][0]["repository_ids"], LISTED)

    def test_two_runs_with_the_state_cover_every_candidate_and_then_rotate(self):
        from collector import collect, events, store

        state = store.empty_state()
        seen = set()
        orders = []
        for run_at in (RUN_1, RUN_2, RUN_3):
            known = collect.last_attempted_from_state(state) if state["last_run_at"] else None
            run, report, client = collect_run(run_at, known)
            orders.append(fetched(client))
            seen.update(r["repository_id"] for r in run["repositories"])
            events.apply_run(state, run)
            if run_at == RUN_2:
                self.assertEqual(seen, set(LISTED) | {20, 30})
        # Run 2: the three never attempted first (newest first), then the oldest attempt (run 1, ties by id).
        self.assertEqual(orders[1], [20, 30, 23, 22, 21, 24])
        # Run 3: everything is known; run 1's leftovers (25, 26, 27) first, then the oldest of run 2 by id.
        self.assertEqual(orders[2], [20, 30, 25, 26, 27, 21])

    def test_without_the_state_the_same_ids_are_left_out_every_run(self):
        for run_at in (RUN_1, RUN_2):
            _, report, client = collect_run(run_at)
            self.assertEqual(fetched(client), [20, 30, 27, 26, 25, 24])
            self.assertEqual(report["not_fetched_repository_ids"], [21, 22, 23])

    def test_the_same_inputs_give_the_same_requests_and_run(self):
        from collector import collect, events, store

        state = store.empty_state()
        events.apply_run(state, collect_run(RUN_1)[0])
        known = collect.last_attempted_from_state(state)
        first = collect_run(RUN_2, dict(known))
        second = collect_run(RUN_2, dict(reversed(list(known.items()))))
        self.assertEqual(first[2].log, second[2].log)
        self.assertEqual(json.dumps(first[0], sort_keys=True), json.dumps(second[0], sort_keys=True))

    def test_a_budget_stop_leaves_the_end_of_the_order_unfetched(self):
        from collector import collect

        client, _ = make_client(routes(RUN_1), max_requests=2 + 4 * 3)
        run, report = collect.collect(client, dict(CONFIG), RUN_1)
        self.assertEqual(fetched(client), [20, 30, 27])
        self.assertEqual(report["stopped"], "budget")
        self.assertEqual(report["not_fetched_repository_ids"], [21, 22, 23, 24, 25, 26])
        self.assertEqual(run["collection_report"]["not_fetched"], 6)


class SkippedRepositoriesKeepTheirData(CollectorTestCase):
    def test_a_repository_left_out_keeps_its_values_and_is_not_failing(self):
        from collector import catalog, collect, events, export, store

        state = store.empty_state()
        run_1, _, _ = collect_run(RUN_1)
        events.apply_run(state, run_1)
        before = {rid: json.dumps(state["repositories"][str(rid)], sort_keys=True) for rid in (25, 26, 27)}
        run_2, report, _ = collect_run(RUN_2, collect.last_attempted_from_state(state))
        self.assertEqual(report["not_fetched_repository_ids"], [25, 26, 27])
        events.apply_run(state, run_2)
        for rid in (25, 26, 27):
            # Not in the run: the stored observation is untouched, not marked failing or stale.
            self.assertEqual(json.dumps(state["repositories"][str(rid)], sort_keys=True), before[rid])
        status = catalog.collection_status(state)
        self.assertEqual(status["not_refreshed"], [25, 26, 27])
        self.assertEqual(status["failing"], [])
        self.assertEqual(status["stale"], [])
        feed = export.build_feed(state)
        repos = {r["id"]: r for r in feed["repos"]}
        self.assertEqual(sorted(repos), ["r%d" % rid for rid in sorted(set(LISTED) | {20, 30})])
        for rid in (25, 26, 27):
            record = repos["r%d" % rid]
            self.assertEqual((record["description"], record["stars"], record["collection_failing"]), ("Fictional %d" % rid, rid, False))
        self.assertEqual(feed["collection"], {"complete": False, "stopped": None, "not_fetched": 3, "error_count": 0, "last_run_at": RUN_2})


def _run_cli(argv, fake_repo, env=None):
    from collector import cli

    out, err = io.StringIO(), io.StringIO()
    with unittest.mock.patch.object(cli, "REPO_ROOT", fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", os.path.join(fake_repo, "data")), unittest.mock.patch.object(
        cli, "FIXTURE_ROOT", os.path.join(fake_repo, "tests", "fixtures")
    ), unittest.mock.patch.dict(os.environ, env or {}, clear=False):
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class PreviousStateFlag(CollectorTestCase):
    def setUp(self):
        from collector import events, store

        CollectorTestCase.setUp(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.repo, "data"))
        os.makedirs(os.path.join(self.repo, "tests", "fixtures"))
        self.config = os.path.join(self.repo, "config.json")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(CONFIG, fh)
        with open(os.path.join(self.repo, "tests", "fixtures", "rec.json"), "w", encoding="utf-8") as fh:
            json.dump({"responses": routes(RUN_2)}, fh)
        state = store.empty_state()
        events.apply_run(state, collect_run(RUN_1)[0])
        store.save_state(state, os.path.join(self.repo, "data", "state.json"))

    def tearDown(self):
        self.tmp.cleanup()
        CollectorTestCase.tearDown(self)

    def _plan(self, *extra):
        code, out, err = _run_cli(["collect", "--config", self.config, "--recorded", "tests/fixtures/rec.json", "--run-at", RUN_2] + list(extra), self.repo)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_recorded_plan_follows_the_previous_state(self):
        payload = self._plan("--previous-state", "data/state.json")
        order = [int(e["path"].split("/")[2]) for e in payload["plan"] if e["path"].startswith("/repositories/")]
        self.assertEqual(order, [20, 30, 23, 22, 21, 24])
        self.assertEqual(payload["previous_state"], {"known_repositories": 6, "last_run_at": RUN_1})
        payload = self._plan()
        order = [int(e["path"].split("/")[2]) for e in payload["plan"] if e["path"].startswith("/repositories/")]
        self.assertEqual(order, [20, 30, 27, 26, 25, 24])
        self.assertEqual(set(payload), {"plan"})

    def test_request_plan_without_the_network_reports_the_state_it_read(self):
        code, out, err = _run_cli(["collect", "--config", self.config], self.repo)
        self.assertEqual(code, 0, err)
        plain = json.loads(out)
        code, out, err = _run_cli(["collect", "--config", self.config, "--previous-state", "data/state.json"], self.repo)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["plan"], plain["plan"])
        self.assertEqual(payload["previous_state"], {"known_repositories": 6, "last_run_at": RUN_1})

    def test_an_unknown_or_unusable_previous_state_is_refused(self):
        outside = os.path.join(self.repo, "state.json")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("{}")
        with open(os.path.join(self.repo, "data", "state.txt"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        os.symlink(os.path.join(self.repo, "data", "state.json"), os.path.join(self.repo, "data", "link.json"))
        for value in ("data/missing.json", outside, "data/state.txt", "data/link.json", "https://example.invalid/state.json"):
            code, out, err = _run_cli(["collect", "--config", self.config, "--previous-state", value], self.repo)
            self.assertEqual((code, out), (2, ""), value)
            self.assertIn("refused", err, value)
        with open(os.path.join(self.repo, "data", "bad.json"), "w", encoding="utf-8") as fh:
            json.dump({"run_at": RUN_1, "roots": [], "searches": [], "repositories": []}, fh)
        with open(os.path.join(self.repo, "data", "junk.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        for name in ("bad.json", "junk.json"):
            code, out, err = _run_cli(["collect", "--config", self.config, "--previous-state", "data/" + name], self.repo)
            self.assertEqual((code, out), (1, ""), name)
            self.assertIn("--previous-state data/%s is not a usable state" % name, err)

    def test_live_refuses_an_out_that_would_replace_the_previous_state(self):
        from collector import github

        state_path = os.path.join(self.repo, "data", "state.json")
        with open(state_path, "rb") as fh:
            before = fh.read()
        with unittest.mock.patch.object(github, "LiveTransport", side_effect=AssertionError("live transport constructed")):
            code, out, err = _run_cli(
                ["collect", "--config", self.config, "--live", "--max-requests", "5", "--out", "data/state.json", "--overwrite", "--previous-state", "data/state.json"],
                self.repo,
                env={"GITHUB_TOKEN": SENTINEL},
            )
        self.assertEqual(code, 2, err)
        self.assertIn("--previous-state", err)
        self.assertNotIn(SENTINEL, out + err)
        with open(state_path, "rb") as fh:
            self.assertEqual(fh.read(), before)

"""The reviewed sources config (config/sources.json) and the collector rules it relies on.

Query filters (`require_any`), the `deny` and `include` lists, their strict
validation, the committed config's request plan, and its real filter terms
run against invented repository names modelled on a real search. Every
response is recorded and fictional; nothing opens a socket.
"""

import json
import os
import tempfile

from helpers import CollectorTestCase
from test_github import RUN_AT, api_commit, api_repo, make_client, ok, search_url

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(REPO_ROOT, "config", "sources.json")
SINCE = "since=2026-10-06T00%3A00%3A00Z"

GAME_TERMS = ["shadow fall", "Killzone 2", "emulator"]


def fetch_routes(rid, full_name, description="Fictional"):
    """The four per-repository requests of a candidate, all answered."""
    return {
        "GET /repositories/%d" % rid: ok(api_repo(rid, full_name, description=description)),
        "GET /repos/%s/commits?page=1&per_page=100&sha=main&%s" % (full_name, SINCE): ok([api_commit(rid)]),
        "GET /repos/%s/releases?per_page=30" % full_name: ok([]),
        "GET /repos/%s/readme" % full_name: ok("# Fictional\n"),
    }


def search(query, items):
    return {search_url(query): ok({"total_count": len(items), "incomplete_results": False, "items": items})}


def run_collect(config, routes, **client_kw):
    from collector import collect

    client_kw.setdefault("max_requests", 100)
    client, transport = make_client(routes, **client_kw)
    run, report = collect.collect(client, config, RUN_AT)
    return run, report, [entry["path"] for entry in client.log]


def write_config(tmp, config):
    path = os.path.join(tmp, "c.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    return path


class QueryFilter(CollectorTestCase):
    def _scenario(self):
        items = [
            api_repo(11, "fan-dev/stats-app", description="Stats for Shadow Fall matches"),
            api_repo(12, "trader-x/zone-indicator", description="Forex session zones"),
            api_repo(13, "archivist/core-EMULATOR", description=None),
            api_repo(14, "student-y/homework", description="class project"),
        ]
        routes = search("zone in:name", items)
        routes.update(fetch_routes(11, "fan-dev/stats-app"))
        routes.update(fetch_routes(13, "archivist/core-EMULATOR"))
        config = {"queries": [{"q": "zone in:name", "require_any": GAME_TERMS}], "roots": []}
        return config, routes

    def test_filter_keeps_matching_items_and_never_requests_the_others(self):
        config, routes = self._scenario()
        run, report, paths = run_collect(config, routes)
        entry = run["searches"][0]
        self.assertEqual(entry["query"], "zone in:name")
        self.assertEqual(entry["repository_ids"], [11, 13])
        self.assertEqual(entry["filtered_out"], 2)
        self.assertIs(entry["complete"], True)
        self.assertEqual([r["repository_id"] for r in run["repositories"]], [11, 13])
        self.assertNotIn("/repositories/12", paths)
        self.assertNotIn("/repositories/14", paths)
        self.assertEqual(len(paths), 1 + 2 * 4)
        cr = run["collection_report"]
        self.assertEqual((cr["filtered_out"], cr["denied"], cr["not_fetched"], cr["dropped_items"]), (2, 0, 0, 0))
        self.assertIs(cr["complete"], True)

    def test_a_plain_query_is_not_filtered_and_has_no_filtered_out(self):
        items = [api_repo(12, "trader-x/zone-indicator", description="Forex session zones")]
        routes = search("zone in:name", items)
        routes.update(fetch_routes(12, "trader-x/zone-indicator"))
        run, _, paths = run_collect({"queries": ["zone in:name"], "roots": []}, routes)
        self.assertEqual(run["searches"][0]["repository_ids"], [12])
        self.assertNotIn("filtered_out", run["searches"][0])
        self.assertEqual(run["collection_report"]["filtered_out"], 0)
        self.assertIn("/repositories/12", paths)

    def test_an_item_left_out_by_one_query_stays_a_candidate_through_another(self):
        item = api_repo(12, "trader-x/zone-indicator", description="Forex session zones")
        routes = search("zone in:name", [item])
        routes.update(search("indicator in:name", [item]))
        routes.update(fetch_routes(12, "trader-x/zone-indicator"))
        config = {"queries": [{"q": "zone in:name", "require_any": GAME_TERMS}, "indicator in:name"], "roots": []}
        run, _, paths = run_collect(config, routes)
        self.assertEqual(run["searches"][0]["repository_ids"], [])
        self.assertEqual(run["searches"][0]["filtered_out"], 1)
        self.assertEqual(run["searches"][1]["repository_ids"], [12])
        self.assertIn("/repositories/12", paths)

    def test_a_cut_short_search_is_still_incomplete_with_a_filter(self):
        items = [api_repo(11, "fan-dev/stats-app", description="Shadow Fall"), api_repo(12, "trader-x/zone", description="Forex")]
        routes = {search_url("zone in:name"): ok({"total_count": 3, "incomplete_results": False, "items": items})}
        routes.update(fetch_routes(11, "fan-dev/stats-app"))
        run, _, _ = run_collect({"queries": [{"q": "zone in:name", "require_any": GAME_TERMS}], "roots": []}, routes)
        self.assertIs(run["searches"][0]["complete"], False)
        self.assertEqual(run["searches"][0]["filtered_out"], 1)

    def test_matching_is_case_insensitive(self):
        from collector import collect

        self.assertTrue(collect.is_relevant("a/b", "SHADOW FALL fan site", ["shadow fall"]))
        self.assertTrue(collect.is_relevant("a/b", "shadow fall fan site", ["Shadow FALL"]))
        self.assertTrue(collect.is_relevant("Owner/KILLZONE-2-Tools", None, ["killzone 2", "Killzone-2"]))
        self.assertFalse(collect.is_relevant("a/b", "Shadowfall", ["shadow fall"]))

    def test_term_in_the_name_or_the_description_but_not_across_them(self):
        from collector import collect

        self.assertTrue(collect.is_relevant("someone/ps-emulator-notes", "", ["emulator"]))
        self.assertTrue(collect.is_relevant("someone/notes", "Notes on an emulator", ["emulator"]))
        self.assertFalse(collect.is_relevant("someone/notes", "Notes", ["emulator"]))
        # "notes" + "emu" must not join into a match across name and description.
        self.assertFalse(collect.is_relevant("someone/emu", "lator", ["emulator"]))
        self.assertFalse(collect.is_relevant("someone/x", {"not": "a string"}, ["string"]))
        self.assertFalse(collect.is_relevant("someone/x", None, ["x y"]))

    def test_the_description_is_matched_within_the_run_format_bound(self):
        from collector import collect, schema

        long_text = "x" * schema.MAX_DESCRIPTION + " emulator"
        self.assertFalse(collect.is_relevant("a/b", long_text, ["emulator"]))
        self.assertTrue(collect.is_relevant("a/b", long_text[: schema.MAX_DESCRIPTION - 9] + "emulator", ["emulator"]))


class DenyAndInclude(CollectorTestCase):
    def _routes(self):
        items = [
            api_repo(21, "good-org/port-a"),
            api_repo(22, "Spam-Owner/clone-1"),
            api_repo(23, "other-org/Unwanted"),
            api_repo(24, "spam-owner/clone-2"),
        ]
        routes = search("port in:name", items)
        routes["GET /repos/good-org/root/forks?page=1&per_page=100"] = ok([api_repo(24, "spam-owner/clone-2"), api_repo(25, "good-org/fork-b")])
        for rid, name in ((20, "good-org/root"), (21, "good-org/port-a"), (25, "good-org/fork-b")):
            routes.update(fetch_routes(rid, name))
        return routes

    def test_deny_by_repository_and_by_owner_case_insensitive(self):
        config = {
            "queries": ["port in:name"],
            "roots": [{"repository_id": 20, "full_name": "good-org/root"}],
            "deny": {"repositories": ["OTHER-ORG/unwanted"], "owners": ["spam-owner"]},
        }
        run, report, paths = run_collect(config, self._routes())
        self.assertEqual([r["repository_id"] for r in run["repositories"]], [20, 21, 25])
        for rid in (22, 23, 24):
            self.assertNotIn("/repositories/%d" % rid, paths)
        self.assertEqual(run["searches"][0]["repository_ids"], [21])
        self.assertEqual(run["roots"][0]["found_repository_ids"], [25])
        cr = run["collection_report"]
        self.assertEqual((cr["denied"], cr["not_fetched"]), (3, 0))
        self.assertIs(cr["complete"], True)

    def test_without_deny_the_same_listing_is_fetched(self):
        config = {"queries": ["port in:name"], "roots": [{"repository_id": 20, "full_name": "good-org/root"}]}
        run, _, paths = run_collect(config, self._routes())
        self.assertIn("/repositories/22", paths)
        self.assertEqual(run["collection_report"]["denied"], 0)

    def test_deny_holds_when_the_run_stops_during_the_searches(self):
        config = {"queries": ["port in:name", "more in:name"], "roots": [], "deny": {"repositories": [], "owners": ["spam-owner"]}, "include": [{"repository_id": 30, "full_name": "good-org/extra"}]}
        run, report, paths = run_collect(config, self._routes(), max_requests=1)
        self.assertEqual(paths, ["/search/repositories"])
        self.assertEqual(run["collection_report"]["denied"], 2)
        self.assertEqual(run["collection_report"]["stopped"], "budget")
        self.assertEqual(run["searches"][0]["repository_ids"], [21, 23])
        # 21 and 23 from the search, 30 from include; never the denied 22 and 24.
        self.assertEqual(report["not_fetched_repository_ids"], [21, 23, 30])

    def test_include_adds_a_candidate_for_the_usual_four_requests(self):
        from collector import collect

        routes = search("port in:name", [api_repo(21, "good-org/port-a")])
        routes.update(fetch_routes(21, "good-org/port-a"))
        routes.update(fetch_routes(31, "lone-dev/extra-port"))
        config = {"queries": ["port in:name"], "roots": [], "include": [{"repository_id": 31, "full_name": "lone-dev/extra-port"}]}
        run, _, paths = run_collect(config, routes)
        self.assertEqual([r["repository_id"] for r in run["repositories"]], [21, 31])
        self.assertEqual(len(paths), 1 + 4 + 4)
        self.assertEqual([p for p in paths if "lone-dev" in p or p.endswith("/31")], ["/repositories/31", "/repos/lone-dev/extra-port/commits", "/repos/lone-dev/extra-port/releases", "/repos/lone-dev/extra-port/readme"])
        self.assertEqual(run["searches"][0]["repository_ids"], [21])
        self.assertIs(run["collection_report"]["complete"], True)
        plan = collect.plan_requests(collect.validate_config(config))
        self.assertEqual([p["path"] for p in plan], ["/search/repositories", "/repositories/31"])


class StrictValidation(CollectorTestCase):
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
        return str(ctx.exception)

    def test_query_objects_are_checked_strictly(self):
        from collector import collect

        q = {"q": "x in:name", "require_any": ["t"]}
        collect.validate_config(dict(self.BASE, queries=["y in:name", dict(q)]))
        for bad in (
            [dict(q, SECRETKEY=1)],
            [{"q": "x in:name"}],
            [{"require_any": ["t"]}],
            [{"q": "x in:name", "require_any": []}],
            [{"q": "x in:name", "require_any": "t"}],
            [{"q": "x in:name", "require_any": [1]}],
            [{"q": "http://x in:name", "require_any": ["t"]}],
            [{"q": "x\nin:name", "require_any": ["t"]}],
            [{"q": "x in:name", "require_any": ["dup", "DUP"]}],
            [{"q": "x in:name", "require_any": ["t" * 65]}],
            [{"q": "x in:name", "require_any": ["t%d" % i for i in range(51)]}],
            ["x in:name", {"q": "X  in:name", "require_any": ["t"]}],
            [7],
            [None],
            [["x in:name"]],
        ):
            message = self.assertRefused({"queries": bad})
            self.assertNotIn("SECRETKEY", message)

    def test_terms_refuse_control_characters_and_odd_spacing(self):
        for term in ("", " lead", "trail ", "two  spaces", "tab\there", "new\nline", "nul\x00", "bell\x07", "nbsp x", "zero​width", "del\x7f", "line sep"):
            self.assertRefused({"queries": [{"q": "x in:name", "require_any": [term]}]}, "require_any[0]")

    def test_good_terms_are_accepted(self):
        from collector import collect

        config = collect.validate_config({"queries": [{"q": "x in:name", "require_any": ["ps vita", "Killzone 2", "videojuego", "t" * 64, "año"]}], "roots": []})
        self.assertEqual(collect.query_terms(config["queries"][0])[1], "Killzone 2")

    def test_deny_is_checked_strictly(self):
        from collector import collect

        collect.validate_config(dict(self.BASE, deny={"repositories": ["a/b"], "owners": ["c"]}))
        for bad in (
            {"repositories": []},
            {"owners": []},
            {"repositories": [], "owners": [], "SECRETKEY": []},
            {"repositories": "a/b", "owners": []},
            {"repositories": ["no-slash"], "owners": []},
            {"repositories": ["a/b c"], "owners": []},
            {"repositories": [], "owners": ["a/b"]},
            {"repositories": [], "owners": ["bad owner"]},
            {"repositories": ["a/b", "A/B"], "owners": []},
            {"repositories": [], "owners": ["x", "X"]},
            {"repositories": ["o/r%d" % i for i in range(501)], "owners": []},
            {"repositories": [], "owners": ["o%d" % i for i in range(201)]},
            [],
        ):
            message = self.assertRefused({"deny": bad})
            self.assertNotIn("SECRETKEY", message)

    def test_deny_may_not_name_a_root_or_an_included_repository(self):
        root = {"repository_id": 5, "full_name": "Root-Org/root"}
        self.assertRefused({"roots": [root], "deny": {"repositories": ["root-org/ROOT"], "owners": []}}, "config.deny")
        self.assertRefused({"roots": [root], "deny": {"repositories": [], "owners": ["ROOT-ORG"]}}, "config.deny")
        self.assertRefused({"include": [root], "deny": {"repositories": [], "owners": ["root-org"]}}, "config.deny")

    def test_include_is_checked_strictly(self):
        from collector import collect

        root = {"repository_id": 5, "full_name": "root-org/root"}
        collect.validate_config(dict(self.BASE, roots=[root], include=[{"repository_id": 6, "full_name": "root-org/other"}]))
        for extra in (
            {"include": [dict(root, SECRETKEY=1)]},
            {"include": [{"repository_id": 5}]},
            {"include": [{"repository_id": 0, "full_name": "a/b"}]},
            {"include": [{"repository_id": 6, "full_name": "a"}]},
            {"include": [{"repository_id": 6, "full_name": "a/b"}, {"repository_id": 6, "full_name": "a/c"}]},
            {"include": [{"repository_id": 6, "full_name": "a/b"}, {"repository_id": 7, "full_name": "A/B"}]},
            {"include": [{"repository_id": 6 + i, "full_name": "a/b%d" % i} for i in range(201)]},
            {"include": "a/b"},
            {"roots": [root], "include": [{"repository_id": 5, "full_name": "x/y"}]},
            {"roots": [root], "include": [{"repository_id": 9, "full_name": "ROOT-ORG/root"}]},
        ):
            message = self.assertRefused(extra)
            self.assertNotIn("SECRETKEY", message)

    def test_defaults_when_the_new_keys_are_absent(self):
        from collector import collect

        config = collect.validate_config({"queries": ["ok in:name"], "roots": []})
        self.assertEqual(config["include"], [])
        self.assertEqual(config["deny"], {"repositories": [], "owners": []})


class ExampleAndRunFormat(CollectorTestCase):
    def test_example_config_is_valid_and_uses_every_new_key_with_placeholders(self):
        from collector import collect

        example = collect.load_config(collect.EXAMPLE_CONFIG_PATH)
        self.assertIsInstance(example["queries"][0], str)
        self.assertTrue(any(isinstance(q, dict) for q in example["queries"]))
        self.assertTrue(example["include"])
        self.assertTrue(example["deny"]["repositories"] and example["deny"]["owners"])
        tokens, ids = collect.example_placeholders()
        for value in [t for q in example["queries"] for t in (collect.query_terms(q) or [])] + example["deny"]["owners"] + example["deny"]["repositories"]:
            for token in collect._tokens(value):
                self.assertIn("example", token)
        self.assertIn(example["include"][0]["repository_id"], ids)

    def test_placeholders_in_the_new_keys_are_found(self):
        from collector import collect

        example = collect.load_config(collect.EXAMPLE_CONFIG_PATH)
        term = [q for q in example["queries"] if isinstance(q, dict)][0]["require_any"][0]
        base = {"queries": ["real in:name"], "roots": []}
        self.assertEqual(collect.config_uses_example_names(collect.validate_config(dict(base))), [])
        cases = (
            dict(base, queries=[{"q": "real in:name", "require_any": ["fine", term.upper()]}]),
            dict(base, include=[{"repository_id": example["include"][0]["repository_id"], "full_name": "real/name"}]),
            dict(base, include=[{"repository_id": 99, "full_name": example["include"][0]["full_name"]}]),
            dict(base, deny={"repositories": [], "owners": [example["deny"]["owners"][0]]}),
            dict(base, deny={"repositories": [example["deny"]["repositories"][0]], "owners": []}),
        )
        for config in cases:
            self.assertTrue(collect.config_uses_example_names(collect.validate_config(config)), config)

    def test_run_format_accepts_the_new_counts_and_older_runs(self):
        from collector import schema

        report = {"complete": True, "stopped": None, "error_count": 0, "dropped_items": 0, "not_fetched": 0, "requests_made": 3}
        base = {"run_at": RUN_AT, "roots": [], "searches": [{"query": "x in:name", "complete": True, "repository_ids": []}], "repositories": []}
        schema.validate_run(dict(base, collection_report=report))
        newer = dict(base, collection_report=dict(report, filtered_out=4, denied=1))
        newer["searches"] = [dict(base["searches"][0], filtered_out=4)]
        out = schema.validate_run(newer)
        self.assertEqual((out["searches"][0]["filtered_out"], out["collection_report"]["denied"]), (4, 1))
        for bad in (dict(newer, collection_report=dict(report, filtered_out=-1)), dict(newer, collection_report=dict(report, denied=True)), dict(base, searches=[dict(base["searches"][0], filtered_out="4")])):
            with self.assertRaises(schema.SchemaError):
                schema.validate_run(bad)


# Invented repositories modelled on a real `killzone` / `helghast` search: the game-related ones the
# filter is for, and the kinds of unrelated hits (trading indicators, profile READMEs, course work,
# game-server scripts, a web engine sharing the word) it must keep out.
KILLZONE_KEEP = [
    ("pocket-dev/kz-tracker-ios", "Stats for Killzone Shadow Fall - iOS App"),
    ("react-fan/killzone_board", "Stat tracking app for Killzone: Shadow Fall"),
    ("scraper-dev/kz3scores", "Killzone 3 Stats - follow your friends"),
    ("archive-dev/Corepak", "an experimental program for killzone 2's .core archive files"),
    ("speedrun-dev/Killzone-Liberation-Timer-PPSSPP-emulator", "In-game time file for speedrunning"),
    ("design-student/killzone", "killzone mercenaries 2 game design"),
    ("web-student/Killzone-fansite", "Proyecto web sobre la saga de videojuegos Killzone"),
    ("tools-studio/AuthoringKit", "Tools used for Guerrilla's Killzone games on PS4"),
    ("lifter-dev/killzone-ps3", None),
]
KILLZONE_DROP = [
    ("chart-dev/ict-killzones-indicator", "TradingView killzones for forex sessions"),
    ("pine-dev/bitcoin-killzones", "Bitcoin Kill Zones PineScript"),
    ("killzone4242/killzone4242", "Config files for my GitHub profile."),
    ("Killzone-Team/Hospital", "Hospital management system written in Qt"),
    ("mc-dev/mc-killzone-plugin", "A Minecraft plugin that creates kill zones"),
    ("survival-dev/dayz-killzone-scripts", None),
    ("server-dev/ChernarusKillZone-Stuff", "Files for the DayZ server Chernarus KillZone"),
    ("git-learner/killzone", "learning how to use github"),
    ("godot-learner/killzone", "My first game with godot and gdscript"),
    ("tabletop-dev/killzone", "Kill Zone tabletop roster data"),
    ("platform-learner/Coding-Week-16", "Checkpoints and Killzones"),
    ("empty-dev/Killzone", None),
]
HELGHAST_KEEP = [
    ("helmet-maker/helghast-helmet", "3D print of a Killzone Helghast helmet"),
    ("helghast-fan/helghast-ps3-tools", "notes"),
]
HELGHAST_DROP = [
    ("Helghast-Example/angular-course", "Intro to the Angular course"),
    ("Helghast-Example/clima-app-node", "Command line weather app in Node"),
    ("Helghast-Example/Helghast-Example", "Config files for my GitHub profile."),
    ("web-engine-dev/helghast", "Helghast is a next-gen game engine for the web."),
    ("first-site/helghasta", "My first website"),
    ("helghast-only/helghast", "helghast helghast HELGHAST"),
]


class CommittedConfig(CollectorTestCase):
    def setUp(self):
        from collector import collect

        CollectorTestCase.setUp(self)
        self.config = collect.load_config(SOURCES)
        self.terms = {collect.query_text(q): collect.query_terms(q) for q in self.config["queries"]}

    def test_it_loads_and_holds_the_reviewed_sources(self):
        from collector import collect

        c = self.config
        self.assertEqual(c["roots"], [{"repository_id": 1178514664, "full_name": "sp00nznet/ps3recomp"}])
        self.assertEqual(c["queries"][:3], ["ps3recomp in:name fork:true", "ps3recomp in:description fork:true", "ps3recomp in:readme fork:true"])
        self.assertEqual(
            list(self.terms),
            ["ps3recomp in:name fork:true", "ps3recomp in:description fork:true", "ps3recomp in:readme fork:true", "killzone in:name", "helghast in:name", "helghast in:description"],
        )
        self.assertEqual([collect.query_kind(q) for q in c["queries"]].count("code"), 0)
        self.assertEqual((c["include"], c["deny"]), ([], {"repositories": [], "owners": []}))
        self.assertEqual((c["max_requests"], c["max_repositories"], c["commit_window_days"], c["readme_bytes"]), (800, 190, 30, 16384))
        self.assertEqual(c["page_caps"], {"search": 3, "code": 1, "forks": 10, "commits": 3})
        self.assertEqual(collect.config_uses_example_names(c), [])

    def test_the_budget_fits_the_built_in_token_with_margin(self):
        from collector import collect

        c = self.config
        kinds = [collect.query_kind(q) for q in c["queries"]]
        searches = kinds.count("repositories") * c["page_caps"]["search"]
        code = kinds.count("code") * c["page_caps"]["code"]
        self.assertLessEqual(searches, 30)  # one minute of the search bucket
        self.assertLessEqual(code, 10)  # one minute of the code search bucket
        # 6 x 3 + 1 x 10 = 28 fixed; + 4 x 190 = 788 <= 800.
        fixed = searches + code + len(c["roots"]) * c["page_caps"]["forks"]
        self.assertEqual((searches, code, fixed), (18, 0, 28))
        self.assertLessEqual(fixed + 4 * c["max_repositories"], c["max_requests"])
        self.assertLessEqual(c["max_requests"], 800)

    def test_the_dry_run_plan_is_exactly_the_expected_requests(self):
        from collector import collect

        def q(text):
            return "page=1&per_page=100&q=" + text.replace(" ", "+").replace(":", "%3A")

        expected = [{"method": "GET", "path": "/search/repositories", "query": q(t)} for t in self.terms]
        expected.append({"method": "GET", "path": "/repos/sp00nznet/ps3recomp/forks", "query": "page=1&per_page=100"})
        expected.append({"method": "GET", "path": "/repositories/1178514664", "query": ""})
        self.assertEqual(collect.plan_requests(self.config), expected)

    def test_the_cli_dry_run_prints_the_plan_without_the_network(self):
        from test_github import _run_cli

        code, out, err = _run_cli(["collect", "--config", SOURCES])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)["plan"]), 8)
        self.assertNotIn("skipped_code_queries", json.loads(out))
        code, out, err = _run_cli(["collect", "--config", SOURCES, "--anonymous"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(len(payload["plan"]), 8)
        self.assertNotIn("/search/code", [p["path"] for p in payload["plan"]])

    def test_only_the_game_queries_are_filtered_and_never_by_their_own_word(self):
        self.assertIsNone(self.terms["ps3recomp in:name fork:true"])
        self.assertIsNone(self.terms["ps3recomp in:description fork:true"])
        # Repository search items carry no README text, so a readme query cannot be filtered.
        self.assertIsNone(self.terms["ps3recomp in:readme fork:true"])
        self.assertNotIn("killzone", [t.casefold() for t in self.terms["killzone in:name"]])
        for query in ("helghast in:name", "helghast in:description"):
            self.assertFalse([t for t in self.terms[query] if "helghast" in t.casefold() or t.casefold() in "helghast"])

    def test_real_terms_against_invented_names_modelled_on_a_real_search(self):
        from collector import collect

        for query, keep, drop in (
            ("killzone in:name", KILLZONE_KEEP, KILLZONE_DROP),
            ("helghast in:name", HELGHAST_KEEP, HELGHAST_DROP),
            ("helghast in:description", HELGHAST_KEEP, HELGHAST_DROP),
        ):
            terms = self.terms[query]
            for name, description in keep:
                self.assertTrue(collect.is_relevant(name, description, terms), (query, name))
            for name, description in drop:
                self.assertFalse(collect.is_relevant(name, description, terms), (query, name))

    def test_the_real_filter_in_a_collection(self):
        items = [api_repo(100 + i, n, description=d) for i, (n, d) in enumerate(KILLZONE_KEEP + KILLZONE_DROP)]
        routes = search("killzone in:name", items)
        for i, (name, description) in enumerate(KILLZONE_KEEP):
            routes.update(fetch_routes(100 + i, name, description))
        config = {"queries": [q for q in self.config["queries"] if isinstance(q, dict) and q["q"] == "killzone in:name"], "roots": []}
        run, _, paths = run_collect(config, routes, max_requests=200)
        self.assertEqual(run["searches"][0]["repository_ids"], list(range(100, 100 + len(KILLZONE_KEEP))))
        self.assertEqual(run["searches"][0]["filtered_out"], len(KILLZONE_DROP))
        self.assertEqual(len(paths), 1 + 4 * len(KILLZONE_KEEP))
        self.assertIs(run["collection_report"]["complete"], True)

"""The reviewed alias list (config/games.json), the stricter README fallback and what they change downstream.

Validation of the file, token-bounded matching and its precedence, the
regression table of junk README headings seen in a real feed, fork
inheritance of alias matches, the new feed fields, and the rule that a
re-label by the list is silent. Repositories are fictional except where a
name or text is quoted from the committed feed to pin a real case.
"""

import copy
import json
import os
import tempfile

from helpers import CollectorTestCase, run_runs
from test_hardening import T, repo, run

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def entry(key="skyfall", title="Skyfall", aliases=("skyfall",), **extra):
    data = {"key": key, "title": title, "aliases": list(aliases)}
    data.update(extra)
    return data


def build(*entries):
    from collector import games

    return games.validate({"games": [copy.deepcopy(e) for e in entries]})


def empty_list():
    return build()


def classify(full_name, desc=None, heading=None, aliases=None):
    from collector import text

    readme = None if heading is None else "# %s\n\nbody\n" % heading
    return text.classify(full_name, desc, readme, aliases)


class CommittedFile(CollectorTestCase):
    def test_it_loads_and_starts_with_the_starter_set(self):
        from collector import games

        loaded = games.load()
        self.assertEqual(
            [e["key"] for e in loaded.entries][:11],
            [
                "god-of-war-ascension", "drakengard-3", "infamous-2", "infamous", "demons-souls",
                "gran-turismo-5-prologue", "saints-row-2", "resistance-fall-of-man", "resistance-2",
                "red-dead-redemption", "guitar-hero-iii",
            ],
        )
        with open(os.path.join(REPO_ROOT, "config", "games.json"), encoding="utf-8") as fh:
            self.assertIn("pull request", json.load(fh)["_comment"])

    def test_real_repository_names_from_the_feed(self):
        for full_name, desc, key, basis, alias in (
            ("arnaldolima1/gow-ascension-pc", None, "god-of-war-ascension", "alias_name", "gow ascension"),
            ("rigaz29/Ascension-R", None, "god-of-war-ascension", "alias_name", "ascension-r"),
            ("MaChInEgUn3/God-of-War-Ascension-Recompiled", None, "god-of-war-ascension", "alias_name", "god of war ascension"),
            ("soyjxck/dod3-recomp", None, "drakengard-3", "alias_name", "dod3"),
            ("erikomis/infamous2-recomp", None, "infamous-2", "alias_name", "infamous 2"),
            ("sp00nznet/GT5P", None, "gran-turismo-5-prologue", "alias_name", "gt5p"),
            ("sp00nznet/saintsrow2", None, "saints-row-2", "alias_name", "saintsrow2"),
            ("sp00nznet/gh3", None, "guitar-hero-iii", "alias_name", "gh3"),
            ("erikomis/resistance-recomp", None, "resistance-fall-of-man", "alias_name", "resistance-recomp"),
            ("erikomis/resistance2-recomp", None, "resistance-2", "alias_name", "resistance 2"),
            ("pedrowallybandeira/RDR_recomp", None, "red-dead-redemption", "alias_name", "rdr-recomp"),
            ("troler11/demons-souls-port-pc", None, "demons-souls", "alias_name", "demons souls"),
            ("some-org/port", "Resistance: Fall of Man (PS3, BCUS98107) statically recompiled", "resistance-fall-of-man", "alias_description", "resistance fall of man"),
        ):
            with self.subTest(full_name=full_name):
                category, game, _ = classify(full_name, desc, "Ascension")
                self.assertEqual((category, game["key"], game["basis"], game["matched_alias"]), ("game", key, basis, alias))
                self.assertEqual(game["confidence"], "strong")
                self.assertIs(game["verified"], False)


class Validation(CollectorTestCase):
    def assertRefused(self, data, fragment):
        from collector import games, schema

        with self.assertRaises(schema.SchemaError) as ctx:
            games.validate(data)
        self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def test_a_good_entry_with_every_key(self):
        loaded = build(entry(name_patterns=["sky-*-pc"], platform="PS3"))
        self.assertEqual(len(loaded.fingerprint), 16)
        self.assertEqual(build(entry(name_patterns=["sky-*-pc"], platform="PS3")).fingerprint, loaded.fingerprint)
        self.assertNotEqual(build(entry()).fingerprint, loaded.fingerprint)

    def test_unknown_keys_are_refused_without_echoing_them(self):
        message = self.assertRefused({"games": [], "evil<script>": 1}, "1 unknown field")
        self.assertNotIn("evil", message)
        message = self.assertRefused({"games": [entry(url="http://x")]}, "games.games[0]: 1 unknown field")
        self.assertNotIn("url", message)

    def test_duplicate_json_keys_are_refused(self):
        from collector import games, schema

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "games.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"games": [{"key": "a-b", "key": "c-d", "title": "Skyfall", "aliases": ["skyfall"]}]}')
            with self.assertRaises(schema.SchemaError) as ctx:
                games.load(path)
            self.assertEqual(str(ctx.exception), games.FILE_ERROR)

    def test_duplicates_across_entries(self):
        self.assertRefused({"games": [entry(), entry(title="Other", aliases=["other"])]}, "duplicate key")
        self.assertRefused({"games": [entry(), entry(key="b", title="skyfall", aliases=["other"])]}, "duplicate title")
        # Same tokens, different spelling.
        self.assertRefused({"games": [entry(aliases=["drakengard 3"]), entry(key="b", title="B", aliases=["Drakengard-3"])]}, "duplicate alias")
        self.assertRefused({"games": [entry(aliases=["dod3", "DOD 3"])]}, "duplicate alias")
        self.assertRefused({"games": [entry(name_patterns=["gt5p"]), entry(key="b", title="B", aliases=["other"], name_patterns=["gt-5-p"])]}, "duplicate pattern")

    def test_hostile_or_malformed_text(self):
        for title in ("@everyone Skyfall", "<b>Skyfall</b>", "Sky‮fall", "Skyfall\x00", "趋势榜", "http://x.example", ""):
            with self.subTest(title=title):
                self.assertRefused({"games": [entry(title=title)]}, "games.games[0].title")
        for alias in ("sky  fall", "skyfall ", " skyfall", "sky<fall>", "http://sky", "sky\nfall", "sky​fall", "a", "x" * 65, "趋势榜"):
            with self.subTest(alias=alias):
                self.assertRefused({"games": [entry(aliases=[alias])]}, "games.games[0].aliases[0]")
        for pattern in ("*", "*-*", "Sky-*", "sky--fall", "sky-", "sky*", "ab", "a-*"):
            with self.subTest(pattern=pattern):
                self.assertRefused({"games": [entry(name_patterns=[pattern])]}, "games.games[0].name_patterns[0]")
        self.assertRefused({"games": [entry(key="Sky_Fall")]}, "games.games[0].key")
        self.assertRefused({"games": [entry(platform="ps3<")]}, "games.games[0].platform")
        self.assertRefused({"games": [entry(aliases=[])]}, "at least one alias")

    def test_sizes_are_bounded(self):
        from collector import games

        self.assertRefused({"games": [entry(aliases=["alias%d" % i for i in range(games.MAX_ALIASES + 1)])]}, "more than")
        self.assertRefused({"games": [entry(key="k%d" % i, title="T%d" % i, aliases=["alias%d" % i]) for i in range(games.MAX_GAMES + 1)]}, "more than")
        self.assertRefused({"games": [entry(aliases=[" ".join(["word"] * (games.MAX_TOKENS + 1))])]}, "tokens")
        self.assertRefused({"games": [entry(aliases=["x" * 65])]}, "longer than")


class Matching(CollectorTestCase):
    def setUp(self):
        super().setUp()
        self.aliases = build(
            entry("god-of-war-ascension", "God of War: Ascension", ["gow ascension", "god of war ascension", "gow"]),
            entry("infamous", "inFAMOUS", ["infamous"]),
            entry("infamous-2", "inFAMOUS 2", ["infamous 2"]),
            entry("resistance-fall-of-man", "Resistance: Fall of Man", ["resistance fall of man"], name_patterns=["resistance-recomp", "res-*-pc"]),
        )

    def match(self, name, desc=None, heading=None):
        hit = self.aliases.match(name, desc, heading)
        return None if hit is None else (hit[0]["key"], hit[1], hit[2])

    def test_token_boundaries(self):
        self.assertEqual(self.match("gow"), ("god-of-war-ascension", "alias_name", "gow"))
        self.assertEqual(self.match("gow-tools"), ("god-of-war-ascension", "alias_name", "gow"))
        for name in ("gowild", "gowild-app", "GoWild", "my_gowe", "agow"):
            with self.subTest(name=name):
                self.assertIsNone(self.match(name))
        self.assertIsNone(self.match("x", "a gowild description"))
        self.assertIsNone(self.match("x", "infamously bad"))

    def test_separators_camel_case_and_digits(self):
        for name in ("gow-ascension-pc", "gow_ascension.pc", "GowAscension", "GoW-Ascension", "GodOfWarAscension"):
            with self.subTest(name=name):
                self.assertEqual(self.match(name)[0], "god-of-war-ascension")
        # inFAMOUS is not split by case, and letters and digits are split.
        self.assertEqual(self.match("inFAMOUS2-recomp"), ("infamous-2", "alias_name", "infamous 2"))
        self.assertEqual(self.match("Infamous_2"), ("infamous-2", "alias_name", "infamous 2"))

    def test_the_longest_alias_wins_inside_a_field(self):
        self.assertEqual(self.match("infamous-recomp"), ("infamous", "alias_name", "infamous"))
        self.assertEqual(self.match("x", "Recompilação estática do inFAMOUS 2 (PS3)"), ("infamous-2", "alias_description", "infamous 2"))
        self.assertEqual(self.match("gow-ascension"), ("god-of-war-ascension", "alias_name", "gow ascension"))

    def test_name_then_description_then_readme_title(self):
        self.assertEqual(self.match("infamous-port", "God of War: Ascension", "Resistance: Fall of Man")[1], "alias_name")
        self.assertEqual(self.match("my-port", "God of War: Ascension", "inFAMOUS 2"), ("god-of-war-ascension", "alias_description", "god of war ascension"))
        self.assertEqual(self.match("my-port", "a port", "inFAMOUS 2"), ("infamous-2", "alias_readme_title", "infamous 2"))
        self.assertIsNone(self.match("my-port", "a port", "Skyfall"))

    def test_name_patterns_match_the_whole_name(self):
        self.assertEqual(self.match("Resistance_Recomp"), ("resistance-fall-of-man", "alias_name", "resistance-recomp"))
        self.assertEqual(self.match("res-mac-pc"), ("resistance-fall-of-man", "alias_name", "res-*-pc"))
        for name in ("resistance-recomp-mac", "my-resistance-recomp", "res-pc", "res-a-b-pc"):
            with self.subTest(name=name):
                self.assertIsNone(self.match(name))

    def test_classify_basis_confidence_and_matched_alias(self):
        _, game, _ = classify("o/gow", aliases=self.aliases)
        self.assertEqual((game["basis"], game["confidence"], game["matched_alias"], game["title"]), ("alias_name", "strong", "gow", "God of War: Ascension"))
        _, game, _ = classify("o/port", "God of War Ascension, natively", aliases=self.aliases)
        self.assertEqual((game["basis"], game["confidence"]), ("alias_description", "strong"))
        _, game, _ = classify("o/port", None, "inFAMOUS 2 — native port", aliases=self.aliases)
        self.assertEqual((game["key"], game["basis"], game["confidence"], game["matched_alias"]), ("infamous-2", "alias_readme_title", "medium", "infamous 2"))
        _, game, _ = classify("o/port", None, "Skyfall", aliases=self.aliases)
        self.assertEqual((game["key"], game["basis"], game["confidence"], game["matched_alias"]), ("skyfall", "readme_title", "low", None))
        # A framework repository stays a framework even when an alias would match.
        category, game, _ = classify("o/ps3recomp-gow", aliases=self.aliases)
        self.assertEqual((category, game["key"]), ("framework", None))


class JunkTitles(CollectorTestCase):
    """README headings that named a "game" in the committed feed, and what they give now (no alias list)."""

    TABLE = (
        ("deepestcyber/vmse2000-detector", "Setup", None),
        ("some-org/port", "O que é", None),
        ("some-org/port", "O que é o projeto?", None),
        ("some-org/port", "What is this?", None),
        ("borger/scoop-emulators", "Emulators Scoop Bucket", None),
        ("jafshare/GithubTrending", "Github 趋势榜追踪", None),
        ("soywiz-archive/cspspemu", "CSharp Psp Emulator", None),
        ("karlforshaw/offblast", "Offblast Launcher", None),
        ("some-org/x", "Lists", None),
        ("some-org/port", "inFAMOUS 2 — native Windows port", "inFAMOUS 2"),
        ("some-org/port", "inFAMOUS 2 – native Windows port", "inFAMOUS 2"),
        ("Awis13/digger-hd-recomp", "Digger HD, recompiled for Mac, iPad and Apple TV", "Digger HD"),
        ("MatRanc/mlb16-recomp", "MLB The Show 16: native PC port (work in progress)", "MLB The Show 16"),
        ("BitJager-dev/segarally-online-arcade-recomp", "SEGA Rally Online Arcade Recompiled (PS3)", "SEGA Rally Online Arcade"),
        ("troler11/demons", "Demon's Souls Port PC", "Demon's Souls"),
        ("frangarcj/pcsx_rearmed", "PCSX ReArcade (PS Vita)", "PCSX ReArcade"),
        ("some-org/x", "God of War: Ascension (PS3) Native PC Port", "God of War: Ascension"),
        ("some-org/x", "Resistance: Fall of Man", "Resistance: Fall of Man"),
        ("some-org/x", "Skyfall, Chapter Two", "Skyfall, Chapter Two"),
        ("some-org/x", "Skyfall - Remastered", "Skyfall - Remastered"),
        # Platform tails and dangling connector words (from a real state).
        ("some-org/x", "Killzone for Windows (native PC port)", "Killzone"),
        ("some-org/x", "God of War II HD for Windows (PS3 static recompilation)", "God of War II HD"),
        ("some-org/x", "Tekken 6 rollback (RPCS3)", "Tekken 6 rollback"),
        ("some-org/x", "Tokisaki Port (Killzone 2 PC Port)", "Tokisaki"),
        ("some-org/x", "Skyfall for PS3 and", "Skyfall"),
        ("some-org/x", "X for Windows", None),
        ("some-org/x", "Setup for Linux", None),
        ("some-org/x", "The Last of Us", "The Last of Us"),
    )

    def test_regression_table(self):
        for full_name, heading, title in self.TABLE:
            with self.subTest(heading=heading):
                category, game, _ = classify(full_name, None, heading, empty_list())
                self.assertEqual(game["title"], title)
                if title is None:
                    self.assertEqual((category, game["basis"], game["confidence"]), ("unclassified", None, "unknown"))
                else:
                    self.assertEqual((category, game["basis"], game["confidence"], game["matched_alias"]), ("game", "readme_title", "low", None))


def gow_root(readme="# Ascension\n"):
    return repo(rid=200, name="maker/gow-ascension-pc", desc="An experiment", readme=readme, commits=[(1, T(1))])


def child(rid, name, readme, desc=None):
    return repo(rid=rid, name=name, desc=desc, readme=readme, commits=[(rid, T(1))], is_fork=True, parent_repository_id=200, network_root_repository_id=200)


class ForkInheritance(CollectorTestCase):
    def game(self, repos, rid):
        state, _ = self.assertDeterministic([run(T(2), repos)])
        return state["repositories"][str(rid)]["game"]

    def test_an_unreviewed_heading_never_overrides_an_inherited_alias(self):
        for heading in ("O que é", "Skyfall Fan Edition", "inFAMOUS 2"):
            with self.subTest(heading=heading):
                game = self.game([gow_root(), child(201, "fan/my-port", "# %s\n" % heading)], 201)
                self.assertEqual(
                    (game["key"], game["basis"], game["inherited_from"], game["matched_alias"], game["confidence"]),
                    ("god-of-war-ascension", "inherited_from_parent", 200, "gow ascension", "strong"),
                )

    def test_own_name_or_description_alias_beats_the_inherited_one(self):
        game = self.game([gow_root(), child(201, "fan/dod3-port", "# Ascension\n")], 201)
        self.assertEqual((game["key"], game["basis"], game["inherited_from"]), ("drakengard-3", "alias_name", None))
        game = self.game([gow_root(), child(201, "fan/my-port", "# Ascension\n", desc="Saints Row 2 instead")], 201)
        self.assertEqual((game["key"], game["basis"]), ("saints-row-2", "alias_description"))

    def test_without_an_ancestor_alias_the_own_heading_stays_low(self):
        root = repo(rid=200, name="maker/skyfall-recomp", desc="x", readme="# Skyfall\n", commits=[(1, T(1))])
        game = self.game([root, child(201, "fan/my-port", "# Firefall\n")], 201)
        self.assertEqual((game["key"], game["basis"], game["confidence"], game["matched_alias"]), ("firefall", "readme_title", "low", None))
        # A fork with nothing of its own still inherits the unreviewed title, labelled.
        game = self.game([root, child(201, "fan/my-port", "")], 201)
        self.assertEqual((game["key"], game["basis"], game["confidence"]), ("skyfall", "inherited_from_parent", "low"))


class ExportFields(CollectorTestCase):
    def test_feed_repositories_carry_the_game_fields(self):
        from collector import export, history

        state, _ = self.assertDeterministic([run(T(2), [gow_root(), child(201, "fan/my-port", "# O que é\n"), repo(rid=300, name="o/skyfall-x", readme="# Skyfall\n", desc=None)])])
        feed = export.build_feed(state)
        records = {r["id"]: r for r in feed["repos"]}
        pick = lambda r: (r["game"], r["game_key"], r["game_basis"], r["game_confidence"], r["matched_alias"], r["game_inherited"])
        self.assertEqual(pick(records["r200"]), ("God of War: Ascension", "god-of-war-ascension", "alias_name", "strong", "gow ascension", False))
        self.assertEqual(pick(records["r201"]), ("God of War: Ascension", "god-of-war-ascension", "inherited_from_parent", "strong", "gow ascension", True))
        self.assertEqual(pick(records["r300"]), ("Skyfall", "skyfall", "readme_title", "low", None, False))
        history.FEED.check(copy.deepcopy(feed), "feed")
        # A feed written before these fields existed still validates for the history.
        for record in feed["repos"]:
            for key in ("game_key", "game_basis", "game_confidence", "matched_alias"):
                del record[key]
        history.FEED.check(feed, "feed")
        # The history snapshot lines keep their own field list.
        self.assertNotIn("game_key", history.SNAPSHOT_FIELDS)


def association_events(per_run):
    return [e for e in per_run if e["type"] == "game_association_changed"]


class SilentRelabel(CollectorTestCase):
    def gow(self, desc="An experiment", name="maker/gow-ascension-pc", readme="# Ascension\n", commit=1):
        return repo(rid=200, name=name, desc=desc, readme=readme, commits=[(commit, T(1))])

    def test_first_run_with_the_list_relabels_without_events(self):
        from collector import events, store

        state = store.empty_state()
        old = events.Config(aliases=empty_list())
        events.apply_run(state, run(T(2), [self.gow()]), old)
        events.apply_run(state, run(T(3), [self.gow(desc="Skyfall port", name="maker/skyfall-port", readme="# Firefall\n")]), old)
        # What an older collector left: no list fingerprint, no matched_alias, the README heading as the game.
        state["games_list"] = None
        for obs in state["repositories"].values():
            del obs["game"]["matched_alias"]
        self.assertEqual(state["repositories"]["200"]["game"]["key"], "firefall")
        emitted = events.apply_run(state, run(T(4), [self.gow()]), events.Config())
        self.assertEqual(association_events(emitted), [])
        self.assertEqual(state["repositories"]["200"]["game"]["key"], "god-of-war-ascension")
        self.assertIsNotNone(state["games_list"])

    def test_moving_onto_an_alias_is_silent_but_a_key_change_between_aliases_is_not(self):
        state, per_run = self.assertDeterministic(
            [
                run(T(2), [self.gow(name="maker/port", desc="A port", readme="# Firefall\n")]),
                # README heading (low) -> alias on the description: silent.
                run(T(3), [self.gow(name="maker/port", desc="God of War Ascension port", readme="# Firefall\n")]),
                # Same key, another alias spelling: nothing to announce.
                run(T(4), [self.gow(name="maker/port", desc="GoW Ascension port", readme="# Firefall\n")]),
                # Alias key -> another alias key: announced.
                run(T(5), [self.gow(name="maker/port", desc="Drakengard 3 port", readme="# Firefall\n")]),
                # Alias -> unreviewed heading: the key changes, announced.
                run(T(6), [self.gow(name="maker/port", desc="A port", readme="# Firefall\n")]),
            ]
        )
        self.assertEqual([len(association_events(r)) for r in per_run], [0, 0, 0, 1, 1])
        change = association_events(per_run[3])[0]
        self.assertEqual(
            (change["details"]["previous_key"], change["details"]["new_key"], change["details"]["basis"], change["details"]["matched_alias"]),
            ("god-of-war-ascension", "drakengard-3", "alias_description", "drakengard 3"),
        )
        self.assertEqual(association_events(per_run[4])[0]["details"]["new_key"], "firefall")


class GamesCommand(CollectorTestCase):
    def test_it_checks_the_committed_file_offline(self):
        import contextlib
        import io

        from collector import cli

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["games"]), cli.EXIT_OK)
        payload = json.loads(out.getvalue())
        with open(os.path.join(REPO_ROOT, "config", "games.json"), encoding="utf-8") as fh:
            self.assertEqual(payload["games"], len(json.load(fh)["games"]))
        self.assertNotIn("changes", payload)

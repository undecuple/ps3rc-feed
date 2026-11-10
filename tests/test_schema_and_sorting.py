"""T26-T27: event schema and strict loading, null-last sorting."""

import copy
import json
import os
import tempfile

from helpers import FIXTURES, CollectorTestCase, load_run


class EventSchemaTests(CollectorTestCase):
    REQUIRED = ("event_id", "type", "repository_id", "observed_at", "source_url", "details")

    def test_t26_every_event_has_the_required_fields(self):
        from collector import schema

        for name in ("new_activity", "failures", "classification_change", "hostile_text"):
            state, _ = self.assertScenarioDeterministic(name)
            for e in state["events"]:
                for key in self.REQUIRED:
                    self.assertIn(key, e)
                self.assertIsInstance(e["repository_id"], int)
                self.assertIsInstance(e["details"], dict)
                schema.validate_event(e)
            # The strict event loader accepts the stored events as a whole.
            schema.load_events(copy.deepcopy(state["events"]))

    def test_t26_unknown_event_field_is_rejected(self):
        from collector import schema

        state, _ = self.assertScenarioDeterministic("new_activity")
        event = copy.deepcopy(state["events"][0])
        event["surprise"] = 1
        with self.assertRaises(schema.SchemaError):
            schema.validate_event(event)
        with self.assertRaises(schema.SchemaError):
            schema.load_events([event])
        missing = copy.deepcopy(state["events"][0])
        del missing["source_url"]
        with self.assertRaises(schema.SchemaError):
            schema.validate_event(missing)
        wrong_type = copy.deepcopy(state["events"][0])
        wrong_type["repository_id"] = "100"
        with self.assertRaises(schema.SchemaError):
            schema.validate_event(wrong_type)

    def test_t26_strict_run_loader_rejects_unknown_and_malformed_fields(self):
        from collector import schema

        run = load_run("new_activity", 1)
        schema.validate_run(copy.deepcopy(run))
        bad = copy.deepcopy(run)
        bad["repositories"][0]["extra_field"] = "x"
        with self.assertRaises(schema.SchemaError) as ctx:
            schema.validate_run(bad)
        self.assertIn("extra_field", str(ctx.exception))
        bad = copy.deepcopy(run)
        bad["repositories"][0]["stars"] = "5"
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(bad)
        bad = copy.deepcopy(run)
        bad["repositories"][0]["commits"][0]["sha"] = "not-a-sha"
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(bad)
        bad = copy.deepcopy(run)
        bad["repositories"][0]["full_name"] = "no-slash"
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(bad)
        bad = copy.deepcopy(run)
        bad["repositories"][0]["fetch_state"] = "exploded"
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(bad)
        bad = copy.deepcopy(run)
        bad["run_at"] = "yesterday"
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(bad)
        bad = copy.deepcopy(run)
        del bad["repositories"][0]["repository_id"]
        with self.assertRaises(schema.SchemaError):
            schema.validate_run(bad)

    def test_t26_strict_loader_reads_every_step1_fixture(self):
        from collector import schema

        for name in os.listdir(FIXTURES):
            folder = os.path.join(FIXTURES, name)
            # github_recorded holds canned API answers, not run files.
            if not os.path.isdir(folder) or name == "github_recorded":
                continue
            for f in sorted(os.listdir(folder)):
                run = schema.load_run_file(os.path.join(folder, f))
                self.assertIn("run_at", run)

    def test_t26_state_round_trips_through_the_strict_loader(self):
        from collector import schema, store

        state, _ = self.assertScenarioDeterministic("failures")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            store.save_state(state, path)
            loaded = store.load_state(path)
            self.assertEqual(json.dumps(loaded, sort_keys=True), json.dumps(state, sort_keys=True))
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            raw["repositories"]["100"]["unexpected"] = True
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(raw, fh)
            with self.assertRaises(schema.SchemaError):
                store.load_state(path)


class SortingTests(CollectorTestCase):
    def test_t27_null_sorts_after_known_values_never_as_zero(self):
        from collector import catalog

        values = [3, None, 0, 7, None, -1]
        ordered = sorted(values, key=catalog.nulls_last)
        self.assertEqual(ordered, [-1, 0, 3, 7, None, None])
        ordered_desc = sorted(values, key=catalog.nulls_last_desc)
        self.assertEqual(ordered_desc, [7, 3, 0, -1, None, None])
        self.assertNotEqual(catalog.nulls_last(None), catalog.nulls_last(0))
        self.assertEqual(sorted(["b", None, "a"], key=catalog.nulls_last), ["a", "b", None])
        state, _ = self.assertScenarioDeterministic("failures", upto=3)
        entries = catalog.build_catalog(state)
        entries.append({"id": 999, "stars": None})
        by_stars = catalog.sort_entries(entries, "stars")
        self.assertIsNone(by_stars[-1]["stars"])
        self.assertEqual([e["id"] for e in by_stars][:2], [100, 120])

"""Observation store: the persisted state and its canonical JSON form."""

import json
import os

from . import schema

SCHEMA_VERSION = 1


def empty_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_at": None,
        "last_run_at": None,
        "repositories": {},
        "events": [],
        "collection": {
            "searches": {},
            "roots": {},
            "incomplete": False,
            "incomplete_searches": [],
            "incomplete_roots": [],
        },
    }


def dumps(state):
    """Canonical serialisation: sorted keys, ASCII only, stable separators."""
    return json.dumps(state, sort_keys=True, indent=1, ensure_ascii=True) + "\n"


def save_state(state, path):
    schema.validate_state(state)
    data = dumps(state)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, path)


def load_state(path):
    with open(path, "r", encoding="utf-8") as fh:
        try:
            state = json.load(fh)
        except json.JSONDecodeError as exc:
            raise schema.SchemaError("%s: not valid JSON (%s)" % (path, exc)) from None
    schema.validate_state(state)
    if state["schema_version"] != SCHEMA_VERSION:
        raise schema.SchemaError("%s: unsupported schema_version %r" % (path, state["schema_version"]))
    return state


def get_observation(state, repository_id):
    return state["repositories"].get(str(repository_id))


def put_observation(state, obs):
    state["repositories"][str(obs["repository_id"])] = obs

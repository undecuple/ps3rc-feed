"""Observation store: the persisted state and its canonical JSON form."""

import json
import os
import tempfile

from . import schema

SCHEMA_VERSION = 1


def empty_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_done": False,
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
    return json.dumps(state, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False) + "\n"


def save_state(state, path):
    """Validate, then write atomically: exclusive temp file in the same directory, fsync, replace.

    Refuses to write through a symbolic link at `path`.
    """
    schema.validate_state(state)
    data = dumps(state).encode("utf-8")
    if os.path.islink(path):
        raise OSError("refusing to write state through a symbolic link: %s" % path)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.islink(path):
            raise OSError("refusing to write state through a symbolic link: %s" % path)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load_state(path):
    state = schema.read_json(path)
    schema.validate_state(state)
    if state["schema_version"] != SCHEMA_VERSION:
        raise schema.SchemaError("%s: unsupported schema_version %r" % (path, state["schema_version"]))
    return state


def get_observation(state, repository_id):
    return state["repositories"].get(str(repository_id))


def put_observation(state, obs):
    state["repositories"][str(obs["repository_id"])] = obs

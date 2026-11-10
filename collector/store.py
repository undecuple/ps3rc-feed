"""Observation store: the persisted state and its canonical JSON form."""

import json
import os
import tempfile

from . import schema

# 2: step 2 added fork relationships, comparisons and discovery records to
# every observation. A version 1 store is refused; rebuild it from the run
# files (the collector is deterministic, so a replay gives the same ids).
SCHEMA_VERSION = 2
SUPPORTED_VERSIONS = (2,)


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
            "pending_repository_ids": [],
            "rejected_fields": [],
            "last_run": {"last_run_at": None, "complete": True, "stopped": None, "not_fetched": 0, "error_count": 0},
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
    version = state.get("schema_version") if isinstance(state, dict) else None
    if version not in SUPPORTED_VERSIONS:
        raise schema.SchemaError(
            "%s: state schema_version %r is not supported (this collector writes version %d); "
            "rebuild the store by replaying the run files" % (path, version, SCHEMA_VERSION)
        )
    schema.validate_state(state)
    return state


def get_observation(state, repository_id):
    return state["repositories"].get(str(repository_id))


def put_observation(state, obs):
    state["repositories"][str(obs["repository_id"])] = obs

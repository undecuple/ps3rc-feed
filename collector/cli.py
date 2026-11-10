"""Read-only dry-run CLI.

    python3 -B -m collector.cli dry-run [--stale-threshold N] <fixture paths...>

Each argument is a scenario directory under tests/fixtures (its numbered
run files are applied in order) or a run file inside tests/fixtures. The
command prints, as JSON, what the collector would publish: the events of
every run, the resulting catalog and the collection status. It writes
nothing and has no network code. (`-B` stops the interpreter from writing
bytecode caches; the package also sets sys.dont_write_bytecode on import,
which covers every module imported after it.)

Input rules: anything that looks like a URL is refused before it is
touched; every path component inside the fixture root must be a real
directory or a regular file, never a symbolic link or a special file; the
resolved path must stay inside tests/fixtures. Directory entries are
checked the same way.
"""

import argparse
import json
import os
import re
import stat
import sys

from . import catalog, events, schema, store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_ROOT = os.path.join(REPO_ROOT, "tests", "fixtures")

_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


class RefusedInput(ValueError):
    """The input is not a plain path inside the fixture root."""


def _inside(real, real_root):
    return real == real_root or real.startswith(real_root + os.sep)


def _check_components(real, real_root):
    """Every component below the root must exist and must not be a symbolic link."""
    rel = os.path.relpath(real, real_root)
    if rel == ".":
        return
    walk = real_root
    for part in rel.split(os.sep):
        walk = os.path.join(walk, part)
        try:
            st = os.lstat(walk)
        except OSError:
            raise RefusedInput("refused: path does not exist inside the fixture root") from None
        if stat.S_ISLNK(st.st_mode):
            raise RefusedInput("refused: symbolic links are not accepted inside the fixture root")
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            raise RefusedInput("refused: not a regular file or directory")


def resolve_fixture_path(value, root=None):
    """Return the real path of `value` if it is a plain path inside `root`, else raise RefusedInput."""
    if root is None:
        root = FIXTURE_ROOT
    if not isinstance(value, str) or not value:
        raise RefusedInput("refused: empty input")
    if _URL_RE.match(value) is not None or "://" in value:
        raise RefusedInput("refused: URLs and scheme-prefixed inputs are not accepted")
    if "\x00" in value:
        raise RefusedInput("refused: invalid path")
    real_root = os.path.realpath(root)
    candidate = value if os.path.isabs(value) else os.path.join(REPO_ROOT, value)
    # Lexical normalisation first, so a symlink anywhere below the root is
    # seen as a symlink and not silently followed by realpath.
    normalised = os.path.normpath(candidate)
    if not _inside(normalised, real_root):
        real_candidate = os.path.realpath(candidate)
        if not _inside(real_candidate, real_root):
            raise RefusedInput("refused: path is outside the fixture root")
        raise RefusedInput("refused: path reaches the fixture root through a link")
    _check_components(normalised, real_root)
    real = os.path.realpath(normalised)
    if not _inside(real, real_root):
        raise RefusedInput("refused: path is outside the fixture root")
    return real


def collect_run_files(paths, root=None):
    if root is None:
        root = FIXTURE_ROOT
    real_root = os.path.realpath(root)
    files = []
    for value in paths:
        real = resolve_fixture_path(value, root)
        mode = os.lstat(real).st_mode
        if stat.S_ISDIR(mode):
            names = sorted(n for n in os.listdir(real) if n.endswith(".json"))
            if not names:
                raise RefusedInput("refused: no run files in %s" % os.path.relpath(real, REPO_ROOT))
            for name in names:
                entry = os.path.join(real, name)
                _check_components(entry, real_root)
                if not stat.S_ISREG(os.lstat(entry).st_mode):
                    raise RefusedInput("refused: %s is not a regular file" % name)
                files.append(entry)
        elif stat.S_ISREG(mode) and real.endswith(".json"):
            files.append(real)
        else:
            raise RefusedInput("refused: not a run file or scenario directory")
    return files


def dry_run(paths, stale_threshold=3, root=None):
    files = collect_run_files(paths, root)
    config = events.Config(stale_threshold=stale_threshold)
    state = store.empty_state()
    runs = []
    for path in files:
        run = schema.load_run_file(path)
        emitted = events.apply_run(state, run, config)
        runs.append(
            {
                "input": os.path.relpath(path, REPO_ROOT),
                "run_at": run["run_at"],
                "events": emitted,
            }
        )
    return {
        "dry_run": True,
        "inputs": [r["input"] for r in runs],
        "stale_threshold": stale_threshold,
        "runs": runs,
        "events": list(state["events"]),
        "catalog": catalog.build_catalog(state),
        "collection_status": catalog.collection_status(state),
    }


def build_parser():
    parser = argparse.ArgumentParser(prog="collector", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    dry = sub.add_parser("dry-run", help="print what would be published from fixture run files; writes nothing")
    dry.add_argument("--stale-threshold", type=int, default=3, help="consecutive failures before collection_stale (default 3)")
    dry.add_argument("paths", nargs="*", help="scenario directories or run files under tests/fixtures/")
    return parser


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2
    if args.command != "dry-run":
        parser.print_usage(sys.stderr)
        return 2
    if not args.paths:
        sys.stderr.write("refused: at least one fixture path is required\n")
        return 2
    try:
        payload = dry_run(args.paths, stale_threshold=args.stale_threshold)
    except RefusedInput as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    except (schema.SchemaError, ValueError, OSError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    sys.stdout.write(json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

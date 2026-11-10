"""Command line: a read-only dry run over fixtures, and the collector.

    python3 -B -m collector.cli dry-run [--stale-threshold N] <fixture paths...>
    python3 -B -m collector.cli collect --config PATH [--recorded FIXTURE] [--run-at TS]
    python3 -B -m collector.cli collect --config PATH --live --max-requests N --out data/<file>.json [--anonymous]

dry-run: each argument is a scenario directory under tests/fixtures (its
numbered run files are applied in order) or a run file inside
tests/fixtures. Prints, as JSON, what the collector would publish. Writes
nothing.

collect without --live: never touches the network. It prints the plan,
the list of requests (method, path, query) the collector makes or would
make, and nothing else. With --recorded it drives the collector against a
canned-answer file under tests/fixtures (same path rules as dry-run) and
prints the requests actually made. --out is refused without --live.

collect --live: the only path that constructs the live transport and the
only path that writes. It refuses to start unless --max-requests is given,
GITHUB_TOKEN is set (or --anonymous is passed), --out lies under
<repo>/data/, and the config uses none of the placeholder names of
config/sources.example.json. GITHUB_TOKEN is read from the environment
here and nowhere else, handed to the client as a Secret, and sent only as
the Authorization header to api.github.com. The run file is written
atomically to --out; stdout gets a summary without the token.

`-B` stops the interpreter from writing bytecode caches; the package also
sets sys.dont_write_bytecode on import.

Fixture path rules: anything that looks like a URL is refused before it is
touched; every path component inside the fixture root must be a real
directory or a regular file, never a symbolic link or a special file; the
resolved path must stay inside tests/fixtures.
"""

import argparse
import errno
import json
import os
import re
import secrets
import stat
import sys
from datetime import datetime, timezone

from . import catalog, collect, events, github, schema, store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_ROOT = os.path.join(REPO_ROOT, "tests", "fixtures")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_PARTIAL = 3
DATA_ROOT = os.path.join(REPO_ROOT, "data")

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
    col = sub.add_parser("collect", help="plan or run a collection; only --live reaches the network and writes")
    col.add_argument("--config", required=True, help="sources config (see config/sources.example.json)")
    col.add_argument("--recorded", help="canned-answer file under tests/fixtures/ to drive the collector without the network")
    col.add_argument("--run-at", help="run timestamp YYYY-MM-DDTHH:MM:SSZ (default: now, UTC)")
    col.add_argument("--live", action="store_true", help="use the live transport to api.github.com (read-only GET)")
    col.add_argument("--anonymous", action="store_true", help="with --live: allow running without GITHUB_TOKEN")
    col.add_argument("--max-requests", type=int, help="with --live: hard request budget for this run (required)")
    col.add_argument("--out", help="with --live: run file to write, under the data/ directory")
    col.add_argument("--overwrite", action="store_true", help="with --live: replace an existing --out file")
    return parser


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _print(payload):
    sys.stdout.write(json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n")


def _refuse(message):
    sys.stderr.write("refused: %s\n" % message)
    return 2


def resolve_out_path(value, overwrite=False):
    """The run file path for --live: a .json file under <repo>/data/, with no symbolic link on the way.

    An existing regular file at the target is refused unless overwrite is
    set (checked here before any request, and again at write time).

    A relative path resolves against the repository root. Every existing
    component from the repository root down to the file is lstat'ed; a
    symbolic link anywhere (data/ itself, a subdirectory, the file) is
    refused. Returns the absolute path; components that do not exist yet
    are created later by write_run_file.
    """
    if not isinstance(value, str) or not value:
        raise RefusedInput("refused: --live requires --out under the data/ directory")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise RefusedInput("refused: --out contains a control character")
    candidate = value if os.path.isabs(value) else os.path.join(REPO_ROOT, value)
    normalised = os.path.normpath(candidate)
    data_root = os.path.normpath(DATA_ROOT)
    if not normalised.startswith(data_root + os.sep):
        raise RefusedInput("refused: --out must lie under the data/ directory of the repository (%s)" % os.path.relpath(DATA_ROOT, REPO_ROOT))
    if not normalised.endswith(".json") or os.path.basename(normalised) == ".json":
        raise RefusedInput("refused: --out must be a .json file")
    walk = os.path.normpath(REPO_ROOT)
    for part in os.path.relpath(normalised, walk).split(os.sep):
        walk = os.path.join(walk, part)
        try:
            st = os.lstat(walk)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            raise RefusedInput("refused: --out path contains a symbolic link")
        if walk != normalised and not stat.S_ISDIR(st.st_mode):
            raise RefusedInput("refused: --out path component is not a directory")
        if walk == normalised and not stat.S_ISREG(st.st_mode):
            raise RefusedInput("refused: --out exists and is not a regular file")
        if walk == normalised and not overwrite:
            raise RefusedInput("refused: --out exists; pass --overwrite to replace it")
    return normalised


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


def _open_dir(name, dir_fd=None):
    """Open a directory without following a symbolic link in its last component."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            # Linux reports a link under O_DIRECTORY|O_NOFOLLOW as ENOTDIR; tell the two apart for the message.
            try:
                is_link = stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
            except OSError:
                is_link = exc.errno == errno.ELOOP
            if is_link:
                raise RefusedInput("refused: --out path contains a symbolic link") from None
            raise RefusedInput("refused: --out path component is not a directory") from None
        raise


def write_run_file(run, out_path, overwrite=False):
    """Write the run through directory descriptors, never following a link.

    The repository root is opened, then each component down to the target
    directory with O_DIRECTORY|O_NOFOLLOW relative to the previous
    descriptor (created with mkdir on that descriptor when missing, mode
    0755 before umask). The run goes to a randomly named temp file opened
    with O_CREAT|O_EXCL|O_NOFOLLOW on the directory descriptor, mode 0644,
    is fsync'ed, renamed over the target with both directory descriptors
    (after a final lstat of the target), and the directory is fsync'ed. An
    existing regular target is replaced only with overwrite=True; anything
    else at the target is refused. On any failure the temp file is removed.
    """
    data = (json.dumps(run, indent=1, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    root = os.path.normpath(REPO_ROOT)
    relative = os.path.relpath(os.path.normpath(out_path), root)
    if relative.startswith(os.pardir) or os.path.isabs(relative):
        raise RefusedInput("refused: --out must lie under the repository")
    parts = relative.split(os.sep)
    name = parts[-1]
    fd = _open_dir(root)
    try:
        for part in parts[:-1]:
            try:
                child = _open_dir(part, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                child = _open_dir(part, dir_fd=fd)
            os.close(fd)
            fd = child
        _check_target(name, fd, overwrite)
        tmp = "%s.%s.tmp" % (name, secrets.token_hex(8))
        tfd = os.open(tmp, _FILE_FLAGS, 0o644, dir_fd=fd)
        try:
            with os.fdopen(tfd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fchmod(fh.fileno(), 0o644)
                os.fsync(fh.fileno())
            _check_target(name, fd, overwrite)
            os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            raise
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_target(name, dir_fd, overwrite):
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise RefusedInput("refused: --out path contains a symbolic link")
    if not stat.S_ISREG(st.st_mode):
        raise RefusedInput("refused: --out exists and is not a regular file")
    if not overwrite:
        raise RefusedInput("refused: --out exists; pass --overwrite to replace it")


def _check_max_requests(value):
    if value is None:
        raise RefusedInput("refused: --live requires an explicit --max-requests")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > collect.MAX_REQUESTS_LIMIT:
        raise RefusedInput("refused: --max-requests must be between 1 and %d" % collect.MAX_REQUESTS_LIMIT)
    return value


def run_collect(args):
    config = collect.load_config(args.config)
    run_at = args.run_at or _now_utc()
    schema.TIMESTAMP.check(run_at, "--run-at")
    if args.max_requests is not None:
        _check_max_requests(args.max_requests)
    if args.live and args.recorded:
        raise RefusedInput("refused: --live and --recorded cannot be combined")

    if not args.live:
        if args.out:
            raise RefusedInput("refused: --out requires --live; without --live nothing is written")
        if args.recorded:
            recorded_path = resolve_fixture_path(args.recorded)
            if not stat.S_ISREG(os.lstat(recorded_path).st_mode):
                raise RefusedInput("refused: --recorded must name a regular file under tests/fixtures")
            transport = github.RecordedTransport.from_file(recorded_path)
            client = github.Client(transport, max_requests=config["max_requests"], page_caps=config["page_caps"])
            collect.collect(client, config, run_at)
            _print({"plan": client.log})
        else:
            _print({"plan": collect.plan_requests(config)})
        return EXIT_OK

    sys.stderr.write("LIVE: read-only GET requests to https://api.github.com will be made.\n")
    max_requests = _check_max_requests(args.max_requests)
    if args.anonymous:
        # The environment is not consulted at all: no Authorization header will be sent.
        token_text = None
    else:
        token_text = os.environ.get("GITHUB_TOKEN") or None
        if token_text is None:
            raise RefusedInput("refused: GITHUB_TOKEN is not set; export it or pass --anonymous for unauthenticated public access")
    out_path = resolve_out_path(args.out, overwrite=args.overwrite)
    placeholders = collect.config_uses_example_names(config)
    if placeholders:
        raise RefusedInput("refused: config uses %d placeholder name(s) from config/sources.example.json" % len(placeholders))
    try:
        token = github.Secret(token_text) if token_text is not None else None
    except ValueError as exc:
        raise RefusedInput("refused: GITHUB_TOKEN %s" % exc) from None

    transport = github.LiveTransport()
    client = github.Client(transport, token=token, max_requests=max_requests, page_caps=config["page_caps"])
    run, report = collect.collect(client, config, run_at)
    write_run_file(run, out_path, overwrite=args.overwrite)
    _print(
        {
            "written": os.path.relpath(out_path, REPO_ROOT),
            "run_at": run_at,
            "repositories": len(run["repositories"]),
            "complete": report["complete"],
            "stopped": report["stopped"],
            "not_fetched_repository_ids": report["not_fetched_repository_ids"][:100],
            "error_count": report["error_count"],
            "errors": report["errors"],
            "dropped_items": report["dropped_items"],
            "client": report["client"],
        }
    )
    return EXIT_OK if report["complete"] else EXIT_PARTIAL


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else EXIT_REFUSED
    try:
        if args.command == "collect":
            return run_collect(args)
        if args.command != "dry-run":
            parser.print_usage(sys.stderr)
            return EXIT_REFUSED
        if not args.paths:
            sys.stderr.write("refused: at least one fixture path is required\n")
            return EXIT_REFUSED
        payload = dry_run(args.paths, stale_threshold=args.stale_threshold)
    except RefusedInput as exc:
        sys.stderr.write("%s\n" % exc)
        return EXIT_REFUSED
    except (github.GitHubError, schema.SchemaError) as exc:
        # Our own exception classes: fixed wording, paths and counts only.
        sys.stderr.write("error: %s\n" % exc)
        return EXIT_ERROR
    except Exception as exc:
        # Anything else: name the class, never its text.
        sys.stderr.write("error: collection failed (%s)\n" % type(exc).__name__)
        return EXIT_ERROR
    _print(payload)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

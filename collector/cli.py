"""ps3rc-feed command line.

  dry-run <fixture paths...>        apply run files under tests/fixtures; print events, catalog, status; write nothing
  collect --config PATH             print the request plan; with --recorded FIXTURE drive the collector offline
  collect --config PATH --live ...  read-only GET requests to api.github.com; writes one run file under data/
                                    (--previous-state sets the fetch order from a state saved by export)
  export --runs FILE... --out-dir D apply run files under data/; write the site's feed.json and manifest.json to D
                                    (--state-in / --state-out carry the store from one export to the next)
  history --feed F --history-dir D  append a feed.json to the history directory D, both under data/ (docs/history.md)

Live runs need --max-requests (the effective budget is the smaller of the flag
and the config's max_requests), GITHUB_TOKEN or --anonymous, --out under data/
(--overwrite to replace a file) and a config without the example placeholders.
The run file write path is POSIX only. Every request, guard and write is
described in the docstrings of collector/github.py, collector/collect.py and the functions below.
"""

import argparse
import errno
import gzip
import json
import os
import re
import secrets
import stat
import sys
from datetime import datetime, timezone

from . import catalog, collect, events, export, github, history, schema, store
from .paths import DATA_ROOT, FIXTURE_ROOT, REPO_ROOT

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_PARTIAL = 3

# write_run_file adds 22 characters (".<16 hex>.tmp") for its temp file; common file systems allow 255 bytes.
MAX_OUT_NAME_BYTES = 200

MIN_STALE_MINUTES = 1
MAX_STALE_MINUTES = 100000

_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


class RefusedInput(ValueError):
    """The input is not a plain path inside the fixture root."""


def _inside(real, real_root):
    return real == real_root or real.startswith(real_root + os.sep)


def _check_components(real, real_root, label="the fixture root"):
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
            raise RefusedInput("refused: path does not exist inside %s" % label) from None
        if stat.S_ISLNK(st.st_mode):
            raise RefusedInput("refused: symbolic links are not accepted inside %s" % label)
        if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            raise RefusedInput("refused: not a regular file or directory")


def resolve_fixture_path(value, root=None, label="the fixture root", flag=None, missing_hint=None):
    """Return the real path of `value` if it is a plain path inside `root`, else raise RefusedInput.

    Anything that looks like a URL is refused before it is touched; every
    component inside the root must be a real directory or regular file,
    never a link or a special file; the resolved path must stay inside.
    With `flag`, a refusal names it; `missing_hint` is added when the path does not exist.
    """
    try:
        return _resolve_fixture_path(value, root, label)
    except RefusedInput as exc:
        if flag is None:
            raise
        text = str(exc).replace("refused: ", "refused: %s: " % flag, 1)
        if missing_hint and "does not exist" in text:
            text += "; " + missing_hint
        raise RefusedInput(text) from None


def _resolve_fixture_path(value, root, label):
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
            raise RefusedInput("refused: path is outside %s" % label)
        raise RefusedInput("refused: path reaches %s through a link" % label)
    _check_components(normalised, real_root, label)
    real = os.path.realpath(normalised)
    if not _inside(real, real_root):
        raise RefusedInput("refused: path is outside %s" % label)
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
    col.add_argument("--anonymous", action="store_true", help="ignore GITHUB_TOKEN and send no Authorization header; code queries are skipped (code search needs a token) and the run is incomplete")
    col.add_argument(
        "--max-requests",
        type=int,
        help="with --live: request budget for this run (required, 1-%d); the smaller of this and the config's max_requests applies" % collect.MAX_REQUESTS_LIMIT,
    )
    col.add_argument("--out", help="with --live: run file to write, a .json under the data/ directory")
    col.add_argument("--overwrite", action="store_true", help="with --live: replace an existing --out file")
    col.add_argument(
        "--previous-state",
        help="state saved by export --state-out (.json under data/): candidates never attempted come first (newest id first), "
        "then known ones by oldest last attempt; without it, newest id first. Roots and include entries always come first",
    )
    exp = sub.add_parser("export", help="apply run files under data/ and write the site feed (feed.json, manifest.json); no network")
    exp.add_argument("--runs", nargs="+", required=True, help="run files (.json) under data/; applied in run_at order")
    exp.add_argument("--out-dir", required=True, help="directory under data/ that receives feed.json and manifest.json")
    exp.add_argument("--overwrite", action="store_true", help="replace an existing feed.json, manifest.json or --state-out file")
    exp.add_argument("--state-in", help="saved state (.json under data/) to apply the runs on top of, instead of an empty store")
    exp.add_argument("--state-out", help="write the resulting state here (.json under data/); may equal --state-in with --overwrite")
    exp.add_argument(
        "--stale-after-minutes",
        type=int,
        help="set the feed's stale_after_minutes (%d-%d); omitted from the feed when not given" % (MIN_STALE_MINUTES, MAX_STALE_MINUTES),
    )
    his = sub.add_parser("history", help="append a feed.json to the history directory (events and change-only snapshots); no network")
    source = his.add_mutually_exclusive_group(required=True)
    source.add_argument("--feed", help="feed.json written by export (.json under data/): its new events and a snapshot of its repositories")
    source.add_argument("--import-feed", help="an older feed.json (.json under data/): only its events, marked with --label; no snapshot")
    his.add_argument("--label", help="with --import-feed: the source label of the imported events (lowercase letters, digits, -), e.g. legacy")
    his.add_argument("--history-dir", required=True, help="history directory under data/ (a checkout of the feed-history branch, or a new directory)")
    his.add_argument(
        "--compact-older-than-months",
        type=int,
        help="gzip the month files more than N months (1-%d) before the last run's month; off when not given" % history.MAX_COMPACT_MONTHS,
    )
    return parser


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _print(payload):
    sys.stdout.write(json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n")


def json_bytes(document):
    """The canonical file form of every document this command writes: sorted keys, ASCII, one trailing newline."""
    return (json.dumps(document, indent=1, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def _require_posix():
    """The descriptor-based write path uses dir_fd, O_NOFOLLOW and fchmod: POSIX only."""
    if os.name != "posix":
        raise RefusedInput("refused: live runs write through POSIX directory descriptors; this platform is not supported")


def resolve_out_path(value, overwrite=False, flag="--out"):
    """The run file path for --live: a .json file under <repo>/data/, with no symbolic link on the way.

    An existing regular file at the target is refused unless overwrite is
    set (checked here before any request, and again at write time).

    A relative path resolves against the repository root. Every existing
    component from the repository root down to the file is lstat'ed; a
    symbolic link anywhere (data/ itself, a subdirectory, the file) is
    refused. Returns the absolute path; components that do not exist yet
    are created later by write_run_file. POSIX only. `flag` names the
    option in refusal messages.
    """
    _require_posix()
    if not isinstance(value, str) or not value:
        raise RefusedInput("refused: --live requires --out under the data/ directory")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise RefusedInput("refused: %s contains a control character" % flag)
    candidate = value if os.path.isabs(value) else os.path.join(REPO_ROOT, value)
    normalised = os.path.normpath(candidate)
    data_root = os.path.normpath(DATA_ROOT)
    if not normalised.startswith(data_root + os.sep):
        raise RefusedInput("refused: %s must lie under the data/ directory of the repository (%s)" % (flag, os.path.relpath(DATA_ROOT, REPO_ROOT)))
    if not normalised.endswith(".json") or os.path.basename(normalised) == ".json":
        raise RefusedInput("refused: %s must be a .json file" % flag)
    if len(os.path.basename(normalised).encode("utf-8", errors="surrogateescape")) > MAX_OUT_NAME_BYTES:
        raise RefusedInput("refused: %s file name is longer than %d bytes" % (flag, MAX_OUT_NAME_BYTES))
    walk = os.path.normpath(REPO_ROOT)
    for part in os.path.relpath(normalised, walk).split(os.sep):
        walk = os.path.join(walk, part)
        try:
            st = os.lstat(walk)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            raise RefusedInput("refused: %s path contains a symbolic link" % flag)
        if walk != normalised and not stat.S_ISDIR(st.st_mode):
            raise RefusedInput("refused: %s path component is not a directory" % flag)
        if walk == normalised and not stat.S_ISREG(st.st_mode):
            raise RefusedInput("refused: %s exists and is not a regular file" % flag)
        if walk == normalised and not overwrite:
            raise RefusedInput("refused: %s exists; pass --overwrite to replace it" % flag)
    return normalised


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


def _open_dir(name, dir_fd=None, flag="--out"):
    """Open a directory without following a symbolic link in its last component. `flag` names the option in refusal messages."""
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
                raise RefusedInput("refused: %s path contains a symbolic link" % flag) from None
            raise RefusedInput("refused: %s path component is not a directory" % flag) from None
        raise


def write_run_file(run, out_path, overwrite=False, flag="--out"):
    """Write the run through directory descriptors, never following a link. POSIX only.

    The repository root is opened, then each component down to the target
    directory with O_DIRECTORY|O_NOFOLLOW relative to the previous
    descriptor (created with mkdir on that descriptor when missing, mode
    0755 before umask). The run goes to a randomly named temp file opened
    with O_CREAT|O_EXCL|O_NOFOLLOW on the directory descriptor, mode 0644,
    is fsync'ed and, after a final lstat of the target, published with both
    directory descriptors: renamed over the target with overwrite=True,
    otherwise hard-linked to the target name (which fails if anything is
    there, so the no-overwrite rule holds even against a target created
    after the check) and then unlinked. The directory is fsync'ed. An
    existing regular target is replaced only with overwrite=True; anything
    else at the target is refused. On any failure the temp file is removed.
    `flag` names the option in refusal messages.
    """
    _write_bytes(json_bytes(run), out_path, overwrite, flag)


def _write_bytes(data, out_path, overwrite=False, flag="--out"):
    """write_run_file for bytes already in their canonical form; the same descriptor-based path and rules."""
    _require_posix()
    root = os.path.normpath(REPO_ROOT)
    relative = os.path.relpath(os.path.normpath(out_path), root)
    if relative.startswith(os.pardir) or os.path.isabs(relative):
        raise RefusedInput("refused: %s must lie under the repository" % flag)
    parts = relative.split(os.sep)
    name = parts[-1]
    fd = _open_dir(root, flag=flag)
    try:
        for part in parts[:-1]:
            try:
                child = _open_dir(part, dir_fd=fd, flag=flag)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fd)
                child = _open_dir(part, dir_fd=fd, flag=flag)
            os.close(fd)
            fd = child
        _check_target(name, fd, overwrite, flag)
        tmp = "%s.%s.tmp" % (name, secrets.token_hex(8))
        tfd = os.open(tmp, _FILE_FLAGS, 0o644, dir_fd=fd)
        try:
            with os.fdopen(tfd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fchmod(fh.fileno(), 0o644)
                os.fsync(fh.fileno())
            _check_target(name, fd, overwrite, flag)
            if overwrite:
                os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
            else:
                # link() never replaces an existing name, so a target created after the check is refused, not overwritten.
                try:
                    os.link(tmp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    raise RefusedInput("refused: %s exists; pass --overwrite to replace it" % flag) from None
                os.unlink(tmp, dir_fd=fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            raise
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_target(name, dir_fd, overwrite, flag="--out"):
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise RefusedInput("refused: %s path contains a symbolic link" % flag)
    if not stat.S_ISREG(st.st_mode):
        raise RefusedInput("refused: %s exists and is not a regular file" % flag)
    if not overwrite:
        raise RefusedInput("refused: %s exists; pass --overwrite to replace it" % flag)


def _check_max_requests(value):
    if value is None:
        raise RefusedInput("refused: --live requires an explicit --max-requests")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > collect.MAX_REQUESTS_LIMIT:
        raise RefusedInput("refused: --max-requests must be between 1 and %d" % collect.MAX_REQUESTS_LIMIT)
    return value


def resolve_run_inputs(values):
    """Run files for export: regular .json files under data/, same path rules as the fixture inputs, no duplicates."""
    files = []
    for value in values:
        real = resolve_fixture_path(value, DATA_ROOT, label="the data/ directory", flag="--runs")
        if not (stat.S_ISREG(os.lstat(real).st_mode) and real.endswith(".json")):
            raise RefusedInput("refused: --runs takes .json run files under the data/ directory")
        if real in files:
            raise RefusedInput("refused: --runs names the same file twice")
        files.append(real)
    return files


def _check_path_text(value, flag):
    """Control characters and over-long components are refused before the path is resolved."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise RefusedInput("refused: %s contains a control character" % flag)
    for part in value.split(os.sep):
        if len(part.encode("utf-8", errors="surrogateescape")) > MAX_OUT_NAME_BYTES:
            raise RefusedInput("refused: %s has a component longer than %d bytes" % (flag, MAX_OUT_NAME_BYTES))


def resolve_export_targets(out_dir, overwrite=False):
    """(feed path, manifest path) inside --out-dir, both checked with the --out rules before anything is read."""
    if not isinstance(out_dir, str) or not out_dir:
        raise RefusedInput("refused: --out-dir must name a directory under the data/ directory")
    _check_path_text(out_dir, "--out-dir")
    targets = []
    for name in (export.FEED_NAME, export.MANIFEST_NAME):
        try:
            targets.append(resolve_out_path(os.path.join(out_dir, name), overwrite=overwrite, flag="--out-dir"))
        except RefusedInput as exc:
            raise RefusedInput("%s (%s)" % (exc, name)) from None
    return targets


def resolve_state_input(value, flag="--state-in"):
    """--state-in (or collect's --previous-state): a regular .json file under data/, with the same path rules as --runs."""
    _check_path_text(value, flag)
    real = resolve_fixture_path(value, DATA_ROOT, label="the data/ directory", flag=flag, missing_hint="run export --state-out first")
    if not (stat.S_ISREG(os.lstat(real).st_mode) and real.endswith(".json")):
        raise RefusedInput("refused: %s takes a .json state file under the data/ directory" % flag)
    return real


def resolve_state_output(value, overwrite=False):
    """--state-out: the --out rules (under data/, .json, no link, existing file only with --overwrite)."""
    if not isinstance(value, str) or not value:
        raise RefusedInput("refused: --state-out must name a .json file under the data/ directory")
    _check_path_text(value, "--state-out")
    return resolve_out_path(value, overwrite=overwrite, flag="--state-out")


def _check_stale_minutes(value):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not MIN_STALE_MINUTES <= value <= MAX_STALE_MINUTES:
        raise RefusedInput("refused: --stale-after-minutes must be between %d and %d" % (MIN_STALE_MINUTES, MAX_STALE_MINUTES))
    return value


def load_export_state(path, flag="--state-in"):
    """store.load_state for --state-in (or --previous-state), with the path in the message relative to the repository (exit 1 on failure)."""
    rel = os.path.relpath(path, REPO_ROOT)
    try:
        return store.load_state(path)
    except schema.SchemaError as exc:
        detail = str(exc).replace(path + ": ", "").replace(path, rel)
        raise schema.SchemaError("%s %s is not a usable state: %s" % (flag, rel, detail)) from None


def run_export(args):
    """Apply the run files to a store, then write the state, feed.json and manifest.json. Never touches the network.

    The store is empty, or the state saved by an earlier export when
    --state-in is given. The runs are applied oldest first (sorted by
    run_at), exactly as dry-run applies them. Two run files with the same
    run_at are refused, so the same files in any order give the same bytes,
    and so is a run whose run_at is not newer than the loaded state's
    last_run_at (it is older than, or already part of, that state).

    Every path is checked before anything is read: --out-dir targets,
    --state-out, --runs, --state-in, and that no output is also an input
    (--state-in may equal --state-out only with --overwrite; the state is
    read in full before the atomic replace). Then everything is read,
    validated and applied, and all output bytes are built before the first
    write, so a refusal or an invalid input leaves every file untouched.

    Write order: state, then feed, then manifest. Each write is atomic (temp
    file, then rename or link), so the only partial outcome is a failure
    between two writes. The state goes first because it is the record of
    which runs have been applied and the feed is derived from it: if a later
    write fails, the published feed and manifest are still the previous
    consistent pair, the next export from the saved state publishes a feed
    that includes this run, and repeating this run is refused (exit 2)
    instead of being applied twice. Writing the state last would instead
    let a failure after the feed leave a published feed that the saved
    state does not contain; the next run would then be applied without it
    and silently rewrite what the page already showed. The feed is written
    before the manifest, so a page that sees the new manifest version finds
    the matching feed. A manifest that appears between the pre-check and its
    write can leave a new feed.json without a manifest; rerunning with
    --overwrite repairs it (with --state-in equal to --state-out, rerun from
    a copy of the previous state, or wait for the next run).
    """
    feed_path, manifest_path = resolve_export_targets(args.out_dir, overwrite=args.overwrite)
    state_out = resolve_state_output(args.state_out, overwrite=args.overwrite) if args.state_out is not None else None
    stale_minutes = _check_stale_minutes(args.stale_after_minutes)
    files = resolve_run_inputs(args.runs)
    state_in = resolve_state_input(args.state_in) if args.state_in is not None else None
    outputs = [os.path.realpath(feed_path), os.path.realpath(manifest_path)]
    if any(path in files for path in outputs):
        raise RefusedInput("refused: --out-dir would replace one of the --runs files")
    if state_out is not None:
        if os.path.realpath(state_out) in files:
            raise RefusedInput("refused: --state-out would replace one of the --runs files")
        if os.path.realpath(state_out) in outputs:
            raise RefusedInput("refused: --state-out is the same file as a --out-dir output")
    if state_in is not None:
        if state_in in files:
            raise RefusedInput("refused: --state-in is also named in --runs")
        if state_in in outputs:
            raise RefusedInput("refused: --out-dir would replace the --state-in file")
    state = load_export_state(state_in) if state_in is not None else store.empty_state()
    loaded = sorted(((schema.load_run_file(path), path) for path in files), key=lambda item: item[0]["run_at"])
    for (earlier, earlier_path), (later, later_path) in zip(loaded, loaded[1:]):
        if earlier["run_at"] == later["run_at"]:
            raise RefusedInput(
                "refused: --runs %s and %s have the same run_at (%s); the order to apply them is undefined"
                % (os.path.relpath(earlier_path, REPO_ROOT), os.path.relpath(later_path, REPO_ROOT), earlier["run_at"])
            )
    oldest, oldest_path = loaded[0]
    if state["last_run_at"] is not None and oldest["run_at"] <= state["last_run_at"]:
        raise RefusedInput(
            "refused: --runs %s has run_at %s, which is not newer than the --state-in last run (%s); "
            "it is already applied or older, and runs are only applied forwards"
            % (os.path.relpath(oldest_path, REPO_ROOT), oldest["run_at"], state["last_run_at"])
        )
    runs = [run for run, _ in loaded]
    config = events.Config()
    for run in runs:
        events.apply_run(state, run, config)
    state_bytes = None
    if state_out is not None:
        schema.validate_state(state)
        state_bytes = store.dumps(state).encode("utf-8")
    feed = export.build_feed(state, stale_after_minutes=stale_minutes)
    feed_bytes = json_bytes(feed)
    manifest = export.build_manifest(feed, feed_bytes)
    written = []
    if state_out is not None:
        _write_bytes(state_bytes, state_out, overwrite=args.overwrite, flag="--state-out")
        written.append(os.path.relpath(state_out, REPO_ROOT))
    write_run_file(feed, feed_path, overwrite=args.overwrite, flag="--out-dir")
    write_run_file(manifest, manifest_path, overwrite=args.overwrite, flag="--out-dir")
    written += [os.path.relpath(feed_path, REPO_ROOT), os.path.relpath(manifest_path, REPO_ROOT)]
    _print(
        {
            "written": written,
            "runs": len(runs),
            "generated_at": feed["generated_at"],
            "version": manifest["version"],
            "repos": len(feed["repos"]),
            "events": len(feed["events"]),
            "events_in_store": len(state["events"]),
        }
    )
    return EXIT_OK


HISTORY_ROOT_ENTRIES = (".git", history.README_NAME, history.INDEX_NAME) + history.KINDS


def resolve_history_dir(value):
    """--history-dir: a directory strictly under data/, with no symbolic link and no non-directory on the way.

    The directory may not exist yet (it is created on the first write).
    Returns the absolute, normalised path.
    """
    if not isinstance(value, str) or not value:
        raise RefusedInput("refused: --history-dir must name a directory under the data/ directory")
    _check_path_text(value, "--history-dir")
    candidate = value if os.path.isabs(value) else os.path.join(REPO_ROOT, value)
    normalised = os.path.normpath(candidate)
    data_root = os.path.normpath(DATA_ROOT)
    if not normalised.startswith(data_root + os.sep):
        raise RefusedInput("refused: --history-dir must lie under the data/ directory of the repository (%s)" % os.path.relpath(DATA_ROOT, REPO_ROOT))
    walk = os.path.normpath(REPO_ROOT)
    for part in os.path.relpath(normalised, walk).split(os.sep):
        walk = os.path.join(walk, part)
        try:
            st = os.lstat(walk)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            raise RefusedInput("refused: --history-dir path contains a symbolic link")
        if not stat.S_ISDIR(st.st_mode):
            raise RefusedInput("refused: --history-dir path component is not a directory")
    return normalised


def resolve_feed_input(value, flag, history_dir):
    """--feed or --import-feed: a regular .json file under data/ (the --runs path rules), outside the history directory."""
    _check_path_text(value, flag)
    real = resolve_fixture_path(value, DATA_ROOT, label="the data/ directory", flag=flag)
    st = os.lstat(real)
    if not (stat.S_ISREG(st.st_mode) and real.endswith(".json")):
        raise RefusedInput("refused: %s takes a .json feed file under the data/ directory" % flag)
    if real == history_dir or real.startswith(history_dir + os.sep) or real.startswith(os.path.realpath(history_dir) + os.sep):
        raise RefusedInput("refused: %s lies inside --history-dir" % flag)
    if st.st_size > history.MAX_FEED_BYTES:
        raise RefusedInput("refused: %s is larger than %d bytes" % (flag, history.MAX_FEED_BYTES))
    return real


def _read_regular(path, what):
    """Bytes of a regular file opened without following a link (refused when it is a link or not a regular file)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RefusedInput("refused: %s is a symbolic link" % what) from None
        raise
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise RefusedInput("refused: %s is not a regular file" % what)
        return fh.read()


def read_history_dir(history_dir):
    """(index bytes or None, {month file name: bytes}, README bytes or None) of a history directory.

    Only the history's own entries may be there: .git (left alone), README.md,
    index.json, events/ and snapshots/ holding month files. Anything else, a
    symbolic link or a special file is refused, so nothing unexpected is
    committed with the history.
    """
    if not os.path.lexists(history_dir):
        return None, {}, None
    index_bytes = None
    readme = None
    files = {}
    for name in sorted(os.listdir(history_dir)):
        path = os.path.join(history_dir, name)
        st = os.lstat(path)
        if name not in HISTORY_ROOT_ENTRIES:
            raise RefusedInput("refused: --history-dir holds %r, which is not part of the history" % name)
        if name == ".git":
            continue
        if stat.S_ISLNK(st.st_mode):
            raise RefusedInput("refused: --history-dir entry %s is a symbolic link" % name)
        if name in history.KINDS:
            if not stat.S_ISDIR(st.st_mode):
                raise RefusedInput("refused: --history-dir entry %s is not a directory" % name)
            for entry in sorted(os.listdir(path)):
                rel = name + "/" + entry
                if history.FILE_RE.fullmatch(rel) is None:
                    raise RefusedInput("refused: --history-dir holds %r, which is not a month file" % rel)
                files[rel] = _read_regular(os.path.join(path, entry), rel)
        elif name == history.INDEX_NAME:
            index_bytes = _read_regular(path, name)
        else:
            readme = _read_regular(path, name)
    return index_bytes, files, readme


def _check_compact_months(value):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= history.MAX_COMPACT_MONTHS:
        raise RefusedInput("refused: --compact-older-than-months must be between 1 and %d" % history.MAX_COMPACT_MONTHS)
    return value


def run_history(args):
    """Append one feed to the history directory (docs/history.md). Never touches the network.

    --feed applies a run: the feed's events whose id is not stored yet go to
    the month of their observed_at, and a snapshot of its repositories
    (header plus changed repositories only) goes to the month of its
    generated_at. A feed whose generated_at equals the history's last run is
    a no-op (exit 0); an older one is refused (exit 2). --import-feed adds
    an older feed's events with "source": --label and no snapshot.

    Every path is checked first: --history-dir under data/ with no link,
    the feed a regular .json under data/ outside the history directory.
    Then the history is read and checked against its index.json, the feed
    is validated, and every output is built before the first write. A file
    over history.MAX_FILE_BYTES is refused before anything is written. The
    month files are written first and index.json last, each atomically
    (temp file, then rename); a failure in between leaves an index that no
    longer matches, and the next run stops with an error instead of
    building on it (the history directory is a git checkout: restore it
    with git). Nothing outside the history directory is written, and
    nothing is read but the feed and the history directory.
    """
    history_dir = resolve_history_dir(args.history_dir)
    compact = _check_compact_months(args.compact_older_than_months)
    if args.import_feed is not None:
        if args.label is None or history.SOURCE_LABEL_RE.fullmatch(args.label) is None:
            raise RefusedInput("refused: --import-feed needs --label: 1-32 lowercase letters, digits or -, starting with a letter")
        feed_path = resolve_feed_input(args.import_feed, "--import-feed", history_dir)
    else:
        if args.label is not None:
            raise RefusedInput("refused: --label is only used with --import-feed")
        feed_path = resolve_feed_input(args.feed, "--feed", history_dir)
    index_bytes, files, readme = read_history_dir(history_dir)
    rel_feed = os.path.relpath(feed_path, REPO_ROOT)
    try:
        archive = history.History.load(index_bytes, files)
    except schema.SchemaError as exc:
        raise schema.SchemaError("--history-dir %s is not a usable history: %s" % (os.path.relpath(history_dir, REPO_ROOT), exc)) from None
    try:
        feed = history.validate_feed(schema.read_json(feed_path), require_collection=args.import_feed is None)
    except schema.SchemaError as exc:
        detail = str(exc).replace(feed_path + ": ", "").replace(feed_path, rel_feed)
        raise schema.SchemaError("%s is not a usable feed: %s" % (rel_feed, detail)) from None
    summary = {"history_dir": os.path.relpath(history_dir, REPO_ROOT), "feed": rel_feed}
    if args.import_feed is not None:
        added, known = archive.import_events(feed, args.label)
        summary.update({"mode": "import", "label": args.label, "events_added": added, "events_already_stored": known})
    else:
        applied = archive.apply_run(feed)
        if applied is None:
            summary.update({"mode": "run", "run_at": feed["generated_at"], "unchanged": True})
            sys.stderr.write("the history already holds the run of %s; nothing to do\n" % feed["generated_at"])
            _print(summary)
            return EXIT_OK
        summary.update({"mode": "run"}, **applied)
    compacted = archive.compact(compact) if compact is not None else []
    new_files = archive.render()
    for name, raw in sorted(new_files.items()):
        if len(raw) > history.MAX_FILE_BYTES:
            raise history.Refused(
                "refused: %s would be %d bytes, over the per-file limit of %d bytes; nothing was written (see docs/history.md)"
                % (name, len(raw), history.MAX_FILE_BYTES)
            )
    new_index = archive.index_bytes(new_files)
    readme_bytes = history.README_TEXT.encode("utf-8")
    writes = [(name, raw) for name, raw in sorted(new_files.items()) if files.get(name) != raw]
    removals = sorted(name for name in files if name not in new_files)
    if readme != readme_bytes:
        writes.append((history.README_NAME, readme_bytes))
    if index_bytes != new_index:
        writes.append((history.INDEX_NAME, new_index))
    for name, raw in writes:
        reason = history.unsafe_text(raw if not name.endswith(".gz") else gzip.decompress(raw))
        if reason is not None:
            raise schema.SchemaError("%s would hold %s; nothing was written" % (name, reason))
    for name, raw in writes:
        if name == history.INDEX_NAME:
            continue
        _write_bytes(raw, os.path.join(history_dir, *name.split("/")), overwrite=True, flag="--history-dir")
    for name in removals:
        path = os.path.join(history_dir, *name.split("/"))
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise RefusedInput("refused: %s is not a regular file" % name)
        os.unlink(path)
    if index_bytes != new_index:
        _write_bytes(new_index, os.path.join(history_dir, history.INDEX_NAME), overwrite=True, flag="--history-dir")
    total = sum(len(raw) for raw in new_files.values())
    summary.update(
        {
            "written": [name for name, _ in writes],
            "removed": removals,
            "compacted": compacted,
            "files": {name: len(raw) for name, raw in sorted(new_files.items())},
            "total_bytes": total,
            "file_limit_bytes": history.MAX_FILE_BYTES,
            "budget_bytes": history.BUDGET_BYTES,
            "over_budget": total > history.BUDGET_BYTES,
        }
    )
    if total > history.BUDGET_BYTES:
        sys.stderr.write(
            "warning: the history holds %d bytes, over the budget of %d bytes; plan compaction or thinning (docs/history.md)\n"
            % (total, history.BUDGET_BYTES)
        )
    _print(summary)
    return EXIT_OK


def _code_queries(config):
    return sum(1 for q in config["queries"] if collect.query_kind(q) == "code")


def run_collect(args):
    """Plan, replay or run a collection.

    Code search needs a token: with --anonymous the config's code queries are
    left out of the plan (the payload says how many) and are not asked in a
    live run, which is then incomplete; each such search is recorded with
    skipped "no_token". Refusing instead would block every anonymous run of
    the reviewed config, while skipping sends nothing GitHub would refuse and
    keeps the gap visible in the run file.

    --previous-state names the state the export keeps (export --state-out).
    Only its repositories' last_attempted_refresh_at values are used, to put
    the candidates in fetch order (collect.fetch_order): never attempted
    first, newest id first, then the oldest attempt first, so the cut made by
    max_repositories rotates instead of always leaving out the same ids. The
    state is read, never written; it must be a valid state under data/ (a
    missing or unusable file is refused, exit 2 or 1, before any request),
    and --out must not name it. Without the flag every candidate counts as
    never attempted.
    """
    config = collect.load_config(args.config)
    run_at = args.run_at or _now_utc()
    schema.TIMESTAMP.check(run_at, "--run-at")
    if args.max_requests is not None:
        _check_max_requests(args.max_requests)
    if args.live and args.recorded:
        raise RefusedInput("refused: --live and --recorded cannot be combined")
    previous_path = resolve_state_input(args.previous_state, flag="--previous-state") if args.previous_state is not None else None
    if previous_path is not None and args.live and args.out:
        if os.path.realpath(resolve_out_path(args.out, overwrite=True)) == previous_path:
            raise RefusedInput("refused: --out would replace the --previous-state file")
    last_attempted = None
    previous_summary = None
    if previous_path is not None:
        previous = load_export_state(previous_path, flag="--previous-state")
        last_attempted = collect.last_attempted_from_state(previous)
        previous_summary = {"known_repositories": len(last_attempted), "last_run_at": previous["last_run_at"]}

    if not args.live:
        if args.out:
            raise RefusedInput("refused: --out requires --live; without --live nothing is written")
        if args.recorded:
            recorded_path = resolve_fixture_path(args.recorded, flag="--recorded")
            if not stat.S_ISREG(os.lstat(recorded_path).st_mode):
                raise RefusedInput("refused: --recorded must name a regular file under tests/fixtures")
            transport = github.RecordedTransport.from_file(recorded_path)
            client = github.Client(transport, max_requests=config["max_requests"], page_caps=config["page_caps"])
            collect.collect(client, config, run_at, last_attempted)
            payload = {"plan": client.log}
        else:
            payload = {"plan": collect.plan_requests(config, authenticated=not args.anonymous)}
            skipped = _code_queries(config) if args.anonymous else 0
            if skipped:
                payload["skipped_code_queries"] = skipped
        if previous_summary is not None:
            payload["previous_state"] = previous_summary
        budget = config["max_requests"] if args.max_requests is None else min(args.max_requests, config["max_requests"])
        sys.stderr.write("%d requests planned; budget %d\n" % (len(payload["plan"]), budget))
        _print(payload)
        return EXIT_OK

    sys.stderr.write("LIVE: read-only GET requests to https://api.github.com will be made.\n")
    # The budget is the smaller of the flag and the config: neither can raise the other.
    max_requests = min(_check_max_requests(args.max_requests), config["max_requests"])
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

    if token is None and _code_queries(config):
        sys.stderr.write("note: %d code search queries are skipped without a token; the run will be incomplete.\n" % _code_queries(config))
    transport = github.LiveTransport()
    client = github.Client(transport, token=token, max_requests=max_requests, page_caps=config["page_caps"])
    run, report = collect.collect(client, config, run_at, last_attempted)
    write_run_file(run, out_path, overwrite=args.overwrite)
    summary = {
        "written": os.path.relpath(out_path, REPO_ROOT),
        "run_at": run_at,
        "repositories": len(run["repositories"]),
        "complete": report["complete"],
        "stopped": report["stopped"],
        "effective_max_requests": max_requests,
        "not_fetched_repository_ids": report["not_fetched_repository_ids"][:100],
        "error_count": report["error_count"],
        "errors": report["errors"],
        "dropped_items": report["dropped_items"],
        "skipped_searches": report["skipped_searches"],
        "code_search_limited": report["code_search_limited"],
        "client": report["client"],
    }
    if previous_summary is not None:
        summary["previous_state"] = previous_summary
    _print(summary)
    if report["stopped"] == "budget":
        sys.stderr.write("stopped: request budget of %d reached; %d repositories not fetched\n" % (max_requests, len(report["not_fetched_repository_ids"])))
    elif report["stopped"] == "unauthorized":
        if token is not None:
            sys.stderr.write("token rejected by GitHub (401); check or regenerate the token\n")
        else:
            sys.stderr.write("request rejected by GitHub (401) without a token\n")
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
        if args.command == "export":
            return run_export(args)
        if args.command == "history":
            return run_history(args)
        if args.command != "dry-run":
            parser.print_usage(sys.stderr)
            return EXIT_REFUSED
        if not args.paths:
            sys.stderr.write("refused: at least one fixture path is required\n")
            return EXIT_REFUSED
        payload = dry_run(args.paths, stale_threshold=args.stale_threshold)
    except (RefusedInput, history.Refused) as exc:
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

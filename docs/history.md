# History

`feed.json` keeps the newest 500 events and the current values of each repository, and the `feed-state` branch
keeps only the newest state. The `feed-history` branch keeps the rest: every event the feed has shown, and how the
tracked repository values changed from run to run. History is written only when the scheduled refresh runs (see
"Scheduled refresh" in the README). Nothing is collected for the time the refresh is off.

## Layout of the `feed-history` branch

    README.md                  short note, written by the command
    index.json                 format version, last run, run count, months, and per file: bytes, lines, sha256
    events/YYYY-MM.jsonl       one feed event per line
    snapshots/YYYY-MM.jsonl    one header line per run, then the repositories that changed in that run
    events/YYYY-MM.jsonl.gz    a compacted month (the same lines, gzip)

Every line is one JSON object with sorted keys and no spaces, in ASCII (so also valid UTF-8), ending in a newline.
`index.json` uses the same form as `feed.json`. The same history and the same feed always give the same bytes.

**Events.** An event line has the keys of a feed event: `id`, `type`, `repo_id`, `source_at`, `observed_at` and
`summary`. An event goes to the month of its `observed_at`, which is the time of the run that first saw it. Lines
in a month are sorted by `source_at` (or `observed_at` when `source_at` is null), then by `id`. An event id is
stored once. A run that brings an id the history already holds adds nothing, so applying the same feed twice
gives the same files. Events imported from an older feed carry one more key, `"source": "legacy"`.

**Snapshots.** Each run adds a header line to the month of the run:
`{"run_at", "complete", "stopped", "not_fetched", "repos"}`, taken from the feed's `collection` object and the
number of repositories in the feed. After it come repository lines with the tracked values:
`{"id", "repo", "stars", "commits_30d", "category", "archived", "fork", "last_commit_at"}`. A repository gets a
line only when one of these values differs from its previous line. Its first line is a full line. A repository
that leaves the feed gets `{"id", "repo", "removed": true}`. When it comes back it gets a full line again. Lines
of one run are in repository id order. To read the values at a run, replay the snapshot files from the first
month to that run's header.

Only these fields are copied. Descriptions, game names and other repository text never reach the snapshots, so
an email address in a description is not copied. Event summaries are built by the collector from fixed templates,
but a release tag inside one comes from the repository. An email address, a token-like string (`ghp_`,
`github_pat_` and similar) or a URL other than `https://github.com/...` in a summary is replaced by `[removed]`.
Before a file is written it is scanned for them again, and the command stops with an error if one is found.

## The command

    # apply one run: new events and a snapshot
    python3 -B -m collector history --feed data/site/feed.json --history-dir data/history

    # import the events of an older feed once, by hand: no snapshot, marked "source": "legacy"
    python3 -B -m collector history --import-feed data/legacy-feed.json --label legacy --history-dir data/history

    # either form, and also gzip the month files more than 3 months before the last run
    python3 -B -m collector history --feed data/site/feed.json --history-dir data/history --compact-older-than-months 3

It reads only the feed and the history directory and writes only inside the history directory. It needs only
`feed.json`, because the feed holds the events, the repositories and the collection result of the run.

- `--history-dir` must lie under `data/` with no symbolic link on the way. It may be new. It may hold only
  `.git` (left alone), `README.md`, `index.json`, `events/` and `snapshots/` with month files. Anything else, a
  symbolic link or a special file is refused.
- The feed must be a regular `.json` file under `data/`, outside the history directory, at most 16 MB. It is
  checked against the exact shape `export` writes (every key, type and link), and a run feed must have the
  `collection` object. An older feed for `--import-feed` may lack it.
- The history is read and checked before anything is written: every line must parse, be in canonical form and in
  order, and `index.json` must match the files byte for byte. A failed check is an error.
- A feed whose `generated_at` equals the last run in the history is a no-op: the command says so and exits 0.
  An older feed is refused. Runs are only applied forwards.
- Every output is built before the first write. Each file is written to a temporary file and renamed over the
  old one. The month files are written first and `index.json` last. If a write fails in between, the index no
  longer matches and the next run stops with an error. The directory is a git checkout, so `git checkout .` and
  `git clean -fd` in it restore the last commit.
- The summary lists the files written, the size of every month file, the total and the limits.

| Exit code | Meaning |
| --- | --- |
| 0 | written, or nothing to do (the same run again, or an import with no new event) |
| 1 | error: an invalid feed, a history that does not match its index, or text that may not be stored |
| 2 | refused: a path, an older run, a file over the size limit, or a wrong option |

## Rotation and limits

- **Monthly files.** A run writes to the month of its run time, so files of past months stop changing. One
  exception: when the history missed runs, the next feed can still hold older events, and they go to their own
  months.
- **Retention.** Events are kept forever. Snapshots stay change-only. After 24 months of history, a separate,
  reviewed change may thin old snapshots (for example, keep one run a day). Nothing is thinned automatically.
- **Compaction.** `--compact-older-than-months N` (1 to 1200, off when not given) gzips each month file whose month
  is more than N months before the month of the last run, and updates `index.json`. With N = 3 and a last run in
  November, July and earlier are compacted and August stays plain. A compacted month that receives a late event is
  rewritten as gzip. The scheduled refresh does not pass this option yet. Turning it on is a reviewed change to
  `.github/workflows/refresh.yml`.
- **Per-file limit: 50 MB.** The command refuses to write a month file larger than 50 MB (52,428,800 bytes), with
  a message, before any file is written.
- **Budget: 500 MB.** When the files together pass 500 MB (524,288,000 bytes), the command still writes but prints
  a warning, and the summary says `over_budget`. That is the time to compact or plan thinning.

GitHub advises keeping a repository well under 1 GB, warns about files over 50 MB and blocks files over 100 MB.
The limits above keep the branch inside that.

## Why the branch stays small

Measured on the current `site/data/feed.json`: a snapshot line is about 170 bytes and an event line about 200
bytes. With about 400 repositories and 4 runs a day (about 120 runs a month):

- Full snapshots would be 400 x 170 bytes = 68 KB a run, about 8 MB a month and about 100 MB a year.
- Change-only snapshots store only what changed. Stars change rarely, and `commits_30d` changes when commits enter
  or leave the 30-day window. If about 160 repositories change a day, that is about 27 KB a day, under 1 MB a
  month. Even if 250 active repositories changed in every run, a month would be about 5 MB. Run headers add about
  12 KB a month.
- Events: 10 to 60 new events a day at 200 bytes are 60 to 360 KB a month.

So a month is usually 1 to 2 MB of files and at most about 6 MB, far under the 50 MB file limit, and a year is
roughly 12 to 25 MB. Gzip makes a compacted month about 7 times smaller.

Git keeps every version of a file. Each run rewrites the current month files, but the commits only append lines,
so git stores the versions as small deltas in its packs. The history of a month costs about its final size,
which is why the branch uses plain commits and monthly files instead of one growing file or a force-pushed
branch.

## Workflow

The `publish-history` job of `.github/workflows/refresh.yml` runs after `collect` under the same conditions as
`publish-state`, on the first run too. It checks out `main`, checks out `feed-history` into `data/history` when
the branch exists, or starts it with `git init`. It downloads `feed.json` from the run's artifact and checks it
like `publish-state` does. Then it runs the command and commits only when a file changed. It pushes with a normal,
fast-forward push to `refs/heads/feed-history`. If the branch moved meanwhile, the push fails and nothing is
overwritten. `publish-state` and `deploy` do not wait for this job, so a failure here blocks neither.

To import the events of the feed published by hand before the refresh started, check out `feed-history` into
`data/history`, copy that `feed.json` to `data/legacy-feed.json`, run the `--import-feed` command above, and commit
and push the result with a normal push.

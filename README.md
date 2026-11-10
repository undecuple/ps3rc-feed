<p align="center">
  <img src="docs/assets/banner@2x.png" width="1280" alt="ps3rc-feed: automated, unofficial PS3 recompilation activity feed">
</p>

# ps3rc-feed

An automated activity feed for PS3 recompilation projects on GitHub. The goal is a searchable index of repositories
(including forks of ps3recomp and of game projects), an RSS feed, a Discord channel feed, and a terminal-style website.

**Status: early.** The collector core exists and runs on recorded and fictional data, and can make a live read-only
run. The static page in `site/` is a design preview on sample data (see `site/README.md`). No real data is published:
there is no RSS feed, no Discord bot, and no dataset in this repository.

## What it will and will not be

- An automated, unofficial index. Project discovery, game associations, toolkit detection and descriptions may be
  wrong, incomplete or out of date. Those labels are inferred.
- A listing does not mean a project is playable, official, safe or endorsed, and does not verify who wrote it.
  Follow the link and read the project's own pages.
- It reports observed repository activity from public GitHub data, not game compatibility or project quality.
- It does not host game files, binaries or builds.

## What exists

A Python standard-library collector in `collector/`, developed and tested on Python 3.14, with offline unit tests in
`tests/` that run in CI. The tests block network access.

- **Event model:** repositories are tracked by GitHub repository ID, observations are persisted, and the first run is
  a baseline that creates no events. A re-run on unchanged data creates none either. Unknown values stay `null`;
  incomplete collection is flagged as incomplete.
- **Forks:** fork networks are followed from configured root repositories, with ancestry kept as reported. A
  repository reached through several roots is one entry. A game association inherited from a parent is labelled
  inherited and unverified.
- **GitHub client:** read-only GET requests to `api.github.com`, with a hard request budget, a stop on rate limits,
  size and time limits, and repository text treated as untrusted data.
- **Site export:** `export` applies run files from `data/` through the same event model, in run order, and writes the
  `feed.json` and `manifest.json` the page in `site/` reads. Text is plain (the page sets it with `textContent`), the
  only links are `https://github.com/owner/name` repository URLs, unknown values are `null`, and the newest 500
  events are kept. The output is deterministic: the same run files in any order give the same bytes (two run files
  with the same `run_at` are refused), `generated_at` is the newest run's `run_at`, and the manifest `version` is the
  first 16 hex digits of the feed's SHA-256.

Not built yet: a persisted store between runs (export replays the run files each time), publishing the exported feed
to the site, and the RSS and Discord feeds.

## Using it

    python3 -B -m unittest discover -s tests

    # request plan only, no network
    python3 -B -m collector.cli collect --config config/sources.example.json

    # live, read-only GET requests to api.github.com
    GITHUB_TOKEN=... python3 -B -m collector.cli collect --config <your config> --live \
        --max-requests 300 --out data/run.json

    # site feed from run files, no network
    python3 -B -m collector export --runs data/run-001.json data/run-002.json --out-dir data/site

A live run needs `--max-requests` and `--out` under `data/` (which git ignores), and either `GITHUB_TOKEN` (a token
with no permissions is enough for public data) or `--anonymous`. It refuses the placeholder example config. Exit
codes: 0 complete, 3 partial (for example stopped by the budget or a rate limit), 1 error, 2 refused. The live write
path uses POSIX file operations, so it targets Linux and macOS. `export` follows the same rules: inputs and
`--out-dir` under `data/`, no symbolic links, existing files replaced only with `--overwrite`; it exits 0 when written,
1 on an invalid run file, 2 when refused. `python3 -m collector` and `python3 -m collector.cli` are the same command.

`config/sources.example.json` holds fictional placeholders only. The real roots and search queries are not committed.

## Licence

[MIT](LICENSE). The banner and social preview are text-only renders set in IBM Plex Mono (SIL Open Font License 1.1).

Developed with AI agent assistance.

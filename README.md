<p align="center">
  <img src="docs/assets/banner@2x.png" width="1280" alt="ps3rc-feed: automated, unofficial PS3 recompilation activity feed">
</p>

# ps3rc-feed

An automated, unofficial activity feed for PS3 recompilation projects on GitHub.

[Live page](https://undecuple.github.io/ps3rc-feed/) · [Tests](https://github.com/undecuple/ps3rc-feed/actions/workflows/tests.yml) · [MIT licence](LICENSE)

**Live page:** <https://undecuple.github.io/ps3rc-feed/>. It shows real collected data, published by hand. It is not
real time, and the page shows its own collection time. The last collection may be incomplete, and the page says so.

## What it is and is not

- An automated, unofficial index. Project discovery, game associations, toolkit detection and descriptions may be
  wrong, incomplete or out of date. Those labels are inferred.
- A listing does not mean a project is playable, official, safe or endorsed, and does not verify who wrote it.
  Follow the link and read the project's own pages.
- It reports observed repository activity from public GitHub data, not game compatibility or project quality.
- It does not host game files, binaries or builds.

## What exists

- **Live page** in `site/`: a feed, a catalog table and a fork view, with filters, search, sorting by clicking a column
  heading, a stars column, and headings that stay visible while scrolling. Forks with no stars and no recent commits are hidden until you search or switch them on. Details are in `site/README.md`.
- **Collector** in `collector/`: a Python standard-library program, developed and tested on Python 3.14. It makes
  read-only GET requests to `api.github.com` with a hard request budget and stops on rate limits.
- **Event model:** repositories are tracked by GitHub repository ID, and the first run is a baseline that creates no
  events. Unknown values stay `null`. Incomplete collection is flagged as incomplete.
- **Forks:** fork networks are followed from configured root repositories. A game association inherited from a parent
  is labelled inherited and unverified.
- **Export:** `export` turns run files into the `feed.json` and `manifest.json` the page reads, and can keep its state
  between runs. See [docs/export.md](docs/export.md).
- **Tests:** offline unit tests in `tests/` run in CI. They block network access.

Not built yet: RSS and Discord feeds.

## Using it

    python3 -B -m unittest discover -s tests

    # request plan only, no network
    python3 -B -m collector collect --config config/sources.example.json

    # live, read-only GET requests to api.github.com
    GITHUB_TOKEN=... python3 -B -m collector collect --config <your config> --live \
        --max-requests 300 --out data/run.json

    # site feed from run files, no network
    python3 -B -m collector export --runs data/run-001.json --out-dir data/site

    # next time: apply one new run on top of the saved state
    python3 -B -m collector export --runs data/run-002.json --out-dir data/site --overwrite \
        --state-in data/state.json --state-out data/state.json --stale-after-minutes 1500

A live run needs `--max-requests` and `--out` under `data/`, and either `GITHUB_TOKEN` or `--anonymous`. A token with
no permissions is enough for public data. Put it in the environment of the command and never in a file. The collector
refuses the placeholder example config. The live write path uses POSIX file operations, so it targets Linux and macOS.

`data/` is ignored by git. Run files are never committed.

| Exit code | Meaning |
| --- | --- |
| 0 | complete (for `export`: files written) |
| 1 | error (for `export`: an invalid run or state file) |
| 2 | refused |
| 3 | partial, for example stopped by the budget or a rate limit |

`config/sources.example.json` holds fictional placeholders only. The real roots and search queries are not committed
yet.

## Search terms and the relevance filter

Searching for game names also matches unrelated repositories. A search for a word like "killzone" returns trading
scripts, class projects and profile pages next to the few repositories about the games. Results from such terms need a
relevance filter before they go on the page. That filter is planned and not built, so only the PS3 recompilation
searches feed the page for now.

## Planned automation

Planned, not running. Each step is its own reviewed change.

1. Read the page's notice text from the feed.
2. A reviewed config with a relevance filter.
3. A scheduled workflow on GitHub runners that uses the built-in token.
4. Switch it on only after the maintainer approves.

## How AI was used

Most of the code, tests and documentation were written with AI agent assistance (Claude Code). Changes go through
review and CI before they are merged, and the page labels what the data is.

## Related projects

- [ps3recomp](https://github.com/sp00nznet/ps3recomp), the toolkit whose fork network the collector follows.

## Licence

[MIT](LICENSE). The banner and social preview are text-only renders set in IBM Plex Mono (SIL Open Font License 1.1).

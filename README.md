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
    python3 -B -m collector collect --config config/sources.json

    # live, read-only GET requests to api.github.com
    GITHUB_TOKEN=... python3 -B -m collector collect --config <your config> --live \
        --max-requests 300 --out data/run.json

    # the same, with the fetch order taken from the state the export keeps (see "Fetch order")
    GITHUB_TOKEN=... python3 -B -m collector.cli collect --config <your config> --live \
        --max-requests 300 --out data/run-003.json --previous-state data/state.json

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

`config/sources.example.json` holds fictional placeholders only. `config/sources.json` holds the reviewed roots and
search queries.

## Sources config

`config/sources.json` is the reviewed config the scheduled job uses. `config/sources.example.json` shows every key
with fictional placeholders only; a live run refuses any config that still uses one of them. Every key is checked
strictly (types, lengths, counts, no unknown keys, no control characters), and error messages name paths and counts,
never values from the file.

- `queries`: GitHub search queries, at most 50. An entry is a plain string, or `{"q": "<query>", "require_any":
  ["term", ...]}`: a result is then kept only when its `owner/name` or its description contains at least one term
  (plain substring, case-insensitive, name and description matched separately). Both come with the search result,
  so a result left out costs no request; it is not recorded in the run, and the number left out is the search
  entry's `filtered_out` and the run's `collection_report.filtered_out`. Terms: 1 to 50 per query, 1 to 64 printable
  characters each, single inner spaces only, no duplicates. Pick terms that name the game, not the word the query
  searches for (a `helghast` query must not use `helghast` as a term, or every result passes), and never list
  repository names as terms: use `include` for one repository.
  A code query is `{"kind": "code", "q": "<code search query>"}`, with an optional `require_any` matched the same way
  against the name and description of the repository holding the file. It asks GitHub's code search (`GET
  /search/code`) and finds repositories by a file they contain, for example one that vendors a licence file without
  naming the toolkit in its name or description. Its entry in the run says `"kind": "code"`; that records how the
  repository was found, not that it is a recompilation. Code search needs a token: with `--anonymous` code queries are
  left out of the plan (it says how many in `skipped_code_queries`) and not asked, and the run is incomplete (the
  search entry says `"skipped": "no_token"`, the report counts `skipped_searches`). Code search has its own rate limit
  (about 10 requests a minute), separate from the search limit, so either running out leaves the other kind going.
  Repository search items carry no README text: a query with `in:readme` cannot use `require_any` and stays a plain
  string.
- `roots`: `{"repository_id", "full_name"}` repositories whose fork network is walked.
- `include`: `{"repository_id", "full_name"}` repositories fetched even when no search or fork walk returns them.
  They cost the same four requests as any candidate (metadata by id, commits, releases, readme) and no lookup.
- `deny`: `{"repositories": ["owner/name", ...], "owners": ["login", ...]}`, matched case-insensitively against the
  name the listing gave, before anything is fetched. Denied ids are left out of the run's listings and counted in
  `collection_report.denied`. Roots and included repositories cannot be denied (the config is refused).
- `max_requests`, `max_repositories`, `commit_window_days`, `readme_bytes`, `page_caps` (`search`, `code` (1 to 3,
  default 1), `forks`, `commits` pages per listing): the budget. A repository costs about four requests; candidates are
  fetched in the fetch order below and those past `max_repositories` are reported as not fetched.

### Fetch order

When there are more candidates than `max_repositories` (or the budget stops the run), the order decides which are
left out. It is deterministic and needs no network:

1. Roots and `include` entries, ascending id. The config names them, so they always come first.
2. Candidates never attempted before, newest (highest) id first, so a newly found repository is fetched first.
3. Candidates attempted before, oldest last attempt first, ties by ascending id. A repository left out of one run
   is among the first of the next, so the cut rotates.

"Attempted before, and when" comes from `--previous-state <state .json under data/>`: the state `export --state-out`
keeps. Only its repositories' `last_attempted_refresh_at` values are read, and the file is never written. It follows
the `--state-in` path rules; a missing or unusable file is refused before any request (exit 2, or 1 for a file that
is not a valid state), and `--out` may not name it. Without the flag every candidate counts as new, so the order is
roots and `include`, then newest id first, and the same repositories are left out every run. The request plan, the
`--recorded` plan and the live summary carry `previous_state` (`known_repositories`, `last_run_at`) when the flag
is given. A daily job runs `collect --previous-state data/state.json`, then `export --state-in data/state.json
--state-out data/state.json --overwrite` with the new run.

A repository left out is simply absent from the run: the run is partial (`complete` false, `not_fetched` counts
it), the export keeps its previous values, and it is listed as not refreshed, not as failing. When more new
repositories turn up every run than there are free places, the lowest new ids wait until fewer new ones arrive.

GitHub answers 409 on the commits listing of an empty repository, but a 409 can also mean the repository is
temporarily unavailable. Such a repository is recorded with no commits and `commits_complete` false (zero is not
taken as certain), counted in `collection_report.commits_unavailable`, and does not make the run partial; any other
incomplete commits listing (a page cap, dropped items) still does.

### Reviewing a change to `config/sources.json`

1. Run `python3 -B -m collector collect --config config/sources.json` (no network): the config must load, and the
   plan lists one search per query (`/search/code` for a code query), one forks walk per root and one metadata
   request per root and include entry.
2. Check the budget: `max_requests` stays under the token's hourly limit with margin (the job's built-in token
   allows 1000 requests an hour, search 30 a minute, code search 10 a minute), and repository searches x
   `page_caps.search` + code searches x `page_caps.code` + roots x `page_caps.forks` + 4 x `max_repositories` fits in
   `max_requests`. For the committed config: 7 x 3 + 1 x 1 + 1 x 10 = 32, plus 4 x 190 = 760, is 792 of 800.
   The fetch order changes which candidates are fetched, not how many, and `--previous-state` costs no request.
   With 190 places, two runs reach every candidate while there are at most 380 (fewer by the roots and `include`
   entries, which take a place in every run).
3. For a new or changed `require_any`, list which repositories of a recent run file the terms keep and which they
   drop, and say why for every borderline case. Terms describe the game; repository names are not terms.
4. Update the expectations in `tests/test_sources_config.py` in the same change, and run the tests.

## Search terms and the relevance filter

Searching for game names also matches unrelated repositories. A search for a word like "killzone" returns trading
scripts, class projects and profile pages next to the few repositories about the games. A query can therefore list
`require_any` terms, and only results whose name or description contains one of them are kept. The rules are in
"Sources config" above.

## Planned automation

Planned, not running. Each step is its own reviewed change.

1. Read the page's notice text from the feed.
2. A scheduled workflow on GitHub runners that uses the built-in token.
3. Switch it on only after the maintainer approves.

## How AI was used

Most of the code, tests and documentation were written with AI agent assistance (Claude Code). Changes go through
review and CI before they are merged, and the page labels what the data is.

## Related projects

- [ps3recomp](https://github.com/sp00nznet/ps3recomp), the toolkit whose fork network the collector follows.

## Licence

[MIT](LICENSE). The banner and social preview are text-only renders set in IBM Plex Mono (SIL Open Font License 1.1).

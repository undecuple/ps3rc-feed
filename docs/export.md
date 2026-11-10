# Export details

`python3 -m collector export` turns run files into the two files the page in `site/` reads.

- Inputs and `--out-dir` are under `data/`, which git ignores. Existing outputs are
  replaced only with `--overwrite`.
- Run files go through the same event model as the dry run, in `run_at` order. Two run files with the same `run_at`
  are refused, so the same run files in any order give the same bytes.
- `feed.json` holds `generated_at` (the newest run's `run_at`), `repos` and `events`. Text is plain, because the page
  sets it with `textContent`. The only links are `https://github.com/owner/name` repository URLs. Unknown values are
  `null`. The newest 500 events and at most 2000 repositories are kept.
- The feed's `collection` object describes the newest run: `complete`, `stopped`, `not_fetched`, `error_count`,
  `last_run_at`. `--stale-after-minutes N` adds `stale_after_minutes` for the page's stale check.
- `manifest.json` holds the feed's name, `generated_at` and a `version`, the first 16 hex digits of the feed's SHA-256.
  The page polls the manifest and reloads the feed when the version changes.
- `--state-out` saves the store after the runs are applied. `--state-in` starts the next export from it, so a daily job
  applies one new run on top of the previous state. A saved state plus the next run gives the same feed bytes as both
  runs from scratch. A run that is not newer than the state's last run is refused.
- Every output is built before the first write. The state is written
  first, then `feed.json`, then `manifest.json`. If a later write fails, the page keeps the previous feed, the next
  export from the saved state publishes this run, and repeating this run is refused instead of being applied twice.
- `--state-in` and `--state-out` may name the same file only with `--overwrite`.

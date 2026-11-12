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
- `--config config/sources.json` removes every repository its `deny` list names (roots and `include` ids excepted)
  from the state, with its events and its ids in the stored search and fork listings, before and after the runs are
  applied. A tracked fork of a denied repository stays (deny names repositories and owners, not networks): it keeps
  the parent id GitHub reports, its feed `parent` becomes `null`, and a game or toolkit inherited from the denied
  repository is dropped. Without `--config` nothing is removed; the refresh workflow always passes it.
- Every repository record carries `game` (the title), `game_key`, `game_basis`, `game_confidence` and
  `matched_alias`. The basis is `alias_name`, `alias_description` or `alias_readme_title` for a match in
  `config/games.json` (confidence `strong`, `strong`, `medium`), `readme_title` for an unreviewed README heading
  (confidence `low`), or `inherited_from_parent` / `inherited_unverified_copy` with the ancestor's confidence and alias.
  The history snapshot lines do not carry these fields.
- The feed bytes, and so the manifest `version`, depend on `config/games.json`: a change to the list changes the labels
  in the next export. The state records the list's fingerprint (`games_list`). A run applied to a state without one
  (made before the list existed) re-labels silently; afterwards `game_association_changed` is emitted only when a
  repository's game key changes, never for a move onto an alias match or a new spelling of the same key.
- Every repository record carries `removed`: true when GitHub reported the repository as not found (404 or 410) for as
  many runs in a row as the stale threshold (3). The page shows status `removed` for it, ahead of `archived` and
  `stale`. It is not proof: a deleted repository, a deleted account and a repository made private look the same, so the
  stale event says "no longer found on GitHub ... (removed or made private; automated, unverified)". The collector
  keeps asking about it every run, one request, so a repository that comes back is picked up and `removed` goes false.
  A feed made before this field has no `removed` and its failing repositories show as `stale`.
  The page labels the stale notice of a removed repository `REMOVED` (its type stays `collection_stale`, so `type:stale` still finds it).

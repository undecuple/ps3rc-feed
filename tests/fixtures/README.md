# Test fixtures

Every repository, owner, id, sha and date here is fictional. No file is real GitHub data and none contains a link to a real project. Each folder is one scenario: numbered run files are fed to the collector in order, each describing what the (fake) API returned at `run_at`.

A repository is `fetch_state: ok`, or a failure state (`rate_limited`, `not_found`) with its metrics set to `null`. `commits_complete: false` means pagination stopped early. A root's `forks_scan_complete: false` means its fork network was not fully listed.

| Scenario | What it exercises |
| --- | --- |
| baseline_unchanged | first run is a baseline, identical re-run is silent |
| new_activity | new commits, release, archived transition, new repository |
| batch_boundaries | same commits seen in different batches, overlap window |
| rename | same repository id, new name and owner |
| history_rewrite | tip replaced, old commits unreachable |
| failures | partial pagination, rate limit, not found, stale debounce and recovery, truncated search |
| forks_two_roots | one fork found via two roots, nested fork, incomplete scan, fork keeping no upstream name or README |
| classification_change | README retitle changes the inferred game |
| hostile_text | HTML, mentions, shell text, javascript: links, XML breakout, bidi, oversized and control characters |
| github_recorded | canned API answers (not run files) for the recorded transport: one search, one root, two repositories |

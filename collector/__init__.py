"""ps3rc-feed collector: run files, observations, events, catalog, and a read-only GitHub client.

Standard library only. `schema`, `store`, `events`, `forks`, `games`, `catalog`,
`export`, `history` and `text` never touch the network: they turn run files
(what a fetch returned) into persisted observations, canonical events, a
catalog, the static site's feed.json and manifest.json, and the feed history. `github` holds the
only code that can open a connection (its
LiveTransport, HTTPS GET to api.github.com), `collect` drives it into the
run format, and `cli` constructs the live transport only when asked to
with --live; every other path, including every test, uses recorded
answers.

The package asks the interpreter not to write bytecode caches, so a
dry run leaves no files behind (run with `python3 -B` to cover this
module's own cache as well).
"""

import sys

sys.dont_write_bytecode = True

__all__ = ["catalog", "cli", "collect", "events", "export", "forks", "games", "github", "history", "model", "paths", "schema", "store", "text"]

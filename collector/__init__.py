"""ps3rc-feed collector: run files, observations, events, catalog, and a read-only GitHub client.

Standard library only. `schema`, `store`, `events`, `forks`, `catalog`
and `text` never touch the network: they turn run files (what a fetch
returned) into persisted observations, canonical events and a catalog.
`github` holds the only code that can open a connection (its
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

__all__ = ["catalog", "cli", "collect", "events", "forks", "github", "model", "paths", "schema", "store", "text"]

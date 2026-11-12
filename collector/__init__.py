"""ps3rc-feed collector: run files, observations, events, catalog, and a read-only GitHub client.

Standard library only. `schema`, `store`, `events`, `forks`, `games`, `catalog`,
`export`, `history`, `discord` (which messages a feed would send; it sends nothing) and `text` never touch the network: they turn run files
(what a fetch returned) into persisted observations, canonical events, a
catalog, the static site's feed.json and manifest.json, and the feed history. `github` and
`discord_send` hold the only code that can open a connection: the former's
LiveTransport (HTTPS GET to api.github.com), the latter's WebhookSender (HTTPS POST
to one Discord webhook). `collect` drives the first into the
run format, and `cli` constructs the live transport only when asked to
with --live and the webhook sender only with `discord --send`; every other
path, including every test, uses recorded answers.

The package asks the interpreter not to write bytecode caches, so a
dry run leaves no files behind (run with `python3 -B` to cover this
module's own cache as well).
"""

import sys

sys.dont_write_bytecode = True

__all__ = ["catalog", "cli", "collect", "discord", "discord_send", "events", "export", "forks", "games", "github", "history", "model", "paths", "schema", "store", "text"]

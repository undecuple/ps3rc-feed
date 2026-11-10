"""ps3rc-feed collector core (step 1).

Standard library only. Nothing in this package talks to the network: input
is a run file describing what a fetch returned, output is persisted
observations, a canonical event list and a catalog view.

The package asks the interpreter not to write bytecode caches, so a
dry run leaves no files behind (run with `python3 -B` to cover this
module's own cache as well).
"""

import sys

sys.dont_write_bytecode = True

__all__ = ["schema", "text", "model", "store", "events", "catalog", "cli"]

"""ps3rc-feed collector core (step 1).

Standard library only. Nothing in this package talks to the network: input
is a run file describing what a fetch returned, output is persisted
observations, a canonical event list and a catalog view.
"""

__all__ = ["schema", "text", "model", "store", "events", "catalog", "cli"]

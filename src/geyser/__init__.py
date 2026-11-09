"""Geyser: agents that self-organize around tasks, reviews and a shared Library.

DESIGN.md describes the behavior this package implements. Package map:

- ``geyser.core``: the ledger and the deterministic state machines (tasks, teams,
  agents, waits, candidates, completion, messages, controls).
- ``geyser.content``: native files, snapshots, the Git Library and workspaces.
- ``geyser.integration``: direct and batch integration and publication.
- ``geyser.gateway``: the two model-facing tools and the operation catalog.
- ``geyser.runtime``: activations, model inputs, rotation and backends.
- ``geyser.host``: process ownership (systemd scopes, or a fake for tests).
- ``geyser.scheduler``: admission, assignment decisions, validation and recovery.
- ``geyser.domains``: the domain pack boundary and the built-in general pack.
- ``geyser.api`` and ``geyser.cli``: the operator API, dashboard and CLI.
"""

__version__ = "0.1.0"

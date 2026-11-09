"""The ledger and the deterministic state machines.

Modules here change state only inside a ledger transaction, take the clock from
their context, and touch the file system only through ``geyser.content`` helpers
for small authoritative files (task contracts, message bodies).
"""

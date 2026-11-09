"""Domain packs: the extension boundary (DESIGN.md section 17.1)."""

from __future__ import annotations

from importlib.metadata import entry_points

from geyser.domains.base import DomainPack
from geyser.errors import InvalidArgument


def load_pack(name: str) -> DomainPack:
    """Load the selected pack. ``general`` is built in; others come from entry points."""
    if name == "general":
        from geyser.domains.general import GeneralPack

        return GeneralPack()
    for entry in entry_points(group="geyser.domains"):
        if entry.name == name:
            factory = entry.load()
            pack = factory()
            if not isinstance(pack, DomainPack):
                raise InvalidArgument(f"Domain pack {name!r} does not implement DomainPack.")
            return pack
    raise InvalidArgument(f"No domain pack named {name!r} is installed.")

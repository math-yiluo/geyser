"""Standing instructions and on-demand guides (DESIGN.md section 7.3).

Standing instructions are identical for every agent of a role: the shared rules, the
required guides' text and a one-line-per-operation cheat sheet generated from the
operation schemas. They contain no identity, path or task, so they form a shared
cached prefix. Each conversation records their digest; a changed digest moves the
conversation to a new one at its next activation.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from geyser.core.actors import Capability
from geyser.jsonio import atomic_write_text, sha256_text

if TYPE_CHECKING:
    from geyser.gateway import Gateway
    from geyser.hub import Hub

ROLE_GUIDES = {"worker": "worker.md", "reviewer": "reviewer.md", "integrator": "integrator.md"}
ROLE_CAPABILITY = {"worker": Capability.WORKER, "reviewer": Capability.REVIEWER,
                   "integrator": Capability.INTEGRATOR}


def _core_guide(name: str) -> str:
    return resources.files("geyser.guides").joinpath(name).read_text(encoding="utf-8")


def standing_instructions(hub: Hub, gateway: Gateway, role: str) -> str:
    """The shared prefix of every conversation of ``role``."""
    prefix = hub.config.gateway.prefix
    parts = [_core_guide(ROLE_GUIDES[role]).strip()]
    for name, text in hub.pack.guides().required.get(role, ()):
        parts.append(f"<!-- guide: {name} -->\n{text.strip()}")
    sheet = gateway.cheat_sheet(ROLE_CAPABILITY[role])
    parts.append(
        "# Hub operations\n\n"
        f"Call them with `{prefix}_call(operation, arguments)`; `{prefix}_actions` lists "
        "them or shows one operation's full schema. Operations you cannot use in a given "
        "turn are refused with a reason.\n\n" + sheet
    )
    return "\n\n".join(parts) + "\n"


def digest(text: str) -> str:
    return sha256_text(text)[:16]


def install_on_demand(hub: Hub, agent_id: str, role: str) -> list[str]:
    """Copy the role's on-demand guides into ``.private/instructions/``; return their paths."""
    directory = hub.layout.instructions_dir(agent_id)
    installed = []
    for name, text in hub.pack.guides().on_demand.get(role, ()):
        path = directory / f"{name}.md"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            atomic_write_text(path, text)
        installed.append(str(path))
    return installed


def guide_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.md"

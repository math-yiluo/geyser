"""Process ownership: every activation runs in an owned scope (DESIGN.md section 17.3)."""

from __future__ import annotations

import sys

from geyser.host.base import ProcessHost


def default_host(kind: str = "auto") -> ProcessHost:
    """``linux_systemd`` on Linux, the in-memory fake elsewhere (tests and dry runs)."""
    if kind == "fake" or (kind == "auto" and not sys.platform.startswith("linux")):
        from geyser.host.fake import FakeHost

        return FakeHost()
    from geyser.host.systemd import SystemdHost

    return SystemdHost()

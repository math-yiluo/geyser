"""Building the configured backends for a running service."""

from __future__ import annotations

import logging
import shutil

from geyser.config import ProjectConfig
from geyser.runtime.backends.base import Backend

log = logging.getLogger(__name__)


def build_backends(config: ProjectConfig) -> dict[str, Backend]:
    """Every backend this machine can run. A missing one is logged, never fatal: agents of
    that backend fail with backoff and an operator notice until it becomes available."""
    backends: dict[str, Backend] = {}
    from geyser.runtime.backends.codex import CodexBackend

    executable = (config.codex.command or ["codex"])[0]
    if shutil.which(executable):
        backends["codex"] = CodexBackend(config.codex)
    else:
        log.warning("codex backend unavailable: %s not found", executable)
    from geyser.runtime.backends.claude import ClaudeBackend, claude_unavailable

    reason = claude_unavailable(config.claude)
    if reason is None:
        backends["claude"] = ClaudeBackend(config.claude)
    else:
        log.info("claude backend unavailable: %s", reason)
    return backends

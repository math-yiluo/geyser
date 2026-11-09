"""The Claude Code backend: the Codex protocol over ``claude_bridge`` (ported from FormalGeyser).

Claude Code runs through the Agent SDK in a bridge process that speaks the
app-server subset the Codex backend drives, so turns, tool replies, interruption
and shutdown are shared. Claude Code has no steering: messages arrive at the next
turn or through ``sync``. Its compaction summarizes inputs, so the next tool result
after a compaction carries the current task view again.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

from geyser.config import BackendConfig
from geyser.runtime.backends.base import BackendCapabilities
from geyser.runtime.backends.claude_bridge import subscription_environment
from geyser.runtime.backends.codex import CodexBackend

HOLD_CODES = {"claudeUsageLimit": "Claude subscription limit reached",
              "claudeSignInRequired": "Claude sign-in required",
              "claudeSubscriptionRequired": "Claude is not signed in with a subscription"}


def claude_unavailable(settings: BackendConfig) -> str | None:
    """Why Claude workers cannot start on this machine, or None."""
    if not sys.platform.startswith("linux"):
        return "Claude workers run on Linux, where Claude Code's command sandbox exists."
    missing = [name for name in ("bwrap", "socat") if shutil.which(name) is None]
    if missing:
        return "Install " + " and ".join(missing) + ": Claude Code's command sandbox needs them."
    if importlib.util.find_spec("claude_agent_sdk") is None:
        return "Install the claude extra (claude-agent-sdk) before selecting Claude workers."
    if not settings.home:
        return "Configure claude.home: the directory that holds the subscription sign-in."
    return None


class ClaudeBackend(CodexBackend):
    name = "claude"
    capabilities = BackendCapabilities(steering=False, compaction_keeps_inputs=False)
    usage_limit_code = "claudeUsageLimit"

    def homes(self) -> list[Path]:
        configured = [self.settings.home, *self.settings.profiles]
        homes = [Path(value).expanduser().resolve() for value in configured if value]
        return homes or [Path.home() / ".claude"]

    def argv(self, protected: list[str], network: bool, sqlite_home: Path | None) -> list[str]:
        return [sys.executable, "-m", "geyser.runtime.backends.claude_bridge"]

    def decision_argv(self) -> list[str]:
        return [sys.executable, "-m", "geyser.runtime.backends.claude_bridge"]

    def environment(self, home: Path) -> dict[str, str]:
        environment = subscription_environment(dict(os.environ))
        environment["CLAUDE_CONFIG_DIR"] = str(home)
        environment.setdefault("PYTHONUTF8", "1")
        return environment

    def thread_extras(self, protected: list[str], network: bool, temporary: Path,
                      prefix: str) -> dict[str, object]:
        extras: dict[str, object] = {"denyRead": protected, "network": network,
                                     "tmpdir": str(temporary), "geyserPrefix": prefix}
        if self.settings.command:
            extras["cliPath"] = self.settings.command[0]
        return extras

    def hold_codes(self) -> dict[str, str]:
        return HOLD_CODES

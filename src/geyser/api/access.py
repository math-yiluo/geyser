"""Operator access (DESIGN.md section 15.2).

Every operator request carries the project's operator token, kept in the user's
runtime directory, readable only by the user and denied to every sandbox. Local
clients send it as a bearer credential; a browser signs in once through the link
``geyser dashboard`` prints and keeps it in a strict HTTP-only cookie. Requests must
name a loopback host and, from a browser, come from the service's own origin.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
COOKIE = "geyser_session"


def runtime_dir() -> Path:
    """The user's private runtime directory for Geyser."""
    if sys.platform.startswith("linux") and os.environ.get("XDG_RUNTIME_DIR"):
        base = Path(os.environ["XDG_RUNTIME_DIR"])
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "state"
    directory = base / "geyser"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        directory.chmod(stat.S_IRWXU)
    return directory


def project_key(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


def token_path(root: Path) -> Path:
    return runtime_dir() / f"{project_key(root)}.token"


def endpoint_path(root: Path) -> Path:
    return runtime_dir() / f"{project_key(root)}.endpoint.json"


def operator_token(root: Path, *, create: bool = True) -> str | None:
    path = token_path(root)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    if not create:
        return None
    token = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(token)
    return token


def write_endpoint(root: Path, port: int) -> None:
    path = endpoint_path(root)
    path.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def read_endpoint(root: Path) -> dict | None:
    path = endpoint_path(root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_endpoint(root: Path) -> None:
    endpoint_path(root).unlink(missing_ok=True)


def host_is_loopback(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else \
        host_header.split("]")[0] + "]"
    return host in LOOPBACK_HOSTS

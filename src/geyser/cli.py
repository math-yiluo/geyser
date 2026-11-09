"""The ``geyser`` command line: setup, inspection, run control and maintenance.

Commands talk to the running service over its loopback API when one is serving the
project, and operate on the project directly otherwise.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from geyser.api import access
from geyser.config import ProjectConfig
from geyser.errors import GeyserError

app = typer.Typer(help="Geyser: agents that self-organize around tasks and a shared Library.",
                  no_args_is_help=True)
console = Console()

ROOT = typer.Option(Path("."), "--project", "-p", help="The project directory.")


# --------------------------------------------------------------------- clients
class LocalClient:
    """Operates on the project directly (no service is running)."""

    def __init__(self, root: Path):
        from geyser.hub import Hub

        self.hub = Hub(root)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        from geyser import operator

        parts = path.strip("/").split("/")[1:]
        hub = self.hub
        if method == "GET":
            if parts == ["status"]:
                return operator.status(hub)
            if parts == ["agents"]:
                return operator.agent_list(hub)
            if parts == ["tasks"]:
                return operator.task_list(hub)
            if parts == ["requests"]:
                return operator.requests_open(hub)
            if parts[:1] == ["activity"]:
                return operator.activity(hub)
            if parts == ["choices"]:
                return operator.choice_overview(hub)
        else:
            body = body or {}
            if parts == ["setup"]:
                return operator.setup_project(hub, **body)
            if parts[:1] == ["run"]:
                return operator.run_action(hub, parts[1].replace("-", "_"))
            if parts == ["choices"]:
                return operator.choose(hub, body.pop("role"), **body)
            if parts[:1] == ["agents"] and parts[2:] == ["move"]:
                return operator.move_agent(hub, parts[1], body["backend"])
            if parts[:1] == ["agents"]:
                return operator.agent_action(hub, parts[1], parts[2].replace("-", "_"))
            if parts[:1] == ["tasks"] and parts[2:] == ["invite-back"]:
                return operator.invite_back(hub, parts[1], body["agent"], body["reason"])
            if parts[:1] == ["groups"]:
                return operator.group_action(hub, parts[1], parts[2].replace("-", "_"))
            if parts == ["messages"]:
                return operator.send_message(hub, body["to"], body["text"])
            if parts[:1] == ["requests"]:
                return operator.answer_request(hub, parts[1], body["text"])
            if parts == ["backend"]:
                return operator.set_new_backend(hub, body["backend"])
        raise typer.BadParameter(f"unsupported offline request {method} {path}")


class HttpClient:
    def __init__(self, root: Path, port: int):
        token = access.operator_token(root, create=False)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}",
                                   headers={"Authorization": f"Bearer {token}"}, timeout=60)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        response = self.client.request(method, path, json=body if method == "POST" else None)
        data = response.json()
        if response.status_code >= 400:
            raise typer.BadParameter(data.get("message") or data.get("detail") or str(data))
        return data


def client_for(root: Path) -> LocalClient | HttpClient:
    root = root.resolve()
    endpoint = access.read_endpoint(root)
    if endpoint:
        try:
            client = HttpClient(root, endpoint["port"])
            client.call("GET", "/v1/status")
            return client
        except (httpx.HTTPError, typer.BadParameter, OSError):
            access.clear_endpoint(root)
    return LocalClient(root)


def show(value: Any) -> None:
    console.print_json(json.dumps(value, ensure_ascii=False, default=str))


def guarded(function):
    """Print Geyser refusals as one line instead of a traceback."""

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except GeyserError as error:
            console.print(f"[red]{error.code}[/red]: {error.message}")
            raise typer.Exit(1) from error

    return wrapper


# ------------------------------------------------------------------- commands
@app.command()
@guarded
def init(root: Path = typer.Argument(Path("."), help="The new project directory."),
         name: str = typer.Option("Geyser project", help="The project's name."),
         domain: str = typer.Option("general", help="The domain pack."),
         backend: str = typer.Option("codex", help="Backend for new agents."),
         import_from: Path | None = typer.Option(None, "--import",
                                                 help="Import this tree as the baseline.")):
    """Create a project: configuration, directories and the Library."""
    from geyser.hub import Hub

    config = ProjectConfig(name=name, domain=domain)
    config.agents.new_backend = backend
    for role in (config.reviewers, config.integrator, config.decisions, config.waker):
        role.backend = backend
    hub = Hub.initialize(root, config, import_from=import_from)
    console.print(f"Initialized [bold]{name}[/bold] in {hub.layout.root}")
    console.print("Next: `geyser setup --title ... --goal ... --acceptance ...`; choose every "
                  "role's model and effort and the answer deadline (`geyser choose` lists "
                  "them); then `geyser serve` and `geyser run start`.")


@app.command()
@guarded
def setup(title: str = typer.Option(..., help="Task 0's title."),
          goal: str = typer.Option(..., help="The goal, including what counts as completion."),
          acceptance: list[str] = typer.Option(..., help="An acceptance condition (repeat)."),
          constraint: list[str] = typer.Option([], help="A constraint (repeat)."),
          root: Path = ROOT):
    """Create Task 0 from the operator's goal."""
    show(client_for(root).call("POST", "/v1/setup", {
        "title": title, "goal": goal, "acceptance": acceptance, "constraints": constraint}))


@app.command()
@guarded
def status(root: Path = ROOT):
    """What the project is doing."""
    show(client_for(root).call("GET", "/v1/status"))


@app.command()
@guarded
def agents(root: Path = ROOT):
    """List agents."""
    table = Table("Agent", "Backend", "State", "Work", "Control", "Failures")
    for agent in client_for(root).call("GET", "/v1/agents"):
        if agent["state"] == "retired":
            continue
        work = agent["current_task"] or agent["current_review"] or agent["current_batch"] or ""
        table.add_row(agent["id"], agent["backend"], agent["state"] + (" (turn)" if agent[
            "live"] else ""), work, agent["control"], str(agent["failures"]))
    console.print(table)


@app.command()
@guarded
def tasks(root: Path = ROOT):
    """List tasks, Task 0 first."""
    table = Table("Task", "State", "Revision", "Executors")
    for task in client_for(root).call("GET", "/v1/tasks"):
        table.add_row(task["task"], task["state"], str(task["revision"]),
                      ", ".join(task["executors"]))
    console.print(table)


@app.command()
@guarded
def run(action: str = typer.Argument(..., help="start | pause | pause-after-turn | resume | "
                                     "stop"),
        root: Path = ROOT):
    """Control the run. Live model work starts only with `run start`."""
    show(client_for(root).call("POST", f"/v1/run/{action}", {}))


@app.command()
@guarded
def agent(action: str = typer.Argument(..., help="pause | pause-after-turn | resume"),
          agent_id: str = typer.Argument(...), root: Path = ROOT):
    """Control one agent while the run continues."""
    show(client_for(root).call("POST", f"/v1/agents/{agent_id}/{action}", {}))


@app.command()
@guarded
def move(agent_id: str = typer.Argument(..., help="A worker."),
         backend_name: str = typer.Argument(..., metavar="BACKEND"), root: Path = ROOT):
    """Move a worker to another backend; it continues in a new conversation there."""
    show(client_for(root).call("POST", f"/v1/agents/{agent_id}/move",
                               {"backend": backend_name}))


@app.command()
@guarded
def group(action: str = typer.Argument(..., help="pause-after-turn | resume"),
          name: str = typer.Argument(..., help="<backend>, <backend>.workers, "
                                     "<backend>.reviewers or integrator"),
          root: Path = ROOT):
    """Control a backend or role group, or the Integrator's."""
    show(client_for(root).call("POST", f"/v1/groups/{name}/{action}", {}))


@app.command()
@guarded
def choose(role: str | None = typer.Argument(None, help="workers.<backend> | reviewers | "
                                      "integrator | dispatcher | waker | answer_deadline; "
                                      "omit to list the choices"),
           model: str | None = typer.Option(None, help="The role's model."),
           effort: str | None = typer.Option(None, help="The role's effort."),
           backend_name: str | None = typer.Option(None, "--backend",
                                                   help="A non-worker role's backend."),
           minutes: float | None = typer.Option(None, help="The answer deadline."),
           root: Path = ROOT):
    """Choose a role's model and effort, or the answer deadline, before a run."""
    client = client_for(root)
    if role is None:
        show(client.call("GET", "/v1/choices"))
        return
    body: dict[str, Any] = {"role": role}
    for key, value in (("model", model), ("effort", effort), ("backend", backend_name),
                       ("minutes", minutes)):
        if value is not None:
            body[key] = value
    show(client.call("POST", "/v1/choices", body))


@app.command("invite-back")
@guarded
def invite_back(task_id: str, worker_id: str, reason: str, root: Path = ROOT):
    """Bring back a worker that left a task."""
    show(client_for(root).call("POST", f"/v1/tasks/{task_id}/invite-back",
                               {"agent": worker_id, "reason": reason}))


@app.command()
@guarded
def message(to: str, text: str, root: Path = ROOT):
    """Send a directed message with human authority to an agent."""
    show(client_for(root).call("POST", "/v1/messages", {"to": to, "text": text}))


@app.command()
@guarded
def answer(request_id: str, text: str, root: Path = ROOT):
    """Answer an agent's request."""
    show(client_for(root).call("POST", f"/v1/requests/{request_id}/answer", {"text": text}))


@app.command()
@guarded
def backend(name: str, root: Path = ROOT):
    """Choose the backend for agents created next."""
    show(client_for(root).call("POST", "/v1/backend", {"backend": name}))


@app.command()
def dashboard(root: Path = ROOT):
    """Print the dashboard's sign-in link."""
    root = root.resolve()
    endpoint = access.read_endpoint(root)
    if not endpoint:
        console.print("No service is serving this project; run `geyser serve` first.")
        raise typer.Exit(1)
    token = access.operator_token(root, create=False)
    console.print(f"http://127.0.0.1:{endpoint['port']}/login?token={token}")


@app.command()
def serve(root: Path = ROOT,
          port: int = typer.Option(8765, help="Loopback port of the operator API."),
          no_resume: bool = typer.Option(False, "--no-resume",
                                         help="Start for inspection only; drive nothing."),
          verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Serve the project: the Hub, the scheduler, the runtime and the operator API."""
    import portalocker
    import uvicorn

    from geyser.api.app import create_app
    from geyser.hub import Hub
    from geyser.runtime.backends.factory import build_backends
    from geyser.service import GeyserService

    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = root.resolve()
    hub = Hub(root)
    lock_file = hub.layout.lock.open("a")
    try:
        portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except portalocker.LockException:
        console.print("[red]Another Geyser service is already serving this project.[/red]")
        raise typer.Exit(1) from None
    backends = build_backends(hub.config)
    service = GeyserService(hub, backends)
    token = access.operator_token(root)
    assert token is not None
    application = create_app(service, token)

    async def main() -> None:
        if no_resume:
            console.print("Inspection only: nothing is driven.")
        else:
            await service.start()
        server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                               log_level="warning"))
        access.write_endpoint(root, port)
        console.print(f"Serving {hub.config.name} on http://127.0.0.1:{port} "
                      f"(backends: {', '.join(backends) or 'none'}). "
                      "Run `geyser dashboard` for the sign-in link.")
        try:
            await server.serve()
        finally:
            access.clear_endpoint(root)
            await service.stop(interrupt=False)

    try:
        asyncio.run(main())
    finally:
        portalocker.unlock(lock_file)
        lock_file.close()
        hub.close()


@app.command()
def doctor(root: Path = ROOT):
    """Check the toolchain, the backends and the process host."""
    from geyser.config import load_config

    config = load_config(root.resolve() / "geyser.toml")
    checks: list[tuple[str, bool, str]] = []
    git = shutil.which("git")
    version = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout \
        if git else ""
    checks.append(("git", bool(git), version.strip() or "not found"))
    for name in ("codex", "claude"):
        settings = config.backend(name)
        executable = (settings.command or [name])[0]
        found = shutil.which(executable)
        checks.append((f"backend {name}", bool(found), found or f"{executable} not found"))
    linux = sys.platform.startswith("linux")
    if linux:
        result = subprocess.run(["systemctl", "--user", "is-system-running"],
                                capture_output=True, text=True)
        checks.append(("systemd user manager", result.returncode in (0, 1),
                       result.stdout.strip() or result.stderr.strip()))
    else:
        checks.append(("process host", False, "live runs need a Linux host (WSL 2 on "
                       "Windows); the core and tests run here"))
    table = Table("Check", "OK", "Detail")
    for name, ok, detail in checks:
        table.add_row(name, "yes" if ok else "no", detail)
    console.print(table)


def main() -> None:  # pragma: no cover - console entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    main()

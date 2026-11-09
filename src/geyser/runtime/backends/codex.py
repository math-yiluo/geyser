"""The Codex backend: drives ``codex app-server`` over JSON-RPC (ported from FormalGeyser).

Each activation launches one app-server in the activation's owned scope, opens the
agent's thread (``thread/start`` for a new conversation, ``thread/resume``
otherwise; threads are never forked), runs one turn, and tears the process tree
down. Gateway tools are Codex dynamic tools: ``item/tool/call`` requests are
answered by the Hub in-process. Any other server request, such as an approval, is
refused: nothing is approved implicitly.

Workers run under a Codex permission profile: read everything, write only the
workspace, ``/tmp`` read-only, protected paths denied, network as configured.
Decisions run read-only, with no tools, over an empty directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from geyser.config import BackendConfig
from geyser.runtime.backends.base import (
    BackendCapabilities,
    DecisionOutcome,
    DecisionRequest,
    SandboxReport,
    TurnControl,
    TurnOutcome,
    TurnRequest,
    TurnStatus,
    Usage,
)
from geyser.runtime.backends.jsonrpc import (
    JsonRpcPeer,
    RpcClosed,
    RpcProtocolError,
    RpcRemoteError,
    RpcReply,
)

log = logging.getLogger(__name__)

WORKER_PROFILE = "geyser-worker"
DISABLED_FEATURES = ("multi_agent", "goals", "apps", "plugins", "hooks", "computer_use",
                     "in_app_browser", "image_generation")
ENABLED_FEATURES = ("skip_host_skill_discovery",)
TOOL_EXECUTION = {
    "features.code_mode.direct_only_tool_namespaces": ["functions"],
    "features.code_mode.default_exec_yield_time_ms": 60000,
}
HOLD_CODES = {"usageLimitExceeded": "Codex usage limit reached",
              "unauthorized": "Codex sign-in required"}
STOP_GRACE_SECONDS = 10.0
DRAIN_GRACE_SECONDS = 60.0
STREAM_LIMIT = 16 * 1024 * 1024


def tool_reply(result: dict[str, Any]) -> dict[str, Any]:
    return {"contentItems": [{"type": "inputText", "text": json.dumps(
        result, ensure_ascii=False, separators=(",", ":"), default=str)}],
            "success": "error" not in result}


def error_code(error: Any) -> str | None:
    """The machine code of a native error: ``code``, ``type`` or ``codexErrorInfo``."""
    if isinstance(error, dict):
        nested = error.get("error")
        if isinstance(nested, dict):
            error = {**error, **nested}
        code = error.get("code", error.get("type", error.get("codexErrorInfo")))
        if isinstance(code, dict):
            code = next(iter(code), None)
        return code if isinstance(code, str) else None
    return None


def error_message(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message") or (error.get("error") or {}).get("message")
        if isinstance(message, str):
            return message[:2000]
    return str(error)[:2000]


def worker_profile(protected: list[str], network: bool) -> list[str]:
    """The ``-c`` overrides that define the worker permission profile."""
    access = {":slash_tmp": "read", **dict.fromkeys(protected, "deny")}
    table = "{" + ", ".join(f"{json.dumps(path)} = {json.dumps(mode)}"
                            for path, mode in access.items()) + "}"
    name = "permissions." + WORKER_PROFILE
    return ["-c", name + '.extends=":workspace"', "-c", name + ".filesystem=" + table,
            "-c", name + ".network.enabled=" + ("true" if network else "false")]


class CodexBackend:
    name = "codex"
    capabilities = BackendCapabilities(steering=True, compaction_keeps_inputs=True)
    usage_limit_code = "usageLimitExceeded"

    def __init__(self, settings: BackendConfig):
        self.settings = settings
        self._exhausted: set[str] = set()

    def hold_codes(self) -> dict[str, str]:
        """Native error codes that hold the account until the operator resumes."""
        return HOLD_CODES

    def thread_extras(self, protected: list[str], network: bool, temporary: Path,
                      prefix: str) -> dict[str, object]:
        """Backend-specific thread parameters (the Claude bridge reads some)."""
        return {}

    # ------------------------------------------------------------------- homes
    def homes(self) -> list[Path]:
        configured = [self.settings.home, *self.settings.profiles]
        homes = [Path(value).expanduser().resolve() for value in configured if value]
        return homes or [Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")]

    def current_home(self) -> Path:
        for home in self.homes():
            if str(home) not in self._exhausted:
                return home
        self._exhausted.clear()
        return self.homes()[0]

    def protected_reads(self, extra: list[Path]) -> list[str]:
        """What sandboxed commands may not read: Hub state, runtime dir, every sign-in."""
        candidates = list(extra)
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            candidates.append(Path(runtime))
        candidates += [home / "auth.json" for home in self.homes()]
        candidates += [Path.home() / ".claude" / ".credentials.json"]
        return list(dict.fromkeys(str(path.resolve()) for path in candidates if path.exists()))

    def argv(self, protected: list[str], network: bool, sqlite_home: Path | None) -> list[str]:
        executable = (self.settings.command or ["codex"])
        arguments = [*executable]
        if sqlite_home is not None:
            arguments += ["-c", "sqlite_home=" + json.dumps(str(sqlite_home))]
        for key, value in TOOL_EXECUTION.items():
            arguments += ["-c", key + "=" + json.dumps(value, separators=(",", ":"))]
        arguments += ["-c", 'default_permissions=":read-only"', *worker_profile(protected,
                                                                                network)]
        for feature in DISABLED_FEATURES:
            arguments += ["--disable", feature]
        for feature in ENABLED_FEATURES:
            arguments += ["--enable", feature]
        return [*arguments, "app-server"]

    def decision_argv(self) -> list[str]:
        arguments = [*(self.settings.command or ["codex"])]
        for feature in DISABLED_FEATURES:
            arguments += ["--disable", feature]
        return [*arguments, "-c", 'default_permissions=":read-only"', "app-server"]

    def environment(self, home: Path) -> dict[str, str]:
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(home)
        if self.settings.home:
            environment["CODEX_SQLITE_HOME"] = str(Path(self.settings.home).expanduser()
                                                   .resolve())
        environment.setdefault("PYTHONUTF8", "1")
        return environment

    # -------------------------------------------------------------------- turns
    async def run_turn(self, request: TurnRequest) -> TurnOutcome:
        home = self.current_home()
        turn = _CodexTurn(self, request, home)
        outcome = await turn.run()
        if outcome.failure == self.usage_limit_code and len(self.homes()) > 1:
            self._exhausted.add(str(home))
            if len(self._exhausted) < len(self.homes()):
                outcome.account_hold = None  # Another profile is still usable.
                outcome.failure = "profile_exhausted"
        return outcome

    async def decide(self, request: DecisionRequest) -> DecisionOutcome:
        return await _CodexDecision(self, request, self.current_home()).run()

    async def probe_sandbox(self, request: TurnRequest) -> SandboxReport:
        """A host-side readiness check; the native profile is enforced by Codex itself."""
        checks = {"workspace_exists": request.workspace.exists()}
        interop = Path("/proc/sys/fs/binfmt_misc")
        checks["wsl_interop_off"] = not any(
            path.read_text().startswith("enabled")
            for path in interop.glob("WSLInterop*") if path.is_file()
        ) if interop.exists() else True
        return SandboxReport(ok=all(checks.values()), checks=checks,
                             detail="" if all(checks.values()) else
                             "Turn WSL interop off ([interop] enabled=false), or sandboxed "
                             "commands can start Windows programs outside the sandbox.")


class _CodexTurn:
    """One activation of ``codex app-server``: open the thread, run one turn, close."""

    def __init__(self, backend: CodexBackend, request: TurnRequest, home: Path):
        self.backend = backend
        self.request = request
        self.home = home
        self.peer: JsonRpcPeer | None = None
        self.process: Any = None
        self.thread_id: str | None = request.conversation_id
        self.turn_id: str | None = None
        self.terminal: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.usage = Usage()
        self.errors: list[dict[str, Any]] = []
        self.hold: str | None = None
        self.compacted = False
        self.input_delivered = False
        self.tool_tasks: set[asyncio.Task[Any]] = set()
        self.stopping = False

    async def run(self) -> TurnOutcome:
        request = self.request
        status = TurnStatus.UNKNOWN
        failure: str | None = None
        detail = ""
        try:
            await self._open()
            status = await self._turn()
        except asyncio.CancelledError:
            raise
        except RpcRemoteError as error:
            code = error_code(error.error)
            failure, detail = code or "remote_error", error_message(error.error)
            status = TurnStatus.FAILED if not self.input_delivered else TurnStatus.UNKNOWN
            self._note_hold(code)
        except Exception as error:  # noqa: BLE001 - the transport failed: outcome unknown
            failure, detail = type(error).__name__, str(error)[:2000]
            status = TurnStatus.UNKNOWN if self.input_delivered else TurnStatus.FAILED
        finally:
            await self._close()
        if status is TurnStatus.FAILED and failure is None and self.errors:
            failure = error_code(self.errors[-1]) or "model_error"
            detail = error_message(self.errors[-1])
        if failure in self.backend.hold_codes():
            self._note_hold(failure)
        saved = self.thread_id is not None and (request.conversation_id is not None
                                                or self.turn_id is not None)
        return TurnOutcome(status=status, conversation_id=self.thread_id,
                           input_delivered=self.input_delivered, usage=self.usage,
                           failure=failure, detail=detail, account_hold=self.hold,
                           conversation_saved=saved, compacted=self.compacted)

    def _note_hold(self, code: str | None) -> None:
        codes = self.backend.hold_codes()
        if code in codes:
            self.hold = codes[code]

    # ------------------------------------------------------------------- opening
    async def _open(self) -> None:
        request = self.request
        backend = self.backend
        request.workspace.mkdir(parents=True, exist_ok=True)
        request.temporary.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)
        protected = backend.protected_reads(list(request.protected_paths))
        sqlite_home = Path(backend.settings.home).expanduser().resolve() \
            if backend.settings.home else None
        argv = backend.argv(protected, request.network, sqlite_home)
        self.process = await request.host.launch(request.scope, argv,
                                                 backend.environment(self.home),
                                                 request.workspace)
        self.peer = JsonRpcPeer(self.process.stdin, self.process.stdout, self.process.stderr,
                                request_handler=self._server_request,
                                notification_handler=self._notification)
        await self.peer.request("initialize", {
            "clientInfo": {"name": "geyser", "title": "Geyser", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }, timeout=120)
        await self.peer.notify("initialized")
        shell = {key: value for key, value in request.environment.items()}
        params: dict[str, Any] = {
            "cwd": str(request.workspace),
            "approvalPolicy": "never",
            "runtimeWorkspaceRoots": [str(request.workspace)],
            "developerInstructions": request.standing_instructions,
            "permissions": WORKER_PROFILE,
            "config": {"model_reasoning_effort": request.effort or "high",
                       "shell_environment_policy.set": shell, **TOOL_EXECUTION},
        }
        if sqlite_home is not None:
            params["config"]["sqlite_home"] = str(sqlite_home)
        if request.model:
            params["model"] = request.model
        prefix = request.tools[0]["name"].rsplit("_", 1)[0] if request.tools else "geyser"
        params.update(backend.thread_extras(protected, request.network, request.temporary,
                                            prefix))
        if self.thread_id is None:
            params["dynamicTools"] = [
                {"type": "function", "name": tool["name"], "description": tool["description"],
                 "inputSchema": tool["input_schema"]} for tool in request.tools]
            params["allowProviderModelFallback"] = False
            result = await self.peer.request("thread/start", params)
        else:
            params["threadId"] = self.thread_id
            params["excludeTurns"] = True
            result = await self.peer.request("thread/resume", params)
        thread = result.get("thread", {}) if isinstance(result, dict) else {}
        if not isinstance(thread.get("id"), str):
            raise RpcProtocolError("thread/start or thread/resume returned no thread ID")
        self.thread_id = thread["id"]

    # -------------------------------------------------------------------- the turn
    async def _turn(self) -> TurnStatus:
        assert self.peer is not None
        control: TurnControl = self.request.control
        params = {"threadId": self.thread_id,
                  "input": [{"type": "text", "text": self.request.input_text}],
                  "effort": self.request.effort or "high"}
        result = await self.peer.request("turn/start", params, timeout=60)
        self.input_delivered = True
        turn = result.get("turn", {}) if isinstance(result, dict) else {}
        if not isinstance(turn.get("id"), str):
            raise RpcProtocolError("turn/start returned no turn ID")
        self.turn_id = turn["id"]
        self.request.transcript({"type": "turn_started", "turn": self.turn_id})
        waiters: dict[str, asyncio.Future[Any]] = {
            "failure": asyncio.create_task(self.peer.wait_failure()),
            "interrupt": asyncio.create_task(control.interrupt.wait()),
            "drain": asyncio.create_task(control.drain_requested.wait()),
            "steer": asyncio.create_task(control.steering.get()),
        }
        interrupted = False
        drain_deadline: float | None = None
        try:
            while not self.terminal.done():
                timeout = None
                if drain_deadline is not None:
                    timeout = max(0.0, drain_deadline - asyncio.get_running_loop().time())
                pending: list[asyncio.Future[Any]] = [self.terminal, *waiters.values()]
                done, _ = await asyncio.wait(pending, timeout=timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:  # The drain grace ended: stop the turn.
                    await self._interrupt()
                    drain_deadline = None
                    interrupted = True
                    continue
                if waiters["failure"] in done and not self.terminal.done():
                    await self._settle_after_close(waiters["failure"].result())
                    break
                if waiters["interrupt"] in done and not interrupted:
                    interrupted = True
                    await self._interrupt()
                    waiters["interrupt"] = asyncio.create_task(asyncio.Event().wait())
                if waiters["drain"] in done and drain_deadline is None and not interrupted:
                    drain_deadline = asyncio.get_running_loop().time() + DRAIN_GRACE_SECONDS
                    waiters["drain"] = asyncio.create_task(asyncio.Event().wait())
                if waiters["steer"] in done:
                    text, future = waiters["steer"].result()
                    await self._steer(text, future)
                    waiters["steer"] = asyncio.create_task(control.steering.get())
            if not self.terminal.done():
                raise RpcClosed("the app-server closed without a terminal event")
            native = self.terminal.result()
        finally:
            for task in waiters.values():
                task.cancel()
            await asyncio.gather(*waiters.values(), return_exceptions=True)
            # Steers still queued can no longer reach this turn: refuse them definitively.
            while not control.steering.empty():
                _text, pending_future = control.steering.get_nowait()
                if not pending_future.done():
                    pending_future.set_result(False)
        if native == "completed":
            return TurnStatus.COMPLETED
        if native == "interrupted":
            return TurnStatus.INTERRUPTED
        return TurnStatus.FAILED

    async def _settle_after_close(self, failure: BaseException) -> None:
        if isinstance(failure, RpcClosed) and self.peer is not None:
            with contextlib.suppress(TimeoutError):
                await self.peer.drain_callbacks(timeout=STOP_GRACE_SECONDS)
            if self.terminal.done():
                return
        raise failure

    async def _interrupt(self) -> None:
        if self.peer is None or self.turn_id is None or self.terminal.done():
            return
        self.stopping = True
        with contextlib.suppress(Exception):
            await self.peer.request("turn/interrupt", {"threadId": self.thread_id,
                                                       "turnId": self.turn_id},
                                    timeout=STOP_GRACE_SECONDS)

    async def _steer(self, text: str, future: asyncio.Future[bool]) -> None:
        if self.peer is None or self.terminal.done() or self.turn_id is None:
            future.set_result(False)
            return
        try:
            result = await self.peer.request("turn/steer", {
                "threadId": self.thread_id, "expectedTurnId": self.turn_id,
                "clientUserMessageId": uuid.uuid4().hex,
                "input": [{"type": "text", "text": text}]})
            future.set_result(isinstance(result, dict) and result.get("turnId") == self.turn_id)
        except RpcRemoteError:
            future.set_result(False)  # A definitive refusal: deliver at the next turn.
        except Exception as error:  # noqa: BLE001 - uncertain: the runner holds it
            if not future.done():
                future.set_exception(error)

    # ---------------------------------------------------------- server traffic
    async def _server_request(self, method: str, params: dict[str, Any]) -> Any:
        if method != "item/tool/call":
            raise RpcProtocolError(f"unsupported server request {method!r}; nothing is "
                                   "approved implicitly")
        if params.get("threadId") != self.thread_id:
            raise RpcProtocolError("a tool request targets another thread")
        call_id, name = params.get("callId"), params.get("tool")
        if not isinstance(call_id, str) or not isinstance(name, str):
            raise RpcProtocolError("a tool request lacks its identifiers")
        arguments = params.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        if not isinstance(arguments, dict):
            return tool_reply({"error": "invalid_argument",
                               "message": "Tool arguments must be a JSON object."})
        if self.stopping or self.terminal.done():
            return RpcReply(tool_reply({"error": "stopping",
                                        "message": "This turn is stopping; end it now."}),
                            ignore_closed_receiver=True)
        async def invoke() -> dict[str, Any]:
            return await self.request.on_tool_call(name, arguments, call_id)

        task: asyncio.Task[dict[str, Any]] = asyncio.create_task(invoke())
        self.tool_tasks.add(task)
        task.add_done_callback(self.tool_tasks.discard)
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the gateway never raises; be safe anyway
            result = {"error": "internal", "message": f"{type(error).__name__}: {error}"}
        return tool_reply(result)

    async def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "account/rateLimits/updated":
            return
        if params.get("threadId") not in (None, self.thread_id):
            return
        if method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                self.request.on_output(item["text"])
                self.request.transcript({"type": "agent_message", "text": item["text"][:20000]})
            elif item.get("type") == "contextCompaction":
                self._compaction()
        elif method == "thread/compacted":
            self._compaction()
        elif method == "thread/tokenUsage/updated":
            self._usage(params.get("tokenUsage", {}))
        elif method == "error":
            error = params.get("error")
            self.errors.append(error if isinstance(error, dict) else {"message": str(error)})
            self._note_hold(error_code(error))
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if isinstance(turn, dict) and turn.get("error") is not None:
                self.errors.append(turn["error"])
                self._note_hold(error_code(turn["error"]))
            status = turn.get("status") if isinstance(turn, dict) else None
            if status in {"completed", "interrupted", "failed"} and not self.terminal.done():
                self.terminal.set_result(status)

    def _compaction(self) -> None:
        self.compacted = True
        self.request.on_compaction()

    def _usage(self, token_usage: dict[str, Any]) -> None:
        last = token_usage.get("last")
        if not isinstance(last, dict):
            return
        sample = Usage(input_tokens=int(last.get("inputTokens", 0) or 0),
                       cached_tokens=int(last.get("cachedInputTokens", 0) or 0),
                       output_tokens=int(last.get("outputTokens", 0) or 0), requests=1,
                       context_tokens=int(last.get("inputTokens", 0) or 0))
        self.usage.add(sample)
        self.request.on_usage(self.usage)

    # ------------------------------------------------------------------ closing
    async def _close(self) -> None:
        self.stopping = True
        for task in list(self.tool_tasks):
            task.cancel()
        if self.tool_tasks:
            await asyncio.gather(*self.tool_tasks, return_exceptions=True)
        try:
            await self.request.host.terminate(self.request.scope, STOP_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 - recovery confirms quiescence later
            log.exception("terminating scope %s failed", self.request.scope.name)
        if self.peer is not None:
            await self.peer.close()


class _CodexDecision:
    """A tool-less, read-only decision with a JSON output schema."""

    def __init__(self, backend: CodexBackend, request: DecisionRequest, home: Path):
        self.backend = backend
        self.request = request
        self.home = home

    async def run(self) -> DecisionOutcome:
        request = self.request
        backend = self.backend
        terminal: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        messages: list[str] = []
        errors: list[Any] = []

        async def notification(method: str, params: dict[str, Any]) -> None:
            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    messages.append(item["text"])
            elif method == "error":
                errors.append(params.get("error"))
            elif method == "turn/completed" and not terminal.done():
                turn = params.get("turn", {})
                if isinstance(turn, dict) and turn.get("error") is not None:
                    errors.append(turn["error"])
                terminal.set_result(turn.get("status", "failed") if isinstance(turn, dict)
                                    else "failed")

        async def refuse(method: str, params: dict[str, Any]) -> Any:
            raise RpcProtocolError(f"decisions have no tools ({method})")

        peer = None
        try:
            argv = backend.decision_argv()
            process = await request.host.launch(request.scope, argv,
                                                backend.environment(self.home),
                                                request.directory)
            assert process.stdin is not None and process.stdout is not None
            peer = JsonRpcPeer(process.stdin, process.stdout, process.stderr,
                               request_handler=refuse, notification_handler=notification)
            await peer.request("initialize", {"clientInfo": {"name": "geyser-decision",
                                                             "version": "0.1.0"},
                                              "capabilities": {"experimentalApi": True}},
                               timeout=120)
            await peer.notify("initialized")
            params: dict[str, Any] = {"cwd": str(request.directory), "approvalPolicy": "never",
                                      "sandbox": "read-only",
                                      "config": {"model_reasoning_effort":
                                                 request.effort or "high"}}
            if request.model:
                params["model"] = request.model
            thread = (await peer.request("thread/start", params))["thread"]["id"]
            await peer.request("turn/start", {"threadId": thread,
                                              "input": [{"type": "text", "text": request.prompt}],
                                              "outputSchema": request.schema}, timeout=60)
            failure: asyncio.Future[Any] = asyncio.create_task(peer.wait_failure())
            watched: list[asyncio.Future[Any]] = [terminal, failure]
            done, _ = await asyncio.wait(watched, timeout=600,
                                         return_when=asyncio.FIRST_COMPLETED)
            failure.cancel()
            if terminal not in done:
                return DecisionOutcome(None, TurnStatus.UNKNOWN, "no terminal event")
            status = terminal.result()
        except Exception as error:  # noqa: BLE001
            code = error_code(getattr(error, "error", None))
            return DecisionOutcome(None, TurnStatus.FAILED, f"{type(error).__name__}: {error}",
                                   account_hold=backend.hold_codes().get(code or ""))
        finally:
            with contextlib.suppress(Exception):
                await request.host.terminate(request.scope, STOP_GRACE_SECONDS)
            if peer is not None:
                await peer.close()
        codes = backend.hold_codes()
        hold = next((codes[code] for code in map(error_code, errors) if code in codes), None)
        if status != "completed" or not messages:
            return DecisionOutcome(None, TurnStatus.FAILED, f"decision turn {status}",
                                   account_hold=hold)
        try:
            answer = json.loads(messages[-1])
        except json.JSONDecodeError:
            return DecisionOutcome(None, TurnStatus.COMPLETED, "the answer was not JSON")
        return DecisionOutcome(answer if isinstance(answer, dict) else None,
                               TurnStatus.COMPLETED)

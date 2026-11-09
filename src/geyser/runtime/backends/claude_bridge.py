"""Claude Code peer for one owned activation (ported from FormalGeyser).

Run as ``python -m geyser.runtime.backends.claude_bridge`` inside the activation's
scope. It speaks the subset of the Codex app-server protocol that the Codex backend
drives (initialize, thread/start, thread/resume, turn/start, turn/interrupt; tool
calls as ``item/tool/call`` requests; usage, compaction and terminal notifications),
so turn handling, tool replies and shutdown are shared.

No credential is read or forwarded: Claude Code uses the subscription sign-in under
``CLAUDE_CONFIG_DIR``, every API-billing source is removed from its environment, and
no prompt is sent until Claude Code reports a subscription account.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

NO_LIMIT_MS = "2000000000"
LOCAL_HOSTS = ["localhost", "*.localhost", "127.0.0.1", "0.0.0.0", "[::1]"]
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
READ_TOOLS = ["Read", "Glob", "Grep"]
WORK_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite"]
DENIED_TOOLS = ["Agent", "Task", "AskUserQuestion", "Skill", "SlashCommand", "WebFetch",
                "WebSearch"]
RESULT_CHARS = 200_000
API_BILLING_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
API_BILLING_KEYS = frozenset({"AWS_BEARER_TOKEN_BEDROCK", "CLAUDECODE"})
CLI_ENVIRONMENT = {
    "BASH_DEFAULT_TIMEOUT_MS": NO_LIMIT_MS,
    "BASH_MAX_TIMEOUT_MS": NO_LIMIT_MS,
    "MCP_TOOL_TIMEOUT": NO_LIMIT_MS,
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}
ACCOUNT_ERRORS = {"authentication_failed": "claudeSignInRequired",
                  "billing_error": "claudeUsageLimit", "rate_limit": "claudeUsageLimit"}


class BridgeError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def subscription_environment(environment: dict[str, str]) -> dict[str, str]:
    """Remove every credential source that would bill an API account."""
    return {key: value for key, value in environment.items()
            if not key.upper().startswith(API_BILLING_PREFIXES)
            and key.upper() not in API_BILLING_KEYS}


def session_file(home: Path, session_id: str) -> Path:
    return home / "geyser-sessions" / f"{session_id}.json"


def transcript_exists(home: Path, session_id: str) -> bool:
    projects = home / "projects"
    return projects.exists() and any(projects.glob(f"*/{session_id}.jsonl"))


def usage_totals(usage: Any) -> dict[str, int]:
    usage = usage if isinstance(usage, dict) else {}

    def number(*keys: str) -> int:
        return sum(int(usage.get(key) or 0) for key in keys)

    input_tokens = number("input_tokens", "cache_creation_input_tokens",
                          "cache_read_input_tokens", "inputTokens")
    cached = number("cache_read_input_tokens", "cachedInputTokens")
    output = number("output_tokens", "outputTokens")
    return {"inputTokens": input_tokens, "cachedInputTokens": cached, "outputTokens": output,
            "totalTokens": input_tokens + output}


def _rule(tool: str, path: Path, *, tree: bool = False) -> str:
    return f"{tool}(//{path.as_posix().lstrip('/')}{'/**' if tree else ''})"


def settings_for(*, workspace: Path, home: Path, prefix: str, network: bool,
                 protected: list[str]) -> dict[str, Any]:
    """Permission rules and the OS sandbox for one session; nothing is loaded from files."""
    allow = [*READ_TOOLS, "TodoWrite", f"mcp__{prefix}__{prefix}_actions",
             f"mcp__{prefix}__{prefix}_call", "Bash", _rule("Edit", workspace, tree=True)]
    sandbox_network: dict[str, Any] = {"deniedDomains": list(LOCAL_HOSTS)}
    if network:
        allow.append("WebFetch(domain:*)")
    else:
        sandbox_network["allowedDomains"] = []
    denied = [Path(path) for path in dict.fromkeys([str(home / ".credentials.json"),
                                                    *protected])]
    return {
        "permissions": {
            "allow": allow,
            "deny": [*(_rule("Read", path, tree=path.is_dir()) for path in denied),
                     *DENIED_TOOLS],
            "defaultMode": "dontAsk",
            "disableBypassPermissionsMode": "disable",
        },
        "sandbox": {
            "enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False, "network": sandbox_network,
            "filesystem": {"denyRead": [str(path) for path in denied]},
        },
    }


def lenient_call_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Claude often writes an operation's fields beside ``operation``, or ``arguments``
    as JSON text; accept both and restore the exact shape in ``gateway_call``."""
    shown = deepcopy(schema)
    shown["required"] = [name for name in shown.get("required", []) if name != "arguments"]
    shown["additionalProperties"] = True
    arguments = shown.get("properties", {}).get("arguments")
    if isinstance(arguments, dict):
        arguments.update(type=["object", "string"])
    return shown


def gateway_call(call: dict[str, Any]) -> dict[str, Any]:
    beside = {key: value for key, value in call.items() if key not in {"operation", "arguments"}}
    arguments: Any = call.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = None
        if not isinstance(arguments, dict):
            raise ValueError("Write `arguments` as a JSON object of the operation's fields.")
    if beside:
        if not isinstance(arguments, dict):
            raise ValueError("Put the operation's fields in the `arguments` object.")
        clashes = [key for key in beside if key in arguments and arguments[key] != beside[key]]
        if clashes:
            raise ValueError("Give each field once, inside `arguments`: " + ", ".join(clashes[:6]))
        arguments = {**beside, **arguments}
    return {"operation": call.get("operation"), "arguments": arguments}


class Peer:
    """The bridge's side of the newline-delimited JSON-RPC pipe (stdin/stdout)."""

    def __init__(self, handler: Any):
        self.handler = handler
        self.loop = asyncio.get_running_loop()
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.next_id = 0
        self.write_lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in sys.stdin.buffer:
            if line.strip():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self.loop.call_soon_threadsafe(self.incoming.put_nowait, message)
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, None)

    async def send(self, message: dict[str, Any]) -> None:
        frame = (json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)
                 + "\n").encode("utf-8")

        def emit() -> None:
            with self.write_lock:
                sys.stdout.buffer.write(frame)
                sys.stdout.buffer.flush()

        await asyncio.to_thread(emit)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.send({"method": method, "params": params})

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.next_id += 1
        request_id = self.next_id
        future: asyncio.Future[Any] = self.loop.create_future()
        self.pending[request_id] = future
        await self.send({"id": request_id, "method": method, "params": params})
        try:
            return await future
        finally:
            self.pending.pop(request_id, None)

    async def _answer(self, message: dict[str, Any]) -> None:
        try:
            result = await self.handler(message["method"], message.get("params") or {})
            await self.send({"id": message["id"], "result": result})
        except BridgeError as error:
            await self.send({"id": message["id"], "error": {
                "code": -32000, "message": str(error), "data": {"code": error.code}}})
        except Exception as error:  # noqa: BLE001 - becomes a JSON-RPC error
            await self.send({"id": message["id"], "error": {
                "code": -32603, "message": f"{type(error).__name__}: {error}"}})

    async def serve(self) -> None:
        tasks: set[asyncio.Task[Any]] = set()
        while (message := await self.incoming.get()) is not None:
            if "method" in message and "id" in message:
                task = asyncio.create_task(self._answer(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            elif "method" not in message and message.get("id") in self.pending:
                future = self.pending[message["id"]]
                if not future.done():
                    if "error" in message:
                        future.set_exception(BridgeError(str(message["error"])))
                    else:
                        future.set_result(message.get("result"))
        for future in self.pending.values():
            if not future.done():
                future.set_exception(BridgeError("the client closed the pipe"))


class Turn:
    def __init__(self, turn_id: str):
        self.id = turn_id
        self.task: asyncio.Task[None] | None = None
        self.interrupt_reason: str | None = None
        self.messages: dict[str, dict[str, int]] = {}
        self.reported = 0


class Bridge:
    def __init__(self, sdk: Any):
        self.sdk = sdk
        self.peer: Peer | None = None
        self.home = Path(os.environ.get("CLAUDE_CONFIG_DIR", "")).expanduser()
        self.client: Any = None
        self.session_id: str | None = None
        self.record: dict[str, Any] = {}
        self.params: dict[str, Any] = {}
        self.workspace: Path | None = None
        self.turn: Turn | None = None
        self.stderr: deque[str] = deque(maxlen=64)
        self.decision = False
        self.prefix = "geyser"
        self._calls: dict[tuple[str, str], deque[str]] = {}
        self._closing = asyncio.Event()
        self._lifecycle: asyncio.Task[None] | None = None
        self._connected: asyncio.Future[None] | None = None

    async def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            if not self.home.is_absolute():
                raise BridgeError("CLAUDE_CONFIG_DIR must name the signed-in configuration "
                                  "directory")
            version = getattr(self.sdk, "__version__", "unknown")
            return {"userAgent": f"geyser-claude-bridge/1 claude-agent-sdk/{version}"}
        if method in {"thread/start", "thread/resume"}:
            return await self._open(method, params)
        if method == "turn/start":
            return await self._turn_start(params)
        if method == "turn/interrupt":
            return await self._interrupt("operator")
        raise BridgeError(f"unsupported request {method}")

    async def _open(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.session_id is not None:
            raise BridgeError("one session is opened per activation")
        self.params = params
        self.workspace = Path(params["cwd"]).resolve()
        self.decision = params.get("sandbox") == "read-only"
        self.prefix = str(params.get("geyserPrefix") or "geyser")
        if method == "thread/start":
            self.session_id = str(uuid4())
            tools = list(params.get("dynamicTools") or [])
            launch: dict[str, Any] = {"session_id": self.session_id}
            record: dict[str, Any] = {"session_id": self.session_id, "tools": tools,
                      "created_at": int(time.time() * 1000)}
        else:
            self.session_id = str(UUID(str(params["threadId"])))
            path = session_file(self.home, self.session_id)
            if not path.exists() or not transcript_exists(self.home, self.session_id):
                raise BridgeError("this Claude session was never saved; start a new one")
            record = json.loads(path.read_text(encoding="utf-8"))
            launch = {"resume": self.session_id}
        self.record = record
        await self._connect(launch)
        path = session_file(self.home, self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return {"thread": {"id": self.session_id, "turns": []}}

    def _options(self, launch: dict[str, Any]) -> Any:
        params, workspace = self.params, self.workspace
        assert workspace is not None and self.session_id is not None
        prefix = self.prefix
        tools = [] if self.decision else WORK_TOOLS
        mcp: dict[str, Any] = {}
        allowed: list[str] = []
        if not self.decision:
            server = self.sdk.create_sdk_mcp_server(name=prefix, version="1", tools=[
                self._mcp_tool(spec) for spec in self.record.get("tools", [])])
            mcp = {prefix: server}
            allowed = [name for name in tools if name not in WRITE_TOOLS] + [
                f"mcp__{prefix}__{prefix}_actions", f"mcp__{prefix}__{prefix}_call"]
        temporary = Path(params.get("tmpdir") or (workspace / ".tmp"))
        temporary.mkdir(parents=True, exist_ok=True)
        environment = {**CLI_ENVIRONMENT, "CLAUDE_CONFIG_DIR": str(self.home),
                       "CLAUDE_CODE_TMPDIR": str(temporary)}
        protected = [path for path in params.get("denyRead") or []
                     if isinstance(path, str) and Path(path).is_absolute()]
        settings = settings_for(workspace=workspace, home=self.home, prefix=prefix,
                                network=bool(params.get("network", True)), protected=protected)
        options: dict[str, Any] = {
            "cwd": str(workspace), "tools": list(tools), "allowed_tools": allowed,
            "disallowed_tools": list(DENIED_TOOLS), "permission_mode": "dontAsk",
            "system_prompt": {"type": "preset", "preset": "claude_code",
                              "append": params.get("developerInstructions", "")},
            "setting_sources": [], "strict_mcp_config": True, "mcp_servers": mcp,
            "settings": json.dumps(settings, separators=(",", ":")),
            "hooks": {"PreToolUse": [self.sdk.HookMatcher(matcher=None,
                                                          hooks=[self._pre_tool_use])]},
            "env": environment, "max_buffer_size": 64 * 1024 * 1024,
            "stderr": self.stderr.append, **launch,
        }
        if params.get("model"):
            options["model"] = params["model"]
        if params.get("cliPath"):
            options["cli_path"] = params["cliPath"]
        return self.sdk.ClaudeAgentOptions(**options)

    async def _connect(self, launch: dict[str, Any]) -> None:
        """Start Claude Code and confirm a subscription account before anything is sent."""
        self.client = self.sdk.ClaudeSDKClient(self._options(launch))
        self._connected = asyncio.get_running_loop().create_future()
        self._lifecycle = asyncio.create_task(self._own_client())
        await asyncio.shield(self._connected)
        info = await self.client.get_server_info() or {}
        account = info.get("account") if isinstance(info, dict) else None
        account = account if isinstance(account, dict) else {}
        if account.get("apiKeySource") not in {None, "none"}:
            raise BridgeError("Claude Code selected an API key; API billing is disabled",
                              "claudeSubscriptionRequired")
        if not account.get("subscriptionType"):
            raise BridgeError("Claude Code is not signed in with a subscription",
                              "claudeSignInRequired")

    async def _own_client(self) -> None:
        assert self._connected is not None
        try:
            await self.client.connect()
        except BaseException as error:  # noqa: BLE001
            if not self._connected.done():
                tail = " | ".join(list(self.stderr)[-5:])
                self._connected.set_exception(BridgeError(
                    f"Claude Code did not start: {error}" + (f" | {tail}" if tail else "")))
            return
        self._connected.set_result(None)
        try:
            await self._closing.wait()
        finally:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _mcp_tool(self, spec: dict[str, Any]) -> Any:
        name = spec["name"]
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        if name.endswith("_call"):
            schema = lenient_call_schema(schema)

        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            return await self._call_tool(name, arguments)

        return self.sdk.SdkMcpTool(name=name, description=spec.get("description", ""),
                                   input_schema=schema, handler=handler,
                                   annotations=self.sdk.ToolAnnotations(
                                       maxResultSizeChars=RESULT_CHARS))

    @staticmethod
    def _key(name: str, arguments: Any) -> tuple[str, str]:
        return name, json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps({"error": "invalid_argument",
                                                                  "message": message})}],
                "is_error": True}

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        turn = self.turn
        if turn is None or self.peer is None or self.session_id is None:
            return self._error("No turn is active.")
        waiting = self._calls.get(self._key(name, arguments))
        call_id = waiting.popleft() if waiting else "call-" + uuid4().hex
        if name.endswith("_call"):
            try:
                arguments = gateway_call(arguments)
            except ValueError as error:
                return self._error(str(error))
        try:
            reply = await self.peer.request("item/tool/call", {
                "threadId": self.session_id, "turnId": turn.id, "callId": call_id,
                "tool": name, "arguments": arguments})
        except BridgeError as error:
            return self._error(str(error))
        items = reply.get("contentItems") if isinstance(reply, dict) else None
        text = "".join(item.get("text", "") for item in items or [] if isinstance(item, dict))
        return {"content": [{"type": "text", "text": text}],
                "is_error": not (isinstance(reply, dict) and reply.get("success") is True)}

    async def _pre_tool_use(self, data: dict[str, Any], tool_use_id: str | None,
                            context: Any) -> dict[str, Any]:
        """The authoritative check before any tool runs, whatever the rules say."""
        name = str(data.get("tool_name") or "")
        raw = data.get("tool_input")
        tool_input: dict[str, Any] = raw if isinstance(raw, dict) else {}
        prefix = f"mcp__{self.prefix}__"
        if name.startswith(prefix):
            if tool_use_id:
                self._calls.setdefault(self._key(name[len(prefix):], tool_input),
                                       deque()).append(tool_use_id)
            return {}
        if name not in WORK_TOOLS:
            return self._deny(f"{name} is not available in this session")
        if name in WRITE_TOOLS:
            target = tool_input.get("file_path") or tool_input.get("notebook_path")
            if not isinstance(target, str) or not self._inside_workspace(target):
                return self._deny("Write only inside your workspace")
        if name == "Bash" and tool_input.get("dangerouslyDisableSandbox"):
            return self._deny("Commands always run inside the sandbox")
        return {}

    def _inside_workspace(self, target: str) -> bool:
        assert self.workspace is not None
        path = Path(target)
        if not path.is_absolute():
            path = self.workspace / path
        return Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(self.workspace)))

    @staticmethod
    def _deny(reason: str) -> dict[str, Any]:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}

    async def _turn_start(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.session_id is None or params.get("threadId") != self.session_id:
            raise BridgeError("open the session before starting its turn")
        if self.turn is not None:
            raise BridgeError("one turn is permitted per activation")
        text = "".join(part.get("text", "") for part in params.get("input") or []
                       if isinstance(part, dict) and part.get("type") == "text")
        turn = Turn(str(uuid4()))
        self.turn = turn
        message = {"type": "user", "message": {"role": "user", "content": text},
                   "parent_tool_use_id": None, "session_id": self.session_id}

        async def prompt():
            yield message

        await self.client.query(prompt())
        turn.task = asyncio.create_task(self._consume(turn))
        return {"turn": {"id": turn.id, "status": "inProgress"}}

    async def _interrupt(self, reason: str) -> dict[str, Any]:
        turn = self.turn
        if turn is not None and turn.interrupt_reason is None:
            turn.interrupt_reason = reason
            await self.client.interrupt()
        return {}

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.peer is not None
        await self.peer.notify(method, {"threadId": self.session_id, **params})

    async def _turn_error(self, turn: Turn, code: str | None, message: str) -> None:
        error = {"code": code, "message": message} if code else {"message": message}
        await self._notify("error", {"turnId": turn.id, "willRetry": False, "error": error})

    async def _usage(self, turn: Turn, final: dict[str, int] | None = None) -> None:
        spent = {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        for counts in turn.messages.values():
            spent = {key: spent[key] + counts.get(key, 0) for key in spent}
        if final is not None and final["totalTokens"] > spent["totalTokens"]:
            spent = final
        if spent["totalTokens"] <= turn.reported:
            return
        turn.reported = spent["totalTokens"]
        last = list(turn.messages.values())[-1] if turn.messages else spent
        await self._notify("thread/tokenUsage/updated", {"turnId": turn.id,
                                                         "tokenUsage": {"total": spent,
                                                                        "last": last}})

    async def _consume(self, turn: Turn) -> None:
        sdk = self.sdk
        rate_limit_event = getattr(sdk, "RateLimitEvent", None)
        status, error = "failed", None
        try:
            await self._notify("turn/started", {"turn": {"id": turn.id}})
            async for message in self.client.receive_response():
                if isinstance(message, sdk.SystemMessage):
                    if message.subtype == "compact_boundary":
                        await self._notify("thread/compacted", {"turnId": turn.id})
                elif isinstance(message, sdk.AssistantMessage):
                    await self._assistant(turn, message)
                elif rate_limit_event is not None and isinstance(message, rate_limit_event):
                    await self._rate_limit(turn, message)
                elif isinstance(message, sdk.ResultMessage):
                    status, error = await self._result(turn, message)
                    if turn.interrupt_reason is not None and \
                            message.subtype == "error_during_execution":
                        error = None
                    break
            else:
                error = {"message": "Claude Code ended the turn without a result"}
        except asyncio.CancelledError:
            raise
        except Exception as failure:  # noqa: BLE001
            tail = " | ".join(list(self.stderr)[-5:])
            error = {"message": f"Claude Code stopped during the turn: {failure}"
                     + (f" | {tail}" if tail else "")}
        if turn.interrupt_reason is not None:
            status = "interrupted"
        await self._notify("turn/completed", {"turn": {"id": turn.id, "status": status,
                                                       "error": error}})

    async def _assistant(self, turn: Turn, message: Any) -> None:
        if getattr(message, "parent_tool_use_id", None):
            return
        if getattr(message, "error", None):
            text = "".join(getattr(block, "text", "") for block in message.content)
            await self._turn_error(turn, ACCOUNT_ERRORS.get(message.error),
                                   text or f"Claude Code reported {message.error}")
        usage = getattr(message, "usage", None)
        if usage:
            key = getattr(message, "message_id", None) or getattr(message, "uuid", None) or \
                str(len(turn.messages))
            counts = usage_totals(usage)
            previous = turn.messages.get(key, {})
            turn.messages[key] = {name: max(value, previous.get(name, 0))
                                  for name, value in counts.items()}
            await self._usage(turn)
        texts = [block.text for block in message.content
                 if isinstance(block, self.sdk.TextBlock)]
        if texts and not getattr(message, "error", None):
            await self._notify("item/completed", {"turnId": turn.id, "item": {
                "type": "agentMessage", "id": "message-" + uuid4().hex, "text": "".join(texts)}})

    async def _rate_limit(self, turn: Turn, event: Any) -> None:
        info = event.rate_limit_info
        if getattr(info, "status", None) == "rejected" or \
                getattr(info, "rate_limit_type", None) == "overage":
            await self._turn_error(turn, "claudeUsageLimit", "Claude subscription limit reached")
            await self._interrupt("usage_limit")

    async def _result(self, turn: Turn, result: Any) -> tuple[str, dict[str, Any] | None]:
        await self._usage(turn, usage_totals(getattr(result, "usage", None)))
        if result.subtype == "success" and not result.is_error:
            return "completed", None
        detail = "; ".join(getattr(result, "errors", None) or []) or result.result or \
            result.subtype
        return "failed", {"message": f"Claude Code ended the turn: {detail}"[:4000]}

    async def close(self) -> None:
        turn = self.turn
        if turn is not None and turn.task is not None and not turn.task.done():
            turn.task.cancel()
            await asyncio.gather(turn.task, return_exceptions=True)
        self._closing.set()
        if self._lifecycle is not None:
            await asyncio.gather(self._lifecycle, return_exceptions=True)


async def serve() -> None:
    import claude_agent_sdk as sdk

    for key in set(os.environ) - set(subscription_environment(dict(os.environ))):
        del os.environ[key]
    bridge = Bridge(sdk)
    bridge.peer = Peer(bridge.handle)
    try:
        await bridge.peer.serve()
    finally:
        await bridge.close()


def main() -> int:
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())

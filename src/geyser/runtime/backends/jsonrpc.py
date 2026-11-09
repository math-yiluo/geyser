"""A bounded, asynchronous, newline-delimited JSON-RPC peer over a child's pipes.

Ported from FormalGeyser's tested transport. Server requests and notifications are
handled by separate serial consumers, so a tool call waiting on the Hub never blocks
the reader. Incoming backlogs beyond ``queue_limit`` frames spill to a private
temporary file instead of growing memory. Any transport failure is recorded once
and seen identically by every waiter.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, BinaryIO, cast


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant: {value}")


class RpcError(RuntimeError):
    pass


class RpcClosed(RpcError):
    pass


class RpcProtocolError(RpcError):
    pass


class RpcTimeout(RpcError):
    def __init__(self, method: str):
        super().__init__(f"RPC request timed out; the remote outcome is unknown: {method}")
        self.method = method


class RpcRemoteError(RpcError):
    def __init__(self, error: Any):
        super().__init__(f"remote RPC error: {error}")
        self.error = error


@dataclass
class RpcReply:
    result: Any
    ignore_closed_receiver: bool = False


RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class _IncomingQueue:
    """A serial FIFO with bounded memory and overflow storage in a private file."""

    def __init__(self, memory_limit: int, max_frame_bytes: int):
        self._memory_limit = memory_limit
        self._max_frame_bytes = max_frame_bytes
        self._memory: deque[dict[str, Any]] = deque()
        self._spill: BinaryIO | None = None
        self._spill_position = 0
        self._spilled = 0
        self._unfinished = 0
        self._available = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()
        self._closed = False

    def put_nowait(self, message: dict[str, Any], frame: bytes) -> None:
        if self._closed:
            raise RpcClosed("the incoming queue is closed")
        if self._spill is None and len(self._memory) < self._memory_limit:
            self._memory.append(message)
        else:
            try:
                if self._spill is None:
                    self._spill = cast(BinaryIO, tempfile.TemporaryFile(mode="w+b",
                                                                        prefix="geyser-rpc-"))
                self._spill.seek(0, 2)
                if self._spill.write(frame) != len(frame):
                    raise OSError("incomplete spill write")
                self._spill.flush()
                self._spilled += 1
            except OSError as error:
                raise RpcError("incoming RPC spill storage failed") from error
        self._unfinished += 1
        self._drained.clear()
        self._available.set()

    async def get(self) -> dict[str, Any]:
        await self._available.wait()
        if self._closed:
            raise RpcClosed("the incoming queue is closed")
        if self._memory:
            message = self._memory.popleft()
        else:
            assert self._spill is not None
            try:
                self._spill.seek(self._spill_position)
                frame = self._spill.readline(self._max_frame_bytes + 1)
                if len(frame) > self._max_frame_bytes or not frame.endswith(b"\n"):
                    raise ValueError("invalid spill frame")
                self._spill_position = self._spill.tell()
                message = cast(dict[str, Any], json.loads(frame))
                self._spilled -= 1
                if not self._spilled:
                    self._close_spill()
            except (OSError, ValueError) as error:
                raise RpcError("incoming RPC spill storage failed") from error
        if not self._memory and not self._spilled:
            self._available.clear()
        return message

    def task_done(self) -> None:
        self._unfinished -= 1
        if self._unfinished <= 0:
            self._unfinished = 0
            self._drained.set()

    async def join(self) -> None:
        await self._drained.wait()

    def _close_spill(self) -> None:
        spill, self._spill = self._spill, None
        self._spill_position = 0
        if spill is not None:
            spill.close()

    def close(self) -> None:
        self._closed = True
        self._memory.clear()
        self._spilled = 0
        self._available.set()
        self._drained.set()
        self._close_spill()


class JsonRpcPeer:
    def __init__(self, stdin: asyncio.StreamWriter, stdout: asyncio.StreamReader,
                 stderr: asyncio.StreamReader | None, *,
                 request_handler: RequestHandler | None = None,
                 notification_handler: NotificationHandler | None = None,
                 max_frame_bytes: int = 8 * 1024 * 1024, queue_limit: int = 256,
                 request_timeout: float = 60.0, stderr_bytes: int = 65_536):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.request_handler = request_handler
        self.notification_handler = notification_handler
        self.max_frame_bytes = max_frame_bytes
        self.request_timeout = request_timeout
        self.stderr_bytes = stderr_bytes
        self.stderr_tail = bytearray()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._retired: deque[int] = deque(maxlen=1024)
        self._outgoing: asyncio.Queue[tuple[bytes, asyncio.Future[None]]] = asyncio.Queue(
            queue_limit)
        self._requests = _IncomingQueue(queue_limit, max_frame_bytes)
        self._notifications = _IncomingQueue(queue_limit, max_frame_bytes)
        self._failure: asyncio.Future[BaseException] = \
            asyncio.get_running_loop().create_future()
        self._closed = False
        self._tasks = [
            asyncio.create_task(self._reader(), name="rpc-reader"),
            asyncio.create_task(self._writer(), name="rpc-writer"),
            asyncio.create_task(self._serve(), name="rpc-requests"),
            asyncio.create_task(self._notify(), name="rpc-notifications"),
        ]
        if stderr is not None:
            self._tasks.append(asyncio.create_task(self._drain_stderr(), name="rpc-stderr"))

    # ----------------------------------------------------------------- failure
    @property
    def failure(self) -> BaseException | None:
        return self._failure.result() if self._failure.done() else None

    async def wait_failure(self) -> BaseException:
        return await asyncio.shield(self._failure)

    def _fail(self, error: BaseException) -> None:
        if not self._failure.done():
            self._failure.set_result(error)
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        while not self._outgoing.empty():
            _, ack = self._outgoing.get_nowait()
            if not ack.done():
                ack.set_exception(error)
            self._outgoing.task_done()

    def _check(self) -> None:
        if self._closed:
            raise RpcClosed("the RPC peer is closed")
        if self.failure is not None:
            raise self.failure

    # ----------------------------------------------------------------- sending
    async def _send(self, message: dict[str, Any]) -> None:
        self._check()
        try:
            frame = (json.dumps(message, ensure_ascii=False, separators=(",", ":"),
                                allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, TypeError) as error:
            raise RpcProtocolError("the message is not valid JSON") from error
        if len(frame) > self.max_frame_bytes:
            raise RpcProtocolError("an outgoing frame exceeds the byte limit")
        ack: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        try:
            self._outgoing.put_nowait((frame, ack))
        except asyncio.QueueFull as error:
            raise RpcProtocolError("the outgoing RPC queue is full") from error
        await ack

    async def request(self, method: str, params: dict[str, Any] | None = None, *,
                      timeout: float | None = None) -> Any:
        self._check()
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(timeout if timeout is not None else self.request_timeout):
                await self._send({"id": request_id, "method": method, "params": params or {}})
                return await future
        except TimeoutError as error:
            raise RpcTimeout(method) from error
        finally:
            self._pending.pop(request_id, None)
            self._retired.append(request_id)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    # --------------------------------------------------------------- receiving
    async def _reader(self) -> None:
        try:
            while True:
                await asyncio.sleep(0)
                try:
                    line = await self.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as error:
                    raise RpcProtocolError("an incoming frame exceeds the byte limit") from error
                if not line:
                    raise RpcClosed("the peer's stdout reached EOF")
                if len(line) > self.max_frame_bytes:
                    raise RpcProtocolError("an incoming frame exceeds the byte limit")
                if not line.endswith(b"\n"):
                    raise RpcProtocolError("the stream ended inside a frame")
                if not line.strip():
                    continue
                try:
                    message = json.loads(line, parse_constant=_reject_constant)
                except (UnicodeDecodeError, ValueError) as error:
                    raise RpcProtocolError("a malformed JSON frame arrived") from error
                if not isinstance(message, dict):
                    raise RpcProtocolError("RPC frames must be objects")
                self._route(message, line)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 - recorded once for every waiter
            self._fail(error)

    def _route(self, message: dict[str, Any], line: bytes) -> None:
        if "method" in message:
            if not isinstance(message["method"], str):
                raise RpcProtocolError("an RPC method must be a string")
            if not isinstance(message.get("params", {}), dict):
                raise RpcProtocolError("RPC params must be an object")
            if "id" in message and (isinstance(message["id"], bool)
                                    or not isinstance(message["id"], (str, int))):
                raise RpcProtocolError("an invalid server request ID arrived")
            queue = self._requests if "id" in message else self._notifications
            queue.put_nowait(message, line)
            return
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise RpcProtocolError("an invalid response ID arrived")
        if ("result" in message) == ("error" in message):
            raise RpcProtocolError("a response needs exactly one of result or error")
        future = self._pending.get(request_id)
        if future is None:
            if request_id in self._retired:
                return
            raise RpcProtocolError("a response has an unknown request ID")
        if future.done():
            return
        if "error" in message:
            future.set_exception(RpcRemoteError(message["error"]))
        else:
            future.set_result(message["result"])

    async def _writer(self) -> None:
        active: asyncio.Future[None] | None = None
        try:
            while True:
                frame, ack = await self._outgoing.get()
                active = ack
                try:
                    self.stdin.write(frame)
                    await self.stdin.drain()
                    if not ack.done():
                        ack.set_result(None)
                finally:
                    self._outgoing.task_done()
                active = None
        except asyncio.CancelledError:
            if active is not None and not active.done():
                active.cancel()
            raise
        except BaseException as error:  # noqa: BLE001
            closed = RpcClosed(f"the RPC writer failed: {type(error).__name__}")
            if active is not None and not active.done():
                active.set_exception(closed)
            self._fail(closed)

    async def _drain_stderr(self) -> None:
        assert self.stderr is not None
        try:
            while chunk := await self.stderr.read(8192):
                self.stderr_tail.extend(chunk)
                if len(self.stderr_tail) > self.stderr_bytes:
                    del self.stderr_tail[:-self.stderr_bytes]
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - stderr is observation only
            return

    async def _serve(self) -> None:
        try:
            while True:
                message = await self._requests.get()
                try:
                    await self._answer(message)
                finally:
                    self._requests.task_done()
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001
            self._fail(error)

    async def _answer(self, message: dict[str, Any]) -> None:
        if self.request_handler is None:
            await self._send({"id": message["id"], "error": {
                "code": -32601, "message": "unsupported server request"}})
            return
        try:
            reply = await self.request_handler(message["method"], message.get("params", {}))
        except Exception as error:  # noqa: BLE001 - becomes a JSON-RPC error reply
            await self._send({"id": message["id"], "error": {
                "code": -32603, "message": f"{type(error).__name__}: {str(error)[:1000]}"}})
            return
        if not isinstance(reply, RpcReply):
            reply = RpcReply(reply)
        try:
            await self._send({"id": message["id"], "result": reply.result})
        except RpcClosed:
            if not reply.ignore_closed_receiver:
                raise

    async def _notify(self) -> None:
        try:
            while True:
                message = await self._notifications.get()
                try:
                    if self.notification_handler is not None:
                        await self.notification_handler(message["method"],
                                                        message.get("params", {}))
                finally:
                    self._notifications.task_done()
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001
            self._fail(error)

    async def drain_callbacks(self, *, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            await self._requests.join()
            await self._notifications.join()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fail(RpcClosed("the RPC peer was closed"))
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._requests.close()
        self._notifications.close()

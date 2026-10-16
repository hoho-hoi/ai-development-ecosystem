"""Newline-delimited JSON-RPC 2.0 over asyncio streams (the ACP stdio transport).

Every frame in either direction is passed, unmodified, to an optional `tap`
so the broker can keep the raw exchange in its history.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

RequestHandler = Callable[[str, Any], Awaitable[Any]]
NotificationHandler = Callable[[str, Any], Awaitable[None]]
Tap = Callable[[str, dict[str, Any]], None]  # ("in" | "out", frame)


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class ConnectionClosed(Exception):
    pass


class JsonRpcPeer:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_request: RequestHandler | None = None,
        on_notification: NotificationHandler | None = None,
        tap: Tap | None = None,
    ):
        self._reader = reader
        self._writer = writer
        self._on_request = on_request
        self._on_notification = on_notification
        self._tap = tap
        self._ids = itertools.count(1)
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.closed = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._read_loop())

    async def _write(self, frame: dict[str, Any]) -> None:
        if self.closed.is_set():
            raise ConnectionClosed("connection closed")
        if self._tap:
            self._tap("out", frame)
        data = (json.dumps(frame, ensure_ascii=False) + "\n").encode()
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def request(self, method: str, params: Any = None, timeout: float | None = None) -> Any:
        rid = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            frame["params"] = params
        try:
            await self._write(frame)
            return await asyncio.wait_for(fut, timeout) if timeout else await fut
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Any = None) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        await self._write(frame)

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("non-JSON line from peer: %r", line[:200])
                    if self._tap:
                        self._tap("in", {"_unparsed": line.decode(errors="replace")})
                    continue
                if self._tap:
                    self._tap("in", frame)
                self._dispatch(frame)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionClosed("peer closed the connection"))

    def _dispatch(self, frame: dict[str, Any]) -> None:
        if "method" in frame and "id" in frame:
            self._spawn(self._handle_request(frame))
        elif "method" in frame:
            if self._on_notification:
                self._spawn(self._on_notification(frame["method"], frame.get("params")))
        elif "id" in frame:
            fut = self._pending.get(frame["id"])
            if fut is None or fut.done():
                return
            if "error" in frame:
                err = frame["error"] or {}
                fut.set_exception(RpcError(err.get("code", -32000), err.get("message", ""), err.get("data")))
            else:
                fut.set_result(frame.get("result"))

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_request(self, frame: dict[str, Any]) -> None:
        rid = frame["id"]
        try:
            if not self._on_request:
                raise RpcError(-32601, f"method not found: {frame['method']}")
            result = await self._on_request(frame["method"], frame.get("params"))
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "result": result}
        except RpcError as e:
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.message}}
            if e.data is not None:
                reply["error"]["data"] = e.data
        except Exception as e:  # never leave the agent waiting
            log.exception("request handler failed")
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}
        try:
            await self._write(reply)
        except ConnectionClosed:
            pass

    async def close(self) -> None:
        try:
            self._writer.close()
        except Exception:
            pass
        if self._task:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()

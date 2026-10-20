"""Minimal ACP (Agent Client Protocol, v1) client.

The broker is the ACP client of every managed session: it creates the ACP
session, sends prompts, receives session/update notifications and answers
permission requests according to the profile. It advertises no fs/terminal
capabilities, so the agent works only with its own tools inside the container.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from . import __version__
from .jsonrpc import JsonRpcPeer, RpcError, Tap
from .profiles import Permissions

ACP_PROTOCOL_VERSION = 1


def decide_permission(params: dict[str, Any], policy: Permissions) -> dict[str, Any]:
    """Pick an ACP permission outcome for a session/request_permission call."""
    options = params.get("options") or []
    kind = (params.get("toolCall") or {}).get("kind")
    allowed = policy.mode == "allow" and (policy.allow_kinds is None or kind in policy.allow_kinds)
    prefs = ("allow_once", "allow_always") if allowed else ("reject_once", "reject_always")
    for pref in prefs:
        for opt in options:
            if opt.get("kind") == pref:
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
    return {"outcome": {"outcome": "cancelled"}}


class AcpClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        permissions: Permissions,
        tap: Tap | None = None,
        on_update: Callable[[dict[str, Any]], None] | None = None,
        on_permission: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ):
        self.permissions = permissions
        self._on_update = on_update
        self._on_permission = on_permission
        self._collect: dict[str, list[str]] = {}  # session id -> agent text of the running turn
        self._cancelled: set[str] = set()
        self.agent_info: dict[str, Any] = {}
        self.peer = JsonRpcPeer(
            reader,
            writer,
            on_request=self._handle_request,
            on_notification=self._handle_notification,
            tap=tap,
        )
        self.peer.start()

    async def initialize(self, timeout: float = 120) -> dict[str, Any]:
        result = await self.peer.request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "harness-broker", "version": __version__},
            },
            timeout=timeout,
        )
        if (result or {}).get("protocolVersion") != ACP_PROTOCOL_VERSION:
            raise RpcError(-32000, f"unsupported ACP protocol version: {result.get('protocolVersion')}")
        self.agent_info = result
        return result

    async def new_session(self, cwd: str, mcp_servers: list[dict[str, Any]], timeout: float = 120) -> str:
        result = await self.peer.request("session/new", {"cwd": cwd, "mcpServers": mcp_servers}, timeout=timeout)
        return result["sessionId"]

    async def load_session(
        self, session_id: str, cwd: str, mcp_servers: list[dict[str, Any]], timeout: float = 300
    ) -> None:
        """Resume a conversation from the harness's own records (needs the
        agent's loadSession capability). The agent replays it as session/update
        notifications before answering."""
        await self.peer.request(
            "session/load", {"sessionId": session_id, "cwd": cwd, "mcpServers": mcp_servers}, timeout=timeout
        )

    async def prompt(self, session_id: str, text: str) -> tuple[str, str]:
        """Run one prompt turn. Returns (stopReason, the text the agent wrote)."""
        self._collect[session_id] = []
        self._cancelled.discard(session_id)
        try:
            result = await self.peer.request(
                "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}
            )
            return (result or {}).get("stopReason", "end_turn"), "".join(self._collect.get(session_id, []))
        finally:
            self._collect.pop(session_id, None)

    async def cancel(self, session_id: str) -> None:
        self._cancelled.add(session_id)
        await self.peer.notify("session/cancel", {"sessionId": session_id})

    async def close(self) -> None:
        await self.peer.close()

    async def _handle_notification(self, method: str, params: Any) -> None:
        if method != "session/update" or not isinstance(params, dict):
            return
        update = params.get("update") or {}
        if update.get("sessionUpdate") == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text" and params.get("sessionId") in self._collect:
                self._collect[params["sessionId"]].append(content.get("text", ""))
        if self._on_update:
            self._on_update(params)

    async def _handle_request(self, method: str, params: Any) -> Any:
        if method == "session/request_permission":
            if (params or {}).get("sessionId") in self._cancelled:
                outcome = {"outcome": {"outcome": "cancelled"}}
            else:
                outcome = decide_permission(params or {}, self.permissions)
            if self._on_permission:
                self._on_permission(params or {}, outcome)
            return outcome
        # fs/* and terminal/* were not advertised in initialize.
        raise RpcError(-32601, f"method not supported by harness-broker: {method}")

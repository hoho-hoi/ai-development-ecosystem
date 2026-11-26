"""The history tool: read-only SQL over the place's trace store.

agora sends its history as OpenTelemetry traces to Phoenix (the place's
choice, set in agora.yaml). Sessions do not talk to Phoenix: the history
tool relays two of Phoenix's MCP tools, describeSqlSchema and executeSql
(read-only), and nothing else Phoenix offers.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

PROTOCOL = "2025-06-18"


class HistoryError(RuntimeError):
    pass


class PhoenixHistory:
    """A minimal MCP client for Phoenix's /mcp (Streamable HTTP; replies come
    as JSON or as a server-sent event stream)."""

    def __init__(self, url: str, api_key: str | None = None, timeout: float = 120):
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self._session: str | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, frame: dict[str, Any]) -> tuple[Any, dict[str, str]]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        req = urllib.request.Request(self.url, data=json.dumps(frame).encode(), headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                body = r.read().decode()
                reply_headers = {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            raise HistoryError(f"the trace store answered {e.code}: {e.read().decode(errors='replace')[:300]}") from None
        except (urllib.error.URLError, OSError) as e:
            raise HistoryError(f"the trace store is unreachable at {self.url}: {e}") from None
        if not body.strip():
            return None, reply_headers
        if reply_headers.get("content-type", "").startswith("text/event-stream"):
            datas = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            msgs = [json.loads(d) for d in datas if d]
            reply = next((m for m in msgs if m.get("id") == frame.get("id")), msgs[-1] if msgs else None)
        else:
            reply = json.loads(body)
        return reply, reply_headers

    def _connect(self) -> None:
        self._session = None  # an expired session id must not go with the handshake
        self._id += 1
        reply, headers = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "agora", "version": "0"}}})
        if not reply or "error" in reply:
            raise HistoryError(f"the trace store refused the MCP handshake: {reply}")
        self._session = headers.get("mcp-session-id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call(self, tool: str, args: dict[str, Any]) -> Any:
        with self._lock:
            for attempt in (0, 1):
                if self._session is None or attempt:
                    self._connect()
                self._id += 1
                try:
                    reply, _ = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                                           "params": {"name": tool, "arguments": args}})
                except HistoryError:
                    if attempt:
                        raise
                    continue  # e.g. the MCP session expired: connect again once
                break
        if not reply:
            raise HistoryError("the trace store sent no answer")
        if "error" in reply:
            raise HistoryError(f"the trace store refused {tool}: {reply['error'].get('message')}")
        result = reply["result"]
        if result.get("structuredContent") is not None:
            data = result["structuredContent"]
        else:
            text = "".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
            try:
                data = json.loads(text)
            except ValueError:
                data = text
        if result.get("isError"):
            raise HistoryError(f"{tool} failed: {data}")
        return data

    def query(self, sql: str | None) -> Any:
        """Without sql: the SQL dialect and the tables that can be read. With
        sql: its rows (read-only; Phoenix limits rows and size)."""
        if not sql:
            return self._call("describeSqlSchema", {})
        return self._call("executeSql", {"sql": sql})

"""LLM gateway (LiteLLM Proxy): one virtual key per session.

The real provider keys live only in the gateway. When a session starts,
agora asks the gateway for a key tagged with the session, module and place,
gives it to the session together with the gateway URL, and deletes the key
when the session ends. The gateway records cost per key, so usage can be
read per session, module and version.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


class GatewayError(RuntimeError):
    pass


# How each harness is pointed at an Anthropic-compatible gateway.
HARNESS_ENV = {
    "claude-code": ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"),
}


class Gateway:
    def __init__(self, url: str, session_url: str, master_key_env: str):
        self.url = url.rstrip("/")
        self.session_url = session_url.rstrip("/")
        self.master_key_env = master_key_env

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        key = os.environ.get(self.master_key_env)
        if not key:
            raise GatewayError(f"environment variable {self.master_key_env} (the gateway's master key) is not set")
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except Exception as e:  # HTTPError, URLError, bad JSON
            raise GatewayError(f"gateway {path} failed: {e}") from None

    def mint(self, session_id: str, metadata: dict[str, Any]) -> str:
        res = self._post("/key/generate", {"key_alias": f"agora-{session_id}", "metadata": metadata})
        key = res.get("key")
        if not key:
            raise GatewayError("gateway returned no key")
        return str(key)

    def revoke(self, key: str) -> None:
        self._post("/key/delete", {"keys": [key]})

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
import urllib.parse
import urllib.request
from typing import Any


class GatewayError(RuntimeError):
    pass


# How each harness is pointed at an Anthropic-compatible gateway.
HARNESS_ENV = {
    "claude-code": ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"),
    # agora-pi reads both and keeps the key out of pi's tools' environment.
    "pi": ("AGORA_LLM_URL", "AGORA_LLM_KEY"),
}


class Gateway:
    def __init__(self, url: str, session_url: str, master_key_env: str):
        self.url = url.rstrip("/")
        self.session_url = session_url.rstrip("/")
        self.master_key_env = master_key_env

    def _post(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """POST `body`, or GET when it is None."""
        key = os.environ.get(self.master_key_env)
        if not key:
            raise GatewayError(f"environment variable {self.master_key_env} (the gateway's master key) is not set")
        req = urllib.request.Request(
            self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="GET" if body is None else "POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except Exception as e:  # HTTPError, URLError, bad JSON
            raise GatewayError(f"gateway {path} failed: {e}") from None

    def mint(self, session_id: str, metadata: dict[str, Any]) -> str:
        # No max_budget on the key: LiteLLM reserves each call's worst-case
        # cost against it, and refuses calls long before the budget is spent.
        res = self._post("/key/generate", {"key_alias": f"agora-{session_id}", "metadata": metadata})
        key = res.get("key")
        if not key:
            raise GatewayError("gateway returned no key")
        return str(key)

    def spend(self, key: str) -> float:
        """What the key has cost so far (USD), as the gateway recorded it. The
        gateway writes spend in batches, so the last few seconds may be missing."""
        res = self._post("/key/info?" + urllib.parse.urlencode({"key": key}), None)
        return float((res.get("info") or {}).get("spend") or 0.0)

    def revoke(self, key: str) -> None:
        self._post("/key/delete", {"keys": [key]})

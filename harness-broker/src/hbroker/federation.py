"""Places talking to places.

There is no center: every place is equal and talks only to the places in
its `peers` list (name, URL, public key). Every request carries the sending
place's name, a timestamp and its signature over method, path, timestamp and
body; the receiving place checks it against the key it was configured with.

Endpoints (served by daemon.py):
    POST /federation/deliver   a signed message for one of our residents
    POST /federation/quiet     wait until one of our residents can send nothing more
    POST /federation/call      a tool call made by someone at the calling place
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .identity import Key, request_bytes, verify

MAX_CLOCK_SKEW = 300  # seconds


class PlaceError(Exception):
    """A request to another place failed."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class Peer:
    name: str
    url: str
    public_key: str


class Federation:
    def __init__(self, name: str, key: Key, peers: list[Peer] | None = None):
        self.name = name
        self.key = key
        self.peers: dict[str, Peer] = {p.name: p for p in peers or []}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def add_peer(self, peer: Peer) -> None:
        self.peers[peer.name] = peer

    def peer(self, name: str) -> Peer:
        if name not in self.peers:
            raise PlaceError(f"unknown place {name!r}; known: {sorted(self.peers) or 'none'}", status=404)
        return self.peers[name]

    # --- signing -------------------------------------------------------------
    def sign_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        return {
            "X-HB-Place": self.name,
            "X-HB-Time": ts,
            "X-HB-Signature": self.key.sign(request_bytes(method, path, ts, body)),
        }

    def verify_request(self, headers: Mapping[str, str], method: str, path: str, body: bytes) -> str:
        """The name of the trusted place that signed this request, or PermissionError."""
        name = headers.get("x-hb-place", "")
        ts = headers.get("x-hb-time", "")
        sig = headers.get("x-hb-signature", "")
        peer = self.peers.get(name)
        if peer is None:
            raise PermissionError(f"place {name!r} is not trusted here")
        try:
            skew = abs(time.time() - int(ts))
        except ValueError:
            raise PermissionError("bad timestamp") from None
        if skew > MAX_CLOCK_SKEW:
            raise PermissionError(f"request time is {skew:.0f} s off")
        if not verify(peer.public_key, sig, request_bytes(method, path, ts, body)):
            raise PermissionError(f"bad signature from place {name!r}")
        return name

    # --- client ---------------------------------------------------------------
    def _post_sync(self, place: str, path: str, payload: dict[str, Any], timeout: float) -> Any:
        peer = self.peer(place)
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", **self.sign_headers("POST", path, body)}
        req = urllib.request.Request(peer.url.rstrip("/") + path, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("error")
            except Exception:
                detail = e.reason
            raise PlaceError(f"place {place}: {detail}", status=e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise PlaceError(f"place {place} unreachable at {peer.url}: {e}") from None

    async def post(self, place: str, path: str, payload: dict[str, Any], timeout: float = 60) -> Any:
        return await asyncio.to_thread(self._post_sync, place, path, payload, timeout)

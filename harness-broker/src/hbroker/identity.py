"""Identity: keys, self-certifying session ids, addresses, signatures.

A person (session) has an Ed25519 key pair. Its id is derived from the
public key, so anyone holding a message, its signature and the sender's
public key can check who sent it without trusting the place that delivered
it. The private key is kept by the place the person lives in; the agent in
the container never sees it.

A place (node) has a key pair too. It signs its requests to other places,
and a place only talks to the places listed in its config with their public
keys.

Addresses are `ref@place`: ref is a session id (or a name, resolved by that
place) and place is a place name. A ref without `@place` means "here".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

PLACE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
OPERATOR = "operator"


class Key:
    def __init__(self, private: Ed25519PrivateKey):
        self._private = private
        self.public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @classmethod
    def generate(cls) -> "Key":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_hex(cls, value: str) -> "Key":
        return cls(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(value)))

    def private_hex(self) -> str:
        return self._private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()

    @property
    def public_hex(self) -> str:
        return self.public.hex()

    def sign(self, data: bytes) -> str:
        return self._private.sign(data).hex()


def verify(public_hex: str, signature_hex: str, data: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(bytes.fromhex(signature_hex), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def session_id_for(public: bytes) -> str:
    """Self-certifying id: anyone can check that a public key belongs to it."""
    return "s-" + hashlib.sha256(public).hexdigest()[:16]


def load_or_create_key(path: Path) -> Key:
    """A key kept in a 0600 file (the place's own key)."""
    if path.exists():
        return Key.from_hex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Key.generate()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.private_hex() + "\n")
    return key


def split_address(address: str) -> tuple[str, str | None]:
    """'ref@place' -> (ref, place); 'ref' -> (ref, None)."""
    ref, sep, place = address.rpartition("@")
    if not sep:
        return address, None
    if not ref or not place:
        raise ValueError(f"not an address: {address!r}")
    return ref, place


def message_bytes(envelope: dict[str, Any]) -> bytes:
    """What a sender signs: the message fields, canonically encoded."""
    fields = {k: envelope.get(k) for k in ("id", "from", "to", "thread_id", "reply_to", "body", "sent_at")}
    return json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def request_bytes(method: str, path: str, timestamp: str, body: bytes) -> bytes:
    """What a place signs for a request to another place."""
    return f"{method}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}".encode()

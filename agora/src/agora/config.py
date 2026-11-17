"""Broker configuration (agora.yaml)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .identity import PLACE_NAME_RE
from .spec import HarnessOverride, ModuleSource, Network

DEFAULT_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "agora"


class DockerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binary: str = "docker"
    network: str = "agora-net"
    # Building harness images (images/<harness>/Dockerfile): extra build
    # arguments (e.g. BASE_IMAGE) and the network to build on.
    build_args: dict[str, str] = Field(default_factory=dict)
    build_network: str | None = None


class GatewaySettings(BaseModel):
    """An LLM gateway (LiteLLM Proxy) that holds the provider keys. agora gets
    a virtual key per session from it."""

    model_config = ConfigDict(extra="forbid")
    url: str  # as agora reaches it
    session_url: str | None = None  # as sessions reach it (default: url)
    master_key_env: str = "LITELLM_MASTER_KEY"  # environment variable holding the gateway's master key


class TelemetrySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    otlp_endpoint: str  # OTLP/HTTP traces endpoint, e.g. http://127.0.0.1:6006/v1/traces


class PeerSettings(BaseModel):
    """Another place this one trusts: its name, where it listens, its key."""

    model_config = ConfigDict(extra="forbid")
    name: str
    url: str
    public_key: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not PLACE_NAME_RE.match(v):
            raise ValueError(f"place name {v!r} must be lowercase letters, digits and '-'")
        return v

    @field_validator("public_key")
    @classmethod
    def _key(cls, v: str) -> str:
        try:
            if len(bytes.fromhex(v)) != 32:
                raise ValueError
        except ValueError:
            raise ValueError("public_key must be 64 hex characters (an Ed25519 public key; see `agora place`)") from None
        return v.lower()


def default_place_name() -> str:
    import re
    import socket

    name = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().lower()).strip("-")
    return (name or "place")[:63]


class BrokerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # This place's name: the part after @ in its residents' addresses.
    place: str = Field(default_factory=default_place_name)
    # Places this one talks to. There is no center; each place lists its peers.
    peers: list[PeerSettings] = Field(default_factory=list)
    # The URL other places reach this one at (shown by `agora place`); listen_host
    # must then be reachable from them, e.g. 0.0.0.0 or a VPN address.
    public_url: str | None = None
    state_dir: Path = DEFAULT_STATE_DIR
    # The modules that can be started here; their plugin.json says how they run.
    modules: list[ModuleSource] = Field(default_factory=list)
    # How containers reach the outside (e.g. an HTTP proxy), for every session.
    network: Network = Field(default_factory=Network)
    # Per harness: a prebuilt image to use, or the ACP command (local runner).
    harnesses: dict[str, HarnessOverride] = Field(default_factory=dict)
    # LLM gateway giving each session a virtual key (optional).
    llm_gateway: GatewaySettings | None = None
    # Where session traces go (optional).
    telemetry: TelemetrySettings | None = None
    listen_host: str = "127.0.0.1"
    listen_port: int = 8765
    runner: Literal["docker", "local"] = "docker"
    # The local runner gives agents no isolation; it must be opted into.
    allow_local_runner: bool = False
    # Broker URL as seen from inside session containers. The broker must
    # listen on an address the containers can reach (e.g. the docker bridge
    # gateway) for this to work.
    container_broker_url: str | None = None
    docker: DockerSettings = Field(default_factory=DockerSettings)
    # Global caps.
    max_sessions: int = 8
    max_spawn_depth: int = 2
    keep_bundles: bool = False
    # The broker's own Codex login (a CODEX_HOME holding auth.json), used by
    # runtimes with login: chatgpt. Default: <state_dir>/codex-login.
    codex_login_dir: Path | None = None

    # Directory that relative paths (local modules) are resolved against.
    base_dir: Path = Field(default_factory=Path.cwd, exclude=True)

    @field_validator("place")
    @classmethod
    def _place(cls, v: str) -> str:
        if not PLACE_NAME_RE.match(v):
            raise ValueError(f"place name {v!r} must be lowercase letters, digits and '-'")
        return v

    @property
    def codex_login_path(self) -> Path:
        return self.codex_login_dir or self.state_dir / "codex-login"

    @property
    def history_dir(self) -> Path:
        return self.state_dir / "history"

    @property
    def key_file(self) -> Path:
        return self.state_dir / "broker.key"

    @property
    def place_key_file(self) -> Path:
        return self.state_dir / "place.key"

    @property
    def local_url(self) -> str:
        host = "127.0.0.1" if self.listen_host in ("0.0.0.0", "::") else self.listen_host
        return f"http://{host}:{self.listen_port}"

    def runtime_broker_url(self) -> str:
        if self.runner == "local":
            return self.local_url
        return self.container_broker_url or f"http://host.docker.internal:{self.listen_port}"


def load_config(path: str | Path | None) -> BrokerConfig:
    if path is None:
        return BrokerConfig()
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = BrokerConfig.model_validate(data)
    # Relative paths in the config are relative to the config file.
    cfg.base_dir = path.parent.resolve()
    if not cfg.state_dir.is_absolute():
        cfg.state_dir = (path.parent / cfg.state_dir).resolve()
    if cfg.codex_login_dir is not None:
        cfg.codex_login_dir = cfg.codex_login_dir.expanduser()
        if not cfg.codex_login_dir.is_absolute():
            cfg.codex_login_dir = (path.parent / cfg.codex_login_dir).resolve()
    return cfg


def ensure_broker_key(cfg: BrokerConfig) -> str:
    """The broker key authorizes the operator CLI and registration of
    external sessions. It lives in a 0600 file in the state dir."""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if cfg.key_file.exists():
        return cfg.key_file.read_text().strip()
    key = secrets.token_urlsafe(32)
    fd = os.open(cfg.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key

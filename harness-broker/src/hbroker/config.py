"""Broker configuration (hb.yaml)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "harness-broker"


class DockerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binary: str = "docker"
    network: str = "hb-net"


class BrokerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_dir: Path = DEFAULT_STATE_DIR
    profiles_dir: Path = Path("profiles")
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
    # profiles with runtime.login: chatgpt. Default: <state_dir>/codex-login.
    codex_login_dir: Path | None = None

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
    if not cfg.profiles_dir.is_absolute():
        cfg.profiles_dir = (path.parent / cfg.profiles_dir).resolve()
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

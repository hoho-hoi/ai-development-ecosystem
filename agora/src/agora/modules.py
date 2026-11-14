"""Modules: harness-independent agent definitions that profiles bind to a runtime.

A module is an Agent Plugins (v1.0.0) package: a directory with a root
plugin.json. agora reads the portable parts as the spec defines them and keeps
what the spec does not cover in its own extension namespace (NAMESPACE):

    my-module/
    ├── plugin.json            {"$schema": ".../plugin.schema.json", "name": "akinator",
    │                           "description": "...", "extensions": {NAMESPACE: {...}}}
    ├── skills/<name>/SKILL.md Agent Skills, immediate children of skills/ only
    ├── mcp.json               optional MCP servers {"$schema": ..., "mcpServers": {...}}
    └── <NAMESPACE>/AGENTS.md  the agent's instructions (default; see AgoraExtension)

The module says what the agent is. The profile says how it runs (harness,
model, image, permissions). A profile points at a module either pinned in a
git repository (repo + full commit SHA) or, for development, in a local
directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PLUGIN_FILE = "plugin.json"
MCP_FILE = "mcp.json"
SKILLS_DIR = "skills"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
# agora's Agent Plugins extension namespace: plugin.json's extensions[NAMESPACE]
# and the directory of the same name at the module root.
NAMESPACE = "io.github.hoho-hoi.agora"
ROOT_PLACEHOLDER = "${PLUGIN_ROOT}"
RESERVED_MCP_NAMES = {"agora"}  # the bridge's server name in every session

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")


class ModuleError(ValueError):
    pass


class _Closed(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class AgoraExtension(_Closed):
    """plugin.json's extensions[NAMESPACE]. Paths are relative to the
    NAMESPACE directory."""

    instructions: str | None = None

    @field_validator("instructions")
    @classmethod
    def _relative(cls, v: str | None) -> str | None:
        return check_relative(v, "instructions") if v is not None else v


class PluginManifest(_Closed):
    """plugin.json (Agent Plugins 1.0.0). The schema is closed."""

    schema_: str = Field(alias="$schema")
    name: str
    version: str | None = None
    description: str = ""
    author: Any = None
    homepage: str | None = None
    repository: Any = None
    license: str | None = None
    keywords: list[str] = Field(default_factory=list)
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("schema_")
    @classmethod
    def _schema(cls, v: str) -> str:
        if v != PLUGIN_SCHEMA:
            raise ValueError(f"unsupported $schema {v!r} (supported: {PLUGIN_SCHEMA})")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v) or "--" in v or ".." in v:
            raise ValueError(f"invalid plugin name {v!r} (1-64 of a-z 0-9 . -, no -- or ..)")
        return v

    @property
    def agora(self) -> AgoraExtension:
        return AgoraExtension.model_validate(self.extensions.get(NAMESPACE, {}))


class McpServerDef(_Closed):
    """One entry of mcp.json's mcpServers."""

    type: Literal["stdio", "http", "streamable-http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class McpManifest(_Closed):
    schema_: str = Field(alias="$schema")
    mcp_servers: dict[str, McpServerDef] = Field(default_factory=dict, alias="mcpServers")

    @field_validator("schema_")
    @classmethod
    def _schema(cls, v: str) -> str:
        if v != MCP_SCHEMA:
            raise ValueError(f"unsupported $schema {v!r} (supported: {MCP_SCHEMA})")
        return v


def check_relative(path: str, what: str) -> str:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not str(p).strip():
        raise ValueError(f"{what} must be a relative path inside the module: {path!r}")
    return path


def _skill_name(skill_md: Path) -> str | None:
    """The name: field of a SKILL.md front matter."""
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key.strip() == "name":
            return value.strip().strip("'\"")
    return None


@dataclass
class ResolvedModule:
    directory: Path
    plugin: PluginManifest
    source: dict[str, Any]
    files: dict[str, str] = field(default_factory=dict)  # relative path -> sha256
    mcp: McpManifest | None = None

    def _inside(self, rel: str) -> Path:
        p = (self.directory / rel).resolve()
        if not p.is_relative_to(self.directory.resolve()):
            raise ModuleError(f"{rel!r} points outside the module")
        return p

    def instructions_text(self) -> str | None:
        ext = self.plugin.agora
        rel = f"{NAMESPACE}/{ext.instructions or 'AGENTS.md'}"
        p = self._inside(rel)
        if not p.is_file():
            if ext.instructions:
                raise ModuleError(f"module instructions not found: {rel}")
            return None
        return p.read_text(encoding="utf-8")

    def skill_dirs(self) -> list[Path]:
        root = self._inside(SKILLS_DIR)
        if not root.is_dir():
            return []
        dirs = sorted(d for d in root.iterdir() if d.is_dir())
        for d in dirs:
            self._inside(str(d.relative_to(self.directory)))  # a symlinked skill must not leave the module
            md = d / "SKILL.md"
            if not md.is_file():
                raise ModuleError(f"skill {d.name} has no SKILL.md")
            if _skill_name(md) != d.name:
                raise ModuleError(f"skill {d.name}: SKILL.md name must match its directory name")
        return dirs

    def mcp_servers(self, root: str) -> list[dict[str, Any]]:
        """mcp.json's servers as ACP McpServer objects, with the module at
        `root` (its path inside the session)."""
        if not self.mcp:
            return []

        def sub(v: str) -> str:
            return v.replace(ROOT_PLACEHOLDER, root)

        out: list[dict[str, Any]] = []
        for name, d in self.mcp.mcp_servers.items():
            if d.type == "stdio":
                cmd = sub(d.command or "")
                if cmd.startswith("./"):
                    cmd = f"{root}/{cmd[2:]}"
                out.append({"name": name, "command": cmd, "args": [sub(a) for a in d.args],
                            "env": [{"name": k, "value": sub(v)} for k, v in d.env.items()]})
            else:
                out.append({"type": "http", "name": name, "url": sub(d.url or ""),
                            "headers": [{"name": k, "value": v} for k, v in d.headers.items()]})
        return out

    def check_mcp(self) -> None:
        if not self.mcp:
            return
        for name, d in self.mcp.mcp_servers.items():
            where = f"{MCP_FILE}: server {name!r}"
            if name in RESERVED_MCP_NAMES:
                raise ModuleError(f"{where}: the name is reserved for the agora bridge")
            if d.type == "stdio":
                if not d.command or d.url:
                    raise ModuleError(f"{where}: a stdio server needs command and no url")
                if any(c.isspace() for c in d.command):
                    raise ModuleError(f"{where}: command must be a single executable, not a command line")
                if d.command.startswith("./"):
                    check_relative(d.command[2:], where)
                    self._inside(d.command[2:])
            else:
                if not d.url or d.command:
                    raise ModuleError(f"{where}: an http server needs url and no command")
                if not d.url.startswith("https://"):
                    raise ModuleError(f"{where}: a remote server must use https")

    def manifest(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "plugin": self.plugin.model_dump(by_alias=True, exclude_none=True),
            "files": self.files,
        }


def hash_files(directory: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file() and ".git" not in p.relative_to(directory).parts:
            out[str(p.relative_to(directory))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _read_json(f: Path, model: type[BaseModel]) -> Any:
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ModuleError(f"{f}: {e}") from None
    if not isinstance(data, dict):
        raise ModuleError(f"{f}: must be a JSON object")
    if model is McpManifest:
        # A server that names a cwd cannot be honoured: ACP gives MCP servers none.
        for name, d in (data.get("mcpServers") or {}).items():
            if isinstance(d, dict) and "cwd" in d:
                raise ModuleError(f"{f}: server {name!r}: cwd is not supported; use {ROOT_PLACEHOLDER} in paths")
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise ModuleError(f"{f}: {e}") from None


def read_plugin(directory: Path) -> PluginManifest:
    f = directory / PLUGIN_FILE
    if not f.is_file():
        if (directory / "module.json").is_file():
            raise ModuleError(
                f"{directory}: module.json is no longer supported; describe the module with an "
                f"Agent Plugins {PLUGIN_FILE} (instructions go in {NAMESPACE}/AGENTS.md)"
            )
        raise ModuleError(f"{PLUGIN_FILE} not found in {directory}")
    plugin = _read_json(f, PluginManifest)
    try:
        plugin.agora  # validate agora's own extension
    except ValidationError as e:
        raise ModuleError(f"{f}: extensions[{NAMESPACE!r}]: {e}") from None
    return plugin


def load_local(directory: Path, source: dict[str, Any]) -> ResolvedModule:
    if not directory.is_dir():
        raise ModuleError(f"module directory not found: {directory}")
    plugin = read_plugin(directory)
    mcp_file = directory / MCP_FILE
    mcp = _read_json(mcp_file, McpManifest) if mcp_file.is_file() else None
    mod = ResolvedModule(directory=directory, plugin=plugin, source=source, files=hash_files(directory), mcp=mcp)
    mod.instructions_text()  # fail early on a broken module
    mod.skill_dirs()
    mod.check_mcp()
    return mod


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ModuleError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


class ModuleCache:
    """Checkouts of pinned module repositories, one per (repo, commit), kept
    under the broker's state dir and reused across sessions."""

    def __init__(self, root: Path):
        self.root = root

    def checkout_dir(self, repo: str, ref: str) -> Path:
        return self.root / hashlib.sha256(repo.encode()).hexdigest()[:16] / ref

    def cached(self, repo: str, ref: str) -> Path | None:
        # The marker sits beside the checkout, so it is never part of a module.
        d = self.checkout_dir(repo, ref)
        return d if d.with_name(f"{ref}.complete").exists() else None

    def fetch(self, repo: str, ref: str) -> Path:
        done = self.cached(repo, ref)
        if done:
            return done
        dest = self.checkout_dir(repo, ref)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f".{ref[:12]}-", dir=dest.parent))
        try:
            _git("clone", "--quiet", "--no-checkout", "--template=", repo, str(tmp / "co"))
            co = tmp / "co"
            try:
                _git("checkout", "--quiet", "--detach", ref, cwd=co)
            except ModuleError:
                # Shallow servers may need the commit fetched explicitly.
                _git("fetch", "--quiet", "--depth", "1", "origin", ref, cwd=co)
                _git("checkout", "--quiet", "--detach", ref, cwd=co)
            head = _git("rev-parse", "HEAD", cwd=co)
            if head != ref:
                raise ModuleError(f"checked out {head}, expected {ref}")
            shutil.rmtree(co / ".git")
            if not dest.exists():  # else another fetch won the race
                co.rename(dest)
            dest.with_name(f"{ref}.complete").write_text(ref)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return dest

    def resolve(self, ref_spec: Any, base: Path | None) -> ResolvedModule:
        """ref_spec is a profiles.ModuleRef."""
        if ref_spec.repo:
            root = self.fetch(ref_spec.repo, ref_spec.ref)
            source = {"repo": ref_spec.repo, "ref": ref_spec.ref, "path": ref_spec.path}
        else:
            local = Path(ref_spec.local)
            root = local if local.is_absolute() else ((base or Path.cwd()) / local).resolve()
            source = {"local": str(root), "path": ref_spec.path}
        return load_local((root / ref_spec.path).resolve(), source)


SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

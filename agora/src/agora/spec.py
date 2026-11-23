"""What agora runs: modules and their runtime settings.

A module (an Agent Plugins package, see modules.py) says everything about how
its sessions run, in plugin.json's agora extension: candidate runtimes
(harness, harness version, model, environment), the agora tools it gets,
whether the harness's built-in tools are on, its session mode and limits.
The place (agora.yaml) adds only what belongs to the machine: which modules
are registered here, the network, and how to obtain a harness image.

`Catalog` lists the registered modules. `Spec` is one module resolved for a
spawn: the runtime chosen for this place, the workspace given by the caller,
and a fingerprint over all of it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .modules import (
    AgoraExtension,
    ModuleCache,
    ResolvedModule,
    RuntimeOption,
    check_relative,
    load_local,
)

# Files and directories removed from every workspace, at any depth. These are
# where harnesses pick up project-level instructions, skills, MCP servers and
# settings.
DEFAULT_STRIP: tuple[str, ...] = (
    "CLAUDE.md",
    "CLAUDE.local.md",
    ".claude",
    ".mcp.json",
    "AGENTS.md",
    "AGENTS.override.md",
    ".codex",
    "GEMINI.md",
    ".gemini",
    ".opencode",
    ".pi",
    ".agents",
)

COMMIT_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")  # SHA-1 or SHA-256 object ids
ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Tools the broker offers to sessions, in the order the bridge lists them.
BROKER_TOOLS = ("send", "receive", "list_modules", "spawn", "clone", "list_sessions", "status", "terminate", "history")

# How agora starts each harness: the ACP command inside the session, and the
# build argument that pins the harness version in images/<harness>/Dockerfile.
HARNESSES: dict[str, dict[str, Any]] = {
    "claude-code": {"adapter": ["claude-agent-acp"], "version_arg": "CLAUDE_AGENT_ACP_VERSION"},
    "codex": {"adapter": ["codex-acp"], "version_arg": "CODEX_ACP_VERSION"},
    "opencode": {"adapter": ["opencode", "acp"], "version_arg": "OPENCODE_VERSION"},
    "pi": {"adapter": ["agora-pi"], "version_arg": "PI_VERSION"},
}

# Claude Code tools turned off when a module has builtinTools: false.
CLAUDE_BUILTINS = (
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit", "TodoWrite",
)


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ModuleSource(_Model):
    """A registered module: a git repository pinned to a commit, or a local
    directory (development; its files are fingerprinted when loaded)."""

    repo: str | None = None
    ref: str | None = None
    local: str | None = None
    path: str = "."  # the module directory inside the repo; default its root

    @model_validator(mode="after")
    def _one_source(self) -> "ModuleSource":
        if bool(self.repo) == bool(self.local):
            raise ValueError("a module needs exactly one of repo (+ ref) or local")
        if self.repo and not (self.ref and COMMIT_RE.match(self.ref)):
            raise ValueError("ref must be a full commit SHA when repo is set; branches and tags are rejected")
        if self.local and self.ref:
            raise ValueError("ref is only valid with repo")
        check_relative(self.path, "path")
        return self


class Workspace(_Model):
    """The repository a session works in; given per spawn."""

    repo: str | None = None
    ref: str | None = None
    strip: list[str] = Field(default_factory=lambda: list(DEFAULT_STRIP))

    @model_validator(mode="after")
    def _ref_pinned(self) -> "Workspace":
        if self.repo and not self.ref:
            raise ValueError("workspace.ref is required when workspace.repo is set")
        if self.ref and not COMMIT_RE.match(self.ref):
            raise ValueError("workspace.ref must be a full commit SHA; branches and tags are rejected")
        return self


class HarnessOverride(_Model):
    """Place-level: how this machine provides a harness (e.g. a prebuilt
    image, or a different ACP command for the local runner)."""

    image: str | None = None
    adapter: list[str] | None = None


class Network(_Model):
    mode: Literal["bridge", "none"] = "bridge"
    proxy: str | None = None


@dataclass
class Permissions:
    """How the broker answers ACP permission requests. Always allow, unless
    the module turned the harness's built-in tools off."""

    mode: str = "allow"
    allow_kinds: list[str] | None = None
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)


@dataclass
class Runtime:
    """The runtime chosen for a session, as the rest of agora reads it."""

    harness: str
    version: str
    model: str
    adapter: list[str]
    image: str | None  # a place-provided image; else the runner builds harness@version
    env: dict[str, str]  # literal values
    secret_env: dict[str, str]  # container variable -> host variable it is read from
    login: str | None
    session_mode: str
    broker_tools: list[str]
    builtin_tools: bool
    permissions: Permissions
    option_index: int  # which entry of the module's runtimes was chosen

    @property
    def secrets(self) -> list[str]:
        return list(self.secret_env)

    def secret_values(self) -> dict[str, str]:
        return {name: os.environ[host] for name, host in self.secret_env.items() if host in os.environ}


@dataclass
class Limits:
    max_runtime_minutes: int
    max_spawn_depth: int
    max_concurrent: int
    memory: str | None = None
    cpus: float | None = None
    pids: int = 512


class SpecError(ValueError):
    pass


@dataclass
class Spec:
    """One module, resolved for one spawn at this place."""

    module: ResolvedModule
    source: ModuleSource
    runtime: Runtime
    limits: Limits
    spawnable_by: list[str]
    network: Network
    workspace: Workspace
    fingerprint: str = ""

    @property
    def id(self) -> str:
        return self.module.plugin.name

    @property
    def version(self) -> str:
        return self.module.plugin.version or "0"

    def resolved_dump(self) -> dict[str, Any]:
        rt = self.runtime
        return {
            "module": self.id,
            "version": self.version,
            "source": self.source.model_dump(exclude_none=True),
            "runtime": {
                "harness": rt.harness, "version": rt.version, "model": rt.model, "adapter": rt.adapter,
                "image": rt.image, "env": rt.env, "secret_env": rt.secret_env, "login": rt.login,
                "sessionMode": rt.session_mode, "tools": rt.broker_tools, "builtinTools": rt.builtin_tools,
                "option": rt.option_index,
            },
            "limits": self.limits.__dict__,
            "spawnableBy": self.spawnable_by,
            "network": self.network.model_dump(),
            "workspace": self.workspace.model_dump(exclude_none=True),
            "_fingerprint": self.fingerprint,
        }


def card(mod: ResolvedModule | None, source: ModuleSource, error: str | None = None) -> dict[str, Any]:
    """What list_modules returns for a module (Agent Card-like fields)."""
    if mod is None:
        return {"id": None, "source": source.model_dump(exclude_none=True), "error": error}
    ext = mod.plugin.agora
    return {
        "id": mod.plugin.name,
        "name": mod.plugin.name,
        "description": mod.plugin.description,
        "version": mod.plugin.version,
        "runtimes": [
            {"harness": r.harness, "version": r.version, "model": r.model} for r in ext.runtimes
        ],
        "source": source.model_dump(exclude_none=True),
    }


def _choose_runtime(ext: AgoraExtension, harnesses: dict[str, HarnessOverride]) -> tuple[int, RuntimeOption]:
    """The first runtime whose harness agora knows and whose ${VAR}
    references are all set in this place's environment."""
    if not ext.runtimes:
        raise SpecError("the module declares no runtimes (extensions.<agora>.runtimes)")
    reasons = []
    for i, opt in enumerate(ext.runtimes):
        if opt.harness not in HARNESSES:
            reasons.append(f"#{i} {opt.harness}: unknown harness")
            continue
        missing = sorted({v for value in opt.env.values() for v in ENV_REF_RE.findall(value)} - set(os.environ))
        if missing:
            reasons.append(f"#{i} {opt.harness}/{opt.model}: environment variable(s) not set here: {', '.join(missing)}")
            continue
        return i, opt
    raise SpecError("no runtime of the module can run at this place: " + "; ".join(reasons))


def resolve(
    mod: ResolvedModule,
    source: ModuleSource,
    *,
    workspace: Workspace | None = None,
    network: Network | None = None,
    harnesses: dict[str, HarnessOverride] | None = None,
) -> Spec:
    harnesses = harnesses or {}
    ext = mod.plugin.agora
    index, opt = _choose_runtime(ext, harnesses)
    literal: dict[str, str] = {}
    secret_env: dict[str, str] = {}
    for name, value in opt.env.items():
        refs = ENV_REF_RE.findall(value)
        if not refs:
            literal[name] = value
        elif ENV_REF_RE.fullmatch(value):
            secret_env[name] = refs[0]
        else:
            raise SpecError(f"env {name}: a value is either literal or exactly one ${{VAR}} reference")
    override = harnesses.get(opt.harness) or HarnessOverride()
    builtin = ext.builtin_tools
    tools = list(ext.tools)
    perms = Permissions(
        mode="allow" if builtin else "deny",
        denied_tools=[] if builtin else list(CLAUDE_BUILTINS),
    )
    runtime = Runtime(
        harness=opt.harness,
        version=opt.version,
        model=opt.model,
        adapter=list(override.adapter or HARNESSES[opt.harness]["adapter"]),
        image=override.image,
        env=literal,
        secret_env=secret_env,
        login=opt.login,
        session_mode=ext.session_mode,
        broker_tools=tools,
        builtin_tools=builtin,
        permissions=perms,
        option_index=index,
    )
    lim = ext.limits
    spec = Spec(
        module=mod,
        source=source,
        runtime=runtime,
        limits=Limits(lim.max_runtime_minutes, lim.max_spawn_depth, lim.max_concurrent),
        spawnable_by=list(ext.spawnable_by),
        network=network or Network(),
        workspace=workspace or Workspace(),
    )
    h = hashlib.sha256(json.dumps(spec.resolved_dump(), sort_keys=True).encode())
    h.update(json.dumps(mod.files, sort_keys=True).encode())
    spec.fingerprint = "sha256:" + h.hexdigest()
    return spec


class Catalog:
    """The modules registered at this place (agora.yaml `modules`), loaded on
    every lookup so edits to local modules take effect without a restart."""

    def __init__(self, sources: list[ModuleSource], cache: ModuleCache, base: Path | None = None,
                 network: Network | None = None, harnesses: dict[str, HarnessOverride] | None = None):
        self.sources = sources
        self.cache = cache
        self.base = base
        self.network = network or Network()
        self.harnesses = harnesses or {}

    def _load(self, source: ModuleSource) -> ResolvedModule:
        return self.cache.resolve(source, self.base)

    def _load_all(self, fetch: bool) -> tuple[dict[str, tuple[ModuleSource, ResolvedModule]], dict[str, str]]:
        found: dict[str, tuple[ModuleSource, ResolvedModule]] = {}
        errors: dict[str, str] = {}
        for src in self.sources:
            key = src.local or f"{src.repo}@{(src.ref or '')[:12]}"
            key = f"{key}:{src.path}" if src.path != "." else key
            if src.repo and not fetch and not self.cache.cached(src.repo, src.ref or ""):
                errors[key] = "not fetched yet"
                continue
            try:
                mod = self._load(src)
            except Exception as e:  # report, keep the rest usable
                errors[key] = str(e)
                continue
            if mod.plugin.name in found:
                errors[key] = f"duplicate module name {mod.plugin.name!r}"
                continue
            found[mod.plugin.name] = (src, mod)
        return found, errors

    def cards(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        found, errors = self._load_all(fetch=True)
        return [card(mod, src) for src, mod in found.values()], errors

    def names(self) -> list[str]:
        return list(self._load_all(fetch=True)[0])

    def errors(self) -> dict[str, str]:
        return self._load_all(fetch=True)[1]

    def get(self, name: str, *, module_ref: str | None = None, module_path: str | None = None,
            workspace: Workspace | None = None) -> Spec:
        """A registered module; with module_ref, another commit of its repository;
        with module_path, the module at another path of its repository (e.g. a
        module being written, not registered yet)."""
        found, errors = self._load_all(fetch=True)
        if name not in found:
            hint = f" (load errors: {errors})" if errors else ""
            raise KeyError(f"unknown module {name!r}{hint}")
        src, mod = found[name]
        if module_ref or module_path:
            if not src.repo:
                raise SpecError("module_ref and module_path need a module registered from a repository")
            if module_ref and not COMMIT_RE.match(module_ref):
                raise SpecError("module_ref must be a full commit SHA")
            if module_path:
                check_relative(module_path, "module_path")
            src = src.model_copy(update={"ref": module_ref or src.ref, "path": module_path or src.path})
            mod = self._load(src)
            if not module_path and mod.plugin.name != name:
                raise SpecError(f"module_ref {module_ref[:12]} holds module {mod.plugin.name!r}, not {name!r}")
        return resolve(mod, src, workspace=workspace, network=self.network, harnesses=self.harnesses)

    def from_directory(self, directory: Path, origin: dict[str, Any], *, workspace: Workspace | None = None) -> Spec:
        """A module that is not registered: a copy of a directory of a session's
        workspace (a custom module). `origin` says where it came from."""
        mod = load_local(directory, origin)
        return resolve(mod, ModuleSource(local=str(directory)), workspace=workspace, network=self.network,
                       harnesses=self.harnesses)


def load_module_dir(path: str | Path) -> ResolvedModule:
    """A module directory on its own (agora modules / tests)."""
    p = Path(path).resolve()
    return load_local(p, {"local": str(p)})

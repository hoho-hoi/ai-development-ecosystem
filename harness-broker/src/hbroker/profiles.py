"""Profile schema and loading.

A profile is an A2A Agent Card (id, name, description, version, skills) plus
the fields needed to run it reproducibly: runtime, workspace, network, limits.
Relative paths inside a profile are resolved against the profile file.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

# Files and directories removed from every workspace, at any depth, unless the
# profile overrides the list. These are where harnesses pick up project-level
# instructions, skills, MCP servers and settings.
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
)

# name@sha256:<digest> (registry) or sha256:<image id> (local image)
DIGEST_RE = re.compile(r"^([^\s@]+@)?sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")  # SHA-1 or SHA-256 object ids
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

Harness = Literal["claude-code", "codex", "gemini", "custom"]


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class Skill(_Model):
    id: str
    description: str = ""


class McpServer(_Model):
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class Permissions(_Model):
    # How the broker answers ACP session/request_permission for this profile.
    mode: Literal["allow", "deny"] = "allow"
    # ACP tool kinds that are allowed when mode is "allow" (None = all kinds).
    allow_kinds: list[str] | None = Field(default=None, alias="allowKinds")
    # Harness-native allow list, written into the harness settings
    # (Claude Code: settings.json permissions.allow).
    allowed_tools: list[str] = Field(default_factory=list, alias="allowedTools")


class Runtime(_Model):
    harness: Harness
    adapter: list[str]
    model: str
    image: str
    instructions: str | None = None
    bundled_skills: list[str] = Field(default_factory=list, alias="bundledSkills")
    mcp_servers: list[McpServer] = Field(default_factory=list, alias="mcpServers")
    permissions: Permissions = Field(default_factory=Permissions)
    # Names of environment variables passed through from the broker's
    # environment (never their values in argv or history).
    secrets: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("adapter", mode="before")
    @classmethod
    def _split_adapter(cls, v: Any) -> Any:
        if isinstance(v, str):
            return shlex.split(v)
        return v

    @field_validator("adapter")
    @classmethod
    def _adapter_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("runtime.adapter must name a command")
        return v

    @field_validator("image")
    @classmethod
    def _image_pinned(cls, v: str) -> str:
        if not DIGEST_RE.match(v):
            raise ValueError("runtime.image must be pinned by digest (name@sha256:<64 hex> or sha256:<image id>); tags are rejected")
        return v

    @field_validator("secrets")
    @classmethod
    def _secret_names(cls, v: list[str]) -> list[str]:
        for name in v:
            if not re.match(r"^[A-Z_][A-Z0-9_]*$", name):
                raise ValueError(f"secret {name!r} must be an environment variable name")
        return v


class Workspace(_Model):
    repo: str | None = None
    ref: str | None = None
    strip: list[str] = Field(default_factory=lambda: list(DEFAULT_STRIP))
    # Paths added to the default strip list instead of replacing it.
    strip_extra: list[str] = Field(default_factory=list, alias="stripExtra")

    @model_validator(mode="after")
    def _ref_pinned(self) -> "Workspace":
        if self.repo and not self.ref:
            raise ValueError("workspace.ref is required when workspace.repo is set")
        if self.ref and not COMMIT_RE.match(self.ref):
            raise ValueError("workspace.ref must be a full commit SHA; branches and tags are rejected")
        return self

    def strip_names(self) -> list[str]:
        return list(dict.fromkeys([*self.strip, *self.strip_extra]))


class Network(_Model):
    mode: Literal["bridge", "none"] = "bridge"
    # Declared egress. Enforcement needs an egress proxy; set `proxy` to one.
    egress: list[str] = Field(default_factory=lambda: ["model-api", "broker"])
    proxy: str | None = None


class Limits(_Model):
    max_runtime_minutes: int = Field(default=60, alias="maxRuntimeMinutes", ge=1)
    max_spawn_depth: int = Field(default=1, alias="maxSpawnDepth", ge=0)
    max_concurrent: int = Field(default=4, alias="maxConcurrent", ge=1)
    memory: str | None = None
    cpus: float | None = None
    pids: int = 512


class Profile(_Model):
    id: str
    version: str
    name: str
    description: str
    skills: list[Skill] = Field(default_factory=list)
    # Who may spawn this profile: "external", "*", or profile ids.
    spawnable_by: list[str] = Field(default_factory=lambda: ["external"], alias="spawnableBy")
    runtime: Runtime
    workspace: Workspace = Field(default_factory=Workspace)
    network: Network = Field(default_factory=Network)
    limits: Limits = Field(default_factory=Limits)

    _source: Path | None = PrivateAttr(default=None)
    _fingerprint: str = PrivateAttr(default="")

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("id must be a lowercase slug (a-z, 0-9, -)")
        return v

    @property
    def source(self) -> Path | None:
        return self._source

    @property
    def fingerprint(self) -> str:
        """sha256 over the resolved profile and every file it references."""
        return self._fingerprint

    def resolve(self, rel: str) -> Path:
        base = self._source.parent if self._source else Path.cwd()
        p = Path(rel)
        return p if p.is_absolute() else (base / p).resolve()

    def instructions_text(self) -> str | None:
        if not self.runtime.instructions:
            return None
        return self.resolve(self.runtime.instructions).read_text(encoding="utf-8")

    def skill_dirs(self) -> list[Path]:
        return [self.resolve(s) for s in self.runtime.bundled_skills]

    def agent_card(self) -> dict[str, Any]:
        """The A2A Agent Card compatible part, as returned by list_profiles."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "skills": [s.model_dump() for s in self.skills],
            "harness": self.runtime.harness,
        }

    def resolved_dump(self) -> dict[str, Any]:
        """The full profile as it will run, for the history record."""
        data = self.model_dump(by_alias=True)
        data["_fingerprint"] = self._fingerprint
        data["_source"] = str(self._source) if self._source else None
        return data


def _hash_tree(h: "hashlib._Hash", path: Path) -> None:
    if path.is_file():
        h.update(path.name.encode())
        h.update(path.read_bytes())
        return
    for child in sorted(path.rglob("*")):
        if child.is_file():
            h.update(str(child.relative_to(path)).encode())
            h.update(child.read_bytes())


def _finish(profile: Profile, source: Path | None) -> Profile:
    profile._source = source
    h = hashlib.sha256(json.dumps(profile.model_dump(by_alias=True), sort_keys=True).encode())
    if profile.runtime.instructions:
        p = profile.resolve(profile.runtime.instructions)
        if not p.is_file():
            raise ValueError(f"instructions file not found: {p}")
        _hash_tree(h, p)
    for d in profile.skill_dirs():
        if not d.exists():
            raise ValueError(f"bundled skill not found: {d}")
        _hash_tree(h, d)
    profile._fingerprint = "sha256:" + h.hexdigest()
    return profile


def load_profile(path: str | Path) -> Profile:
    path = Path(path).resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a profile must be a mapping")
    return _finish(Profile.model_validate(data), path)


def profile_from_dict(data: dict[str, Any], source: Path | None = None) -> Profile:
    return _finish(Profile.model_validate(data), source)


class ProfileRegistry:
    """Profiles from a directory (*.yaml / *.yml), re-read on every lookup so
    edits take effect without restarting the broker."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def _load_all(self) -> tuple[dict[str, Profile], dict[str, str]]:
        profiles: dict[str, Profile] = {}
        errors: dict[str, str] = {}
        if not self.directory.is_dir():
            return profiles, errors
        for path in sorted([*self.directory.glob("*.yaml"), *self.directory.glob("*.yml")]):
            try:
                p = load_profile(path)
            except Exception as e:  # report, keep the rest usable
                errors[path.name] = str(e)
                continue
            if p.id in profiles:
                errors[path.name] = f"duplicate profile id {p.id!r}"
                continue
            profiles[p.id] = p
        return profiles, errors

    def list(self) -> list[Profile]:
        return list(self._load_all()[0].values())

    def errors(self) -> dict[str, str]:
        return self._load_all()[1]

    def get(self, profile_id: str) -> Profile:
        profiles, errors = self._load_all()
        if profile_id not in profiles:
            hint = f" (load errors: {errors})" if errors else ""
            raise KeyError(f"unknown profile {profile_id!r}{hint}")
        return profiles[profile_id]

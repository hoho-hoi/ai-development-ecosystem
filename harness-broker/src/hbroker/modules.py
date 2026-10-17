"""Modules: harness-independent agent definitions that profiles bind to a runtime.

A module is a directory with a module.json:

    {
      "schemaVersion": 1,
      "id": "akinator",
      "name": "Akinator (questioner)",
      "description": "Guesses what the other side has in mind ...",
      "instructions": "AGENTS.md",      # role instructions, relative to the module dir
      "skills": "skills"                # optional; default "skills" when that dir exists
    }

The module says what the agent is (instructions, skills, description). The
profile says how it runs (harness, model, image, permissions). A profile
points at a module either pinned in a git repository (repo + full commit SHA)
or, for development, in a local directory.
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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MODULE_FILE = "module.json"
SCHEMA_VERSION = 1


class ModuleError(ValueError):
    pass


class ModuleSpec(BaseModel):
    """Contents of module.json."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_version: int = Field(alias="schemaVersion")
    id: str
    name: str
    description: str = ""
    instructions: str | None = None
    skills: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"unsupported module schemaVersion {v} (supported: {SCHEMA_VERSION})")
        return v

    @field_validator("instructions", "skills")
    @classmethod
    def _relative(cls, v: str | None) -> str | None:
        if v is not None:
            check_relative(v, "module path")
        return v


def check_relative(path: str, what: str) -> str:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not str(p).strip():
        raise ValueError(f"{what} must be a relative path inside the module: {path!r}")
    return path


@dataclass
class ResolvedModule:
    directory: Path
    spec: ModuleSpec
    source: dict[str, Any]
    files: dict[str, str] = field(default_factory=dict)  # relative path -> sha256

    def _inside(self, rel: str) -> Path:
        p = (self.directory / rel).resolve()
        if not p.is_relative_to(self.directory.resolve()):
            raise ModuleError(f"{rel!r} points outside the module")
        return p

    def instructions_text(self) -> str | None:
        if not self.spec.instructions:
            return None
        p = self._inside(self.spec.instructions)
        if not p.is_file():
            raise ModuleError(f"module instructions not found: {self.spec.instructions}")
        return p.read_text(encoding="utf-8")

    def skill_dirs(self) -> list[Path]:
        rel = self.spec.skills or "skills"
        root = self._inside(rel)
        if not root.is_dir():
            if self.spec.skills:
                raise ModuleError(f"module skills directory not found: {rel}")
            return []
        dirs = sorted(d for d in root.iterdir() if d.is_dir())
        for d in dirs:
            self._inside(str(d.relative_to(self.directory)))  # a symlinked skill must not leave the module
        return dirs

    def manifest(self) -> dict[str, Any]:
        return {"source": self.source, "spec": self.spec.model_dump(by_alias=True), "files": self.files}


def hash_files(directory: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file() and ".git" not in p.relative_to(directory).parts:
            out[str(p.relative_to(directory))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def read_spec(directory: Path) -> ModuleSpec:
    f = directory / MODULE_FILE
    if not f.is_file():
        raise ModuleError(f"{MODULE_FILE} not found in {directory}")
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ModuleError(f"{f}: {e}") from None
    return ModuleSpec.model_validate(data)


def load_local(directory: Path, source: dict[str, Any]) -> ResolvedModule:
    if not directory.is_dir():
        raise ModuleError(f"module directory not found: {directory}")
    spec = read_spec(directory)
    mod = ResolvedModule(directory=directory, spec=spec, source=source, files=hash_files(directory))
    mod.instructions_text()  # fail early on a broken module
    mod.skill_dirs()
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
        d = self.checkout_dir(repo, ref)
        return d if (d / ".hb-complete").exists() else None

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
            (co / ".hb-complete").write_text(ref)
            if dest.exists():  # another fetch won the race
                return dest
            co.rename(dest)
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

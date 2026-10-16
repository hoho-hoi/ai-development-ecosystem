"""Host-side workspace preparation.

The workspace is built on the host from a pinned commit and then copied into
the session's container; the host's own working copy is never mounted. Files
that harnesses read as project-level configuration are removed at every depth.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .profiles import COMMIT_RE, Profile


class WorkspaceError(RuntimeError):
    pass


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    # Hooks and templates from the host must not run or land in the clone
    # (`--template=` on clone, hooksPath for every command).
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def strip_tree(root: Path, names: list[str]) -> list[str]:
    """Remove every file or directory under root whose name matches one of
    `names` (shell-style patterns allowed). The .git directory is skipped."""
    removed: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        here = Path(dirpath)
        if here == root and ".git" in dirnames:
            dirnames.remove(".git")
        for d in list(dirnames):
            if any(fnmatch.fnmatchcase(d, pat) for pat in names):
                shutil.rmtree(here / d)
                dirnames.remove(d)
                removed.append(str((here / d).relative_to(root)) + "/")
        for f in filenames:
            if any(fnmatch.fnmatchcase(f, pat) for pat in names):
                (here / f).unlink()
                removed.append(str((here / f).relative_to(root)))
    return sorted(removed)


def prepare_workspace(profile: Profile, dest: Path, ref_override: str | None = None) -> dict[str, Any]:
    """Create the session workspace in `dest` (which must not exist yet)."""
    ws = profile.workspace
    ref = ref_override or ws.ref
    if ref_override and not COMMIT_RE.match(ref_override):
        raise WorkspaceError("ref override must be a full commit SHA")
    if ref_override and not ws.repo:
        raise WorkspaceError("ref override given but the profile has no workspace.repo")

    manifest: dict[str, Any] = {"repo": ws.repo, "ref": ref, "head": None, "removed": []}
    if ws.repo:
        _git("clone", "--quiet", "--no-checkout", "--template=", ws.repo, str(dest))
        _git("checkout", "--quiet", "--detach", ref, cwd=dest)
        head = _git("rev-parse", "HEAD", cwd=dest)
        if head != ref:
            raise WorkspaceError(f"checked out {head}, expected {ref}")
        manifest["head"] = head
    else:
        dest.mkdir(parents=True)
    manifest["removed"] = strip_tree(dest, ws.strip_names())
    return manifest

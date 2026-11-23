"""Host-side workspace preparation.

The workspace is built on the host from a pinned commit and then copied into
the session's container; the host's own working copy is never mounted. Its
files are left as they are: harnesses are configured not to read project-level
instructions or settings on their own (see harness.py).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .spec import COMMIT_RE, Workspace


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


def prepare_workspace(ws: Workspace, dest: Path) -> dict[str, Any]:
    """Create the session workspace in `dest` (which must not exist yet)."""
    ref = ws.ref

    manifest: dict[str, Any] = {"repo": ws.repo, "ref": ref, "head": None}
    if ws.repo:
        _git("clone", "--quiet", "--no-checkout", "--template=", ws.repo, str(dest))
        _git("checkout", "--quiet", "--detach", ref, cwd=dest)
        head = _git("rev-parse", "HEAD", cwd=dest)
        if head != ref:
            raise WorkspaceError(f"checked out {head}, expected {ref}")
        manifest["head"] = head
    else:
        dest.mkdir(parents=True)
    return manifest

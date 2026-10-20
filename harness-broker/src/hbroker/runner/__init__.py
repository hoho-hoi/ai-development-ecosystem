"""Runners turn a prepared session bundle into a running ACP agent process.

A bundle is a host directory with two parts:
    workspace/  the stripped checkout the agent works in
    hb/         the per-session harness config (see harness.py)

The runner decides where those live at runtime and gives back the agent's
stdio streams for the ACP connection.
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..profiles import Profile

STREAM_LIMIT = 64 * 1024 * 1024  # ACP frames can carry whole files


@dataclass
class RunHandle:
    process: asyncio.subprocess.Process
    workspace_path: str  # cwd for ACP session/new, as seen by the agent
    hb_path: str  # per-session harness config dir, as seen by the agent
    bridge_command: list[str]  # how the agent starts the broker's MCP bridge
    broker_url: str  # broker URL as reachable from the agent
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def reader(self) -> asyncio.StreamReader:
        assert self.process.stdout is not None
        return self.process.stdout

    @property
    def writer(self) -> asyncio.StreamWriter:
        assert self.process.stdin is not None
        return self.process.stdin  # type: ignore[return-value]


class Runner(abc.ABC):
    name: str = "runner"
    broker_url: str = ""

    @abc.abstractmethod
    def runtime_paths(self, bundle: Path) -> tuple[str, str]:
        """(workspace path, hb path) as the agent will see them."""

    @abc.abstractmethod
    async def start(
        self,
        session_id: str,
        profile: Profile,
        bundle: Path,
        env: dict[str, str],
        stderr_path: Path,
    ) -> RunHandle:
        ...

    async def stop(self, handle: RunHandle, timeout: float = 10) -> None:
        proc = handle.process
        if proc.returncode is None and proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    @abc.abstractmethod
    async def collect(self, handle: RunHandle, record_subdir: str, dest: Path) -> bool:
        """Copy the harness's own session records out before destruction."""

    @abc.abstractmethod
    async def snapshot_workspace(self, handle: RunHandle, dest: Path) -> None:
        """Copy the session's workspace as it is now into dest (for clone)."""

    @abc.abstractmethod
    async def destroy(self, handle: RunHandle) -> None:
        ...

    async def cleanup_orphans(self) -> list[str]:
        """Remove what a previous run of this broker left behind."""
        return []

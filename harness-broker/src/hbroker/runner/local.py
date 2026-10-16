"""LocalRunner: runs the adapter as a plain child process on the host.

There is NO isolation: the agent can read anything the broker user can. It
exists for development and tests, and must be enabled explicitly in the
broker config. HOME, cwd and the harness config dir still point inside the
session bundle, so the host's ~/.claude / ~/.codex are not picked up.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from ..profiles import Profile
from . import STREAM_LIMIT, RunHandle, Runner

BRIDGE_FILE = Path(__file__).resolve().parent.parent / "bridge.py"

PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TERM", "TZ", "SSL_CERT_FILE", "HTTPS_PROXY", "NO_PROXY")


class LocalRunner(Runner):
    name = "local"

    def __init__(self, broker_url: str, keep_bundles: bool = False):
        self.broker_url = broker_url
        self.keep_bundles = keep_bundles

    def runtime_paths(self, bundle: Path) -> tuple[str, str]:
        return str(bundle / "workspace"), str(bundle / "home" / ".hb")

    async def start(self, session_id: str, profile: Profile, bundle: Path, env: dict[str, str], stderr_path: Path) -> RunHandle:
        workspace, hb = self.runtime_paths(bundle)
        home = bundle / "home"
        # The bundle's hb/ dir becomes $HOME/.hb, mirroring the container layout.
        (home).mkdir(exist_ok=True)
        if (bundle / "hb").exists():
            (bundle / "hb").rename(Path(hb))
        child_env = {k: os.environ[k] for k in PASSTHROUGH if k in os.environ}
        child_env.update({"HOME": str(home), "TMPDIR": str(bundle / "tmp")})
        (bundle / "tmp").mkdir(exist_ok=True)
        for name in profile.runtime.secrets:
            if name in os.environ:
                child_env[name] = os.environ[name]
        child_env.update(env)
        stderr = open(stderr_path, "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                *profile.runtime.adapter,
                cwd=workspace,
                env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                limit=STREAM_LIMIT,
                start_new_session=True,
            )
        finally:
            stderr.close()
        return RunHandle(
            process=proc,
            workspace_path=workspace,
            hb_path=hb,
            bridge_command=[sys.executable, str(BRIDGE_FILE)],
            broker_url=self.broker_url,
            info={"runner": self.name, "pid": proc.pid, "bundle": str(bundle)},
        )

    async def collect(self, handle: RunHandle, record_subdir: str, dest: Path) -> bool:
        src = Path(handle.hb_path) / record_subdir
        if not src.exists():
            return False
        shutil.copytree(src, dest, dirs_exist_ok=True)
        return True

    async def destroy(self, handle: RunHandle) -> None:
        if not self.keep_bundles:
            shutil.rmtree(handle.info["bundle"], ignore_errors=True)

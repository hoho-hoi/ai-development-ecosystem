"""DockerRunner: one container per session.

Flow: `docker create` (image pinned by digest, no host mounts) → `docker cp`
the session bundle in as a tarball → `docker start -a -i`, whose stdio is the
ACP connection. The image's entrypoint (images/common/entrypoint.sh) unpacks
the bundle, hands it to the unprivileged `agent` user and execs the adapter.
On termination the harness's own records are copied out with `docker cp`
before the container is removed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from ..profiles import Profile
from . import STREAM_LIMIT, RunHandle, Runner

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_HB = "/home/agent/.hb"
CONTAINER_BUNDLE = "/hb-bundle.tar"
CONTAINER_BRIDGE = ["python3", "/opt/hb/hb_bridge.py"]

# Enough for the entrypoint to unpack and chown the bundle, then drop to the
# agent user; everything else is dropped.
CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID")


class DockerError(RuntimeError):
    pass


async def _run(*args: str, check: bool = True) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise DockerError(f"{' '.join(args[:3])} … failed: {err.decode(errors='replace').strip()}")
    return proc.returncode or 0, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()


def container_name(session_id: str) -> str:
    return f"hb-{session_id}"


def network_gateway(docker: str, network: str) -> str:
    """Create the session network if needed and return its gateway IP: the
    host's address on that network, where the broker listens for containers."""
    inspect = [docker, "network", "inspect", network, "--format", "{{(index .IPAM.Config 0).Gateway}}"]
    proc = subprocess.run(inspect, capture_output=True, text=True)
    if proc.returncode != 0:
        subprocess.run([docker, "network", "create", "--driver", "bridge", network], check=True, capture_output=True)
        proc = subprocess.run(inspect, capture_output=True, text=True, check=True)
    gateway = proc.stdout.strip()
    if not gateway:
        raise DockerError(f"docker network {network} has no gateway address")
    return gateway


def build_create_args(
    docker: str,
    session_id: str,
    profile: Profile,
    env: dict[str, str],
    network: str,
    owner: str = "",
) -> list[str]:
    """The full `docker create` argv. Secret values never appear here: they
    are passed as `-e NAME` and taken from the docker CLI's environment."""
    name = container_name(session_id)
    lim = profile.limits
    args = [
        docker, "create",
        "--name", name,
        "--interactive",
        "--init",
        "--label", f"hb.session={session_id}",
        "--label", f"hb.profile={profile.id}@{profile.version}",
        "--label", f"hb.owner={owner}",
        "--cap-drop", "ALL",
        *[a for cap in CAPS for a in ("--cap-add", cap)],
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(lim.pids),
        "--workdir", CONTAINER_WORKSPACE,
    ]
    if lim.memory:
        args += ["--memory", lim.memory]
    if lim.cpus:
        args += ["--cpus", str(lim.cpus)]
    if profile.network.mode == "none":
        args += ["--network", "none"]
    else:
        args += ["--network", network, "--add-host", "host.docker.internal:host-gateway"]
        if profile.network.proxy:
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                args += ["--env", f"{key}={profile.network.proxy}"]
            args += ["--env", "NO_PROXY=host.docker.internal", "--env", "no_proxy=host.docker.internal"]
    for key, value in sorted(env.items()):
        args += ["--env", f"{key}={value}"]
    for secret in profile.runtime.secrets:
        args += ["--env", secret]
    args += [profile.runtime.image, *profile.runtime.adapter]
    return args


def pack_bundle(bundle: Path, dest: Path) -> None:
    """Tar the bundle so it unpacks at / as /workspace and /home/agent/.hb."""
    with tarfile.open(dest, "w") as tar:
        tar.add(bundle / "workspace", arcname=CONTAINER_WORKSPACE.lstrip("/"))
        tar.add(bundle / "hb", arcname=CONTAINER_HB.lstrip("/"))


class DockerRunner(Runner):
    name = "docker"

    def __init__(
        self,
        broker_url: str,
        network: str = "hb-net",
        docker: str = "docker",
        keep_bundles: bool = False,
        owner: str = "",
    ):
        self.broker_url = broker_url
        self.network = network
        self.docker = docker
        self.keep_bundles = keep_bundles
        self.owner = owner  # label value identifying this broker's containers
        self._network_ready = False

    async def cleanup_orphans(self) -> list[str]:
        """Remove containers this broker left behind (e.g. after a crash)."""
        _, out, _ = await _run(
            self.docker, "ps", "-aq", "--filter", f"label=hb.owner={self.owner}", check=False
        )
        ids = out.split()
        if ids:
            await _run(self.docker, "rm", "-f", *ids, check=False)
        return ids

    def runtime_paths(self, bundle: Path) -> tuple[str, str]:
        return CONTAINER_WORKSPACE, CONTAINER_HB

    async def _ensure_network(self) -> None:
        if self._network_ready:
            return
        code, _, _ = await _run(self.docker, "network", "inspect", self.network, check=False)
        if code != 0:
            await _run(self.docker, "network", "create", "--driver", "bridge", self.network)
        self._network_ready = True

    async def start(self, session_id: str, profile: Profile, bundle: Path, env: dict[str, str], stderr_path: Path) -> RunHandle:
        if profile.network.mode != "none":
            await self._ensure_network()
        name = container_name(session_id)
        tar_path = bundle.with_suffix(".tar")
        await asyncio.to_thread(pack_bundle, bundle, tar_path)
        args = build_create_args(self.docker, session_id, profile, env, self.network, self.owner)
        await _run(*args)
        try:
            await _run(self.docker, "cp", str(tar_path), f"{name}:{CONTAINER_BUNDLE}")
            stderr = open(stderr_path, "ab")
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.docker, "start", "--attach", "--interactive", name,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr,
                    limit=STREAM_LIMIT,
                    env=os.environ.copy(),
                )
            finally:
                stderr.close()
        except BaseException:  # including cancellation: never leave the container behind
            await asyncio.shield(_run(self.docker, "rm", "-f", name, check=False))
            raise
        finally:
            tar_path.unlink(missing_ok=True)
        return RunHandle(
            process=proc,
            workspace_path=CONTAINER_WORKSPACE,
            hb_path=CONTAINER_HB,
            bridge_command=list(CONTAINER_BRIDGE),
            broker_url=self.broker_url,
            info={"runner": self.name, "container": name, "image": profile.runtime.image, "bundle": str(bundle)},
        )

    async def stop(self, handle: RunHandle, timeout: float = 10) -> None:
        name = handle.info["container"]
        if handle.process.stdin:
            try:
                handle.process.stdin.close()
            except Exception:
                pass
        await _run(self.docker, "stop", "--time", str(int(timeout)), name, check=False)
        try:
            await asyncio.wait_for(handle.process.wait(), timeout + 5)
        except asyncio.TimeoutError:
            handle.process.kill()

    async def collect(self, handle: RunHandle, record_subdir: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        code, _, _ = await _run(
            self.docker, "cp", f"{handle.info['container']}:{CONTAINER_HB}/{record_subdir}", str(dest), check=False
        )
        return code == 0

    async def snapshot_workspace(self, handle: RunHandle, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await _run(self.docker, "cp", f"{handle.info['container']}:{CONTAINER_WORKSPACE}", str(dest))

    async def destroy(self, handle: RunHandle) -> None:
        await _run(self.docker, "rm", "-f", handle.info["container"], check=False)
        if not self.keep_bundles:
            shutil.rmtree(handle.info["bundle"], ignore_errors=True)

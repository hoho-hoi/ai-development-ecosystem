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

from ..spec import HARNESSES, Spec
from . import STREAM_LIMIT, RunHandle, Runner

# The agora project directory: images/<harness>/Dockerfile and src/ (build context).
IMAGES_ROOT = Path(__file__).resolve().parents[3]

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_HB = "/home/agent/.agora"
CONTAINER_BUNDLE = "/agora-bundle.tar"
CONTAINER_BRIDGE = ["python3", "/opt/agora/agora_bridge.py"]

# Enough for the entrypoint to unpack and chown the bundle, then drop to the
# agent user; everything else is dropped.
CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID")


class DockerError(RuntimeError):
    pass


async def _run(*args: str, check: bool = True, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise DockerError(f"{' '.join(args[:3])} … failed: {err.decode(errors='replace').strip()}")
    return proc.returncode or 0, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()


def container_name(session_id: str) -> str:
    return f"agora-{session_id}"


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
    spec: Spec,
    env: dict[str, str],
    network: str,
    owner: str = "",
    image: str | None = None,
    extra_secrets: tuple[str, ...] = (),
) -> list[str]:
    """The full `docker create` argv. Secret values never appear here: they
    are passed as `-e NAME` and taken from the docker CLI's environment."""
    name = container_name(session_id)
    lim = spec.limits
    args = [
        docker, "create",
        "--name", name,
        "--interactive",
        "--init",
        "--label", f"agora.session={session_id}",
        "--label", f"agora.module={spec.id}@{spec.version}",
        "--label", f"agora.owner={owner}",
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
    if spec.network.mode == "none":
        args += ["--network", "none"]
    else:
        args += ["--network", network, "--add-host", "host.docker.internal:host-gateway"]
        if spec.network.proxy:
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                args += ["--env", f"{key}={spec.network.proxy}"]
            args += ["--env", "NO_PROXY=host.docker.internal", "--env", "no_proxy=host.docker.internal"]
    for key, value in sorted(env.items()):
        args += ["--env", f"{key}={value}"]
    for secret in [*spec.runtime.secrets, *(n for n in extra_secrets if n not in spec.runtime.secret_env)]:
        args += ["--env", secret]
    args += [image or spec.runtime.image or "", *spec.runtime.adapter]
    return args


def pack_bundle(bundle: Path, dest: Path) -> None:
    """Tar the bundle so it unpacks at / as /workspace and /home/agent/.agora."""
    with tarfile.open(dest, "w") as tar:
        tar.add(bundle / "workspace", arcname=CONTAINER_WORKSPACE.lstrip("/"))
        tar.add(bundle / "agora", arcname=CONTAINER_HB.lstrip("/"))


class DockerRunner(Runner):
    bridge_command = CONTAINER_BRIDGE
    name = "docker"

    def __init__(
        self,
        broker_url: str,
        network: str = "agora-net",
        docker: str = "docker",
        keep_bundles: bool = False,
        owner: str = "",
        build_args: dict[str, str] | None = None,
        build_network: str | None = None,
        images_dir: Path | None = None,
    ):
        self.broker_url = broker_url
        self.network = network
        self.docker = docker
        self.keep_bundles = keep_bundles
        self.owner = owner  # label value identifying this broker's containers
        self.build_args = build_args or {}
        self.build_network = build_network
        self.images_dir = images_dir or IMAGES_ROOT
        self._network_ready = False
        self._image_lock = asyncio.Lock()

    async def ensure_image(self, spec: Spec) -> str:
        """The image ID for the module's harness at its version: a prebuilt
        image the place names (harnesses.<name>.image), else agora/<harness>:
        <version>, built from images/<harness>/Dockerfile when missing."""
        rt = spec.runtime
        ref = rt.image or f"agora/{rt.harness}:{rt.version}"
        async with self._image_lock:
            code, out, _ = await _run(self.docker, "image", "inspect", "--format", "{{.Id}}", ref, check=False)
            if code == 0 and out:
                return out
            if rt.image:
                raise DockerError(f"image {rt.image} for {rt.harness} is not present at this place")
            dockerfile = self.images_dir / "images" / rt.harness / "Dockerfile"
            if not dockerfile.is_file():
                raise DockerError(f"no image definition for harness {rt.harness} ({dockerfile})")
            args = [self.docker, "build", "-q", "-f", str(dockerfile), "-t", ref,
                    "--build-arg", f"{HARNESSES[rt.harness]['version_arg']}={rt.version}"]
            for k, v in sorted(self.build_args.items()):
                args += ["--build-arg", f"{k}={v}"]
            if self.build_network:
                args += ["--network", self.build_network]
            await _run(*args, str(self.images_dir))
            _, out, _ = await _run(self.docker, "image", "inspect", "--format", "{{.Id}}", ref)
            return out

    async def cleanup_orphans(self) -> list[str]:
        """Remove containers this broker left behind (e.g. after a crash)."""
        _, out, _ = await _run(
            self.docker, "ps", "-aq", "--filter", f"label=agora.owner={self.owner}", check=False
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

    async def start(self, session_id: str, spec: Spec, bundle: Path, env: dict[str, str], stderr_path: Path,
                    secrets: dict[str, str] | None = None) -> RunHandle:
        if spec.network.mode != "none":
            await self._ensure_network()
        image = await self.ensure_image(spec)
        name = container_name(session_id)
        tar_path = bundle.with_suffix(".tar")
        await asyncio.to_thread(pack_bundle, bundle, tar_path)
        secret_values = {**spec.runtime.secret_values(), **(secrets or {})}
        args = build_create_args(self.docker, session_id, spec, env, self.network, self.owner, image,
                                 extra_secrets=tuple(secrets or {}))
        # Secret values go only into the docker CLI's environment (`-e NAME`).
        await _run(*args, env={**os.environ, **secret_values})
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
            agora_path=CONTAINER_HB,
            bridge_command=list(CONTAINER_BRIDGE),
            broker_url=self.broker_url,
            info={"runner": self.name, "container": name, "image": image,
                  "harness": f"{spec.runtime.harness}@{spec.runtime.version}", "bundle": str(bundle)},
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

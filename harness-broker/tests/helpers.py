"""Shared fixtures: a git repo with planted config files, a broker running
in-process with the local runner and the fake ACP agent."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hbroker.config import BrokerConfig  # noqa: E402
from hbroker.daemon import Broker, make_sockets  # noqa: E402

FAKE_AGENT = ROOT / "tests" / "fake_acp_agent.py"
BRIDGE = ROOT / "src" / "hbroker" / "bridge.py"
FAKE_DIGEST = "sha256:" + "a" * 64


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_repo(root: Path) -> tuple[Path, str, str]:
    """A repo whose first commit carries every file a harness would read as
    project config, at the top and nested. Returns (path, commit_a, commit_b)."""
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    files = {
        "README.md": "app\n",
        "src/app.py": "print('hi')\n",
        "CLAUDE.md": "host instructions\n",
        "CLAUDE.local.md": "local\n",
        ".mcp.json": "{}\n",
        ".claude/skills/evil/SKILL.md": "evil\n",
        ".claude/settings.json": "{}\n",
        "src/AGENTS.md": "nested agents\n",
        "src/.codex/config.toml": "model='x'\n",
        "docs/GEMINI.md": "gemini\n",
    }
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a")
    commit_a = git(repo, "rev-parse", "HEAD")
    (repo / "later.txt").write_text("added in b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "b")
    commit_b = git(repo, "rev-parse", "HEAD")
    return repo, commit_a, commit_b


def make_module_repo(root: Path) -> tuple[Path, str]:
    """A module repository: execution/ is the module, plus a second module
    under requirements/peer. Returns (repo, commit)."""
    repo = root / "module-repo"
    files = {
        "execution/module.json": json.dumps(
            {"schemaVersion": 1, "id": "mod", "name": "Test module", "description": "a module for tests",
             "instructions": "AGENTS.md"}
        ),
        "execution/AGENTS.md": "You are the test module.\n",
        "execution/skills/mod-skill/SKILL.md": "---\nname: mod-skill\ndescription: test\n---\n",
        "requirements/peer/module.json": json.dumps(
            {"schemaVersion": 1, "id": "peer", "name": "Peer", "description": "peer module", "instructions": "AGENTS.md"}
        ),
        "requirements/peer/AGENTS.md": "You are the peer.\n",
        "requirements/purpose.md": "never given to the agent\n",
    }
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "module")
    return repo, git(repo, "rev-parse", "HEAD")


def write_profiles(pdir: Path, repo: Path, commit: str, image: str = FAKE_DIGEST, adapter: list[str] | None = None,
                   module_repo: tuple[Path, str] | None = None) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "echo-instructions.md").write_text("You are the echo profile.\n")
    skill = pdir / "skills" / "echo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: echo-skill\ndescription: test\n---\n")
    adapter = adapter or [sys.executable, str(FAKE_AGENT)]
    base = {
        "version": "1.0.0",
        "description": "test profile",
        "runtime": {
            "harness": "claude-code",
            "adapter": adapter,
            "model": "test-model-1",
            "image": image,
            "instructions": "./echo-instructions.md",
            "bundledSkills": ["./skills/echo-skill"],
            "secrets": ["HB_TEST_SECRET"],
            "permissions": {"mode": "allow", "allowKinds": ["read", "edit"]},
        },
        "workspace": {"repo": str(repo), "ref": commit},
        "limits": {"maxRuntimeMinutes": 5, "maxSpawnDepth": 1, "maxConcurrent": 3},
    }
    profiles = {
        "echo": {**base, "id": "echo", "name": "Echo", "spawnableBy": ["external", "echo"]},
        "locked": {**base, "id": "locked", "name": "Locked", "spawnableBy": ["reviewer"]},
    }
    if module_repo:
        mrepo, mcommit = module_repo
        rt = {k: v for k, v in base["runtime"].items() if k not in ("instructions", "bundledSkills")}
        profiles["modlocal"] = {
            "id": "modlocal", "version": "1.0.0",
            "module": {"local": str(mrepo), "path": "execution"},
            "runtime": {**rt, "sessionMode": "per_message", "brokerTools": ["send"],
                        "permissions": {"mode": "deny", "deniedTools": ["Bash", "WebSearch"]}},
            "limits": base["limits"],
        }
        profiles["modrepo"] = {
            "id": "modrepo", "version": "1.0.0",
            "module": {"repo": str(mrepo), "ref": mcommit, "path": "requirements/peer"},
            "runtime": {**rt, "brokerTools": ["send", "receive"]},
            "limits": base["limits"],
        }
        profiles["notools"] = {
            "id": "notools", "version": "1.0.0",
            "module": {"local": str(mrepo), "path": "execution"},
            "runtime": {**rt, "brokerTools": False},
            "limits": base["limits"],
        }
    for pid, data in profiles.items():
        (pdir / f"{pid}.yaml").write_text(json.dumps(data))  # JSON is valid YAML


class RunningBroker:
    """Broker + uvicorn on a free port inside the test's event loop."""

    def __init__(self, tmp: Path, repo: Path, commit: str, docker_image: str | None = None):
        module_repo = make_module_repo(tmp)
        if docker_image:
            write_profiles(tmp / "profiles", repo, commit, image=docker_image,
                           adapter=["python3", "/opt/hb/fake_acp_agent.py"], module_repo=module_repo)
        else:
            write_profiles(tmp / "profiles", repo, commit, module_repo=module_repo)
        self.cfg = BrokerConfig(
            state_dir=tmp / "state",
            profiles_dir=tmp / "profiles",
            listen_port=0,
            runner="docker" if docker_image else "local",
            allow_local_runner=not docker_image,
            docker={"network": "hb-test-net"},
            max_sessions=6,
            max_spawn_depth=2,
        )
        self.broker = Broker(self.cfg)
        self.sockets = make_sockets(self.cfg, self.broker.manager.runner)
        self.url = self.cfg.local_url
        self._task: asyncio.Task[None] | None = None
        self._server: Any = None

    async def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(self.broker.app(), log_level="warning", lifespan="on")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=self.sockets))
        for _ in range(100):
            if self._server.started:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("broker did not start")

    async def stop(self) -> None:
        self._server.should_exit = True
        await asyncio.wait_for(self._task, 30)
        self.broker.store.close()

    # --- HTTP helpers ---------------------------------------------------------
    def _post_sync(self, path: str, body: dict[str, Any], token: str, timeout: float) -> tuple[int, Any]:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    async def post(self, path: str, body: dict[str, Any], token: str | None = None, timeout: float = 60) -> tuple[int, Any]:
        return await asyncio.to_thread(self._post_sync, path, body, token or self.broker.key, timeout)

    async def tool(self, tool_name: str, token: str | None = None, /, **args: Any) -> Any:
        code, body = await self.post(f"/api/tools/{tool_name}", args, token, timeout=args.get("wait", 0) + 120)
        if code != 200:
            raise AssertionError(f"{tool_name} -> {code}: {body}")
        return body

    def bridge_env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("HB_")}
        env.update({"HB_BROKER_URL": self.url, "HB_BROKER_KEY": self.broker.key, **extra})
        return env

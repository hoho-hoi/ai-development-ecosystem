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

from agora.config import BrokerConfig  # noqa: E402
from agora.daemon import Broker, make_sockets  # noqa: E402

FAKE_AGENT = ROOT / "tests" / "fake_acp_agent.py"
BRIDGE = ROOT / "src" / "agora" / "bridge.py"


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


PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
NS = "io.github.hoho-hoi.agora"


TEST_RUNTIME = {"harness": "claude-code", "version": "test", "model": "test-model-1"}
TEST_LIMITS = {"maxRuntimeMinutes": 5, "maxSpawnDepth": 1, "maxConcurrent": 3}


def plugin_json(name: str, description: str = "", **extra) -> str:
    return json.dumps({"$schema": PLUGIN_SCHEMA, "name": name, "description": description, **extra})


def make_module_repo(root: Path) -> tuple[Path, str]:
    """A repository with two modules (Agent Plugins with agora's extension):
    agent/ (instructions, a skill and an MCP server) and peer/, plus a file
    outside both. Returns (repo, commit)."""
    repo = root / "module-repo"
    files = {
        "agent/plugin.json": plugin_json("mod", "a module for tests", version="1.2.0", extensions={NS: {
            "runtimes": [TEST_RUNTIME], "sessionMode": "per_message", "tools": ["send"], "builtinTools": False,
            "limits": TEST_LIMITS}}),
        f"agent/{NS}/AGENTS.md": "You are the test module.\n",
        "agent/skills/mod-skill/SKILL.md": "---\nname: mod-skill\ndescription: test\n---\n",
        "agent/mcp.json": json.dumps({"$schema": MCP_SCHEMA, "mcpServers": {
            "tool": {"type": "stdio", "command": "./bin/tool", "args": ["--root", "${PLUGIN_ROOT}"],
                     "env": {"MODE": "x"}}}}),
        "agent/bin/tool": "#!/bin/sh\n",
        "peer/plugin.json": plugin_json("peer", "peer module", extensions={NS: {
            "runtimes": [TEST_RUNTIME], "tools": ["send", "receive"], "limits": TEST_LIMITS}}),
        f"peer/{NS}/AGENTS.md": "You are the peer.\n",
        "docs/purpose.md": "never given to the agent\n",
    }
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    git(repo.parent, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "module")
    return repo, git(repo, "rev-parse", "HEAD")


def fake_jwt(claims: dict) -> str:
    import base64

    def enc(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


FAKE_ACCESS_TOKEN_MARK = "access-secret-mark-1234567890"
FAKE_REFRESH_TOKEN = "rt-refresh-secret-0987654321"


def make_codex_login(login_dir: Path, expires_in: float = 10 * 86400) -> dict:
    """An auth.json shaped like a Codex ChatGPT login."""
    import time

    login_dir.mkdir(parents=True, exist_ok=True)
    auth = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": fake_jwt({"email": "me@example.com",
                                  "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}),
            "access_token": fake_jwt({"exp": int(time.time() + expires_in), "mark": FAKE_ACCESS_TOKEN_MARK}),
            "refresh_token": FAKE_REFRESH_TOKEN,
            "account_id": "acct-1",
        },
        "last_refresh": "2026-10-18T00:00:00Z",
    }
    (login_dir / "auth.json").write_text(json.dumps(auth))
    return auth


def _module(d: Path, name: str, ext: dict, instructions: str | None = None, skill: bool = False,
            copy_from: Path | None = None) -> Path:
    import shutil

    if copy_from:
        shutil.copytree(copy_from, d)
    else:
        d.mkdir(parents=True)
    (d / "plugin.json").write_text(plugin_json(name, f"test module {name}", version="1.0.0", extensions={NS: ext}))
    if instructions is not None:
        (d / NS).mkdir(exist_ok=True)
        (d / NS / "AGENTS.md").write_text(instructions)
    if skill:
        sk = d / "skills" / "echo-skill"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text("---\nname: echo-skill\ndescription: test\n---\n")
    return d


def write_modules(home: Path, module_repo: tuple[Path, str]) -> list[dict]:
    """Local test modules (their plugin.json says how they run), plus the
    module repository's two modules. Returns agora.yaml's `modules` list."""
    mdir = home / "modules"
    mrepo, mcommit = module_repo
    echo_rt = {**TEST_RUNTIME, "env": {"AGORA_TEST_SECRET": "${AGORA_TEST_SECRET}"}}
    base = {"runtimes": [echo_rt], "limits": TEST_LIMITS}
    agent = mrepo / "agent"
    plain = {k: v for k, v in TEST_RUNTIME.items()}
    mods = [
        _module(mdir / "echo", "echo", {**base, "spawnableBy": ["external", "echo"]},
                "You are the echo module.\n", skill=True),
        _module(mdir / "locked", "locked", {**base, "spawnableBy": ["reviewer"]}, "Locked.\n"),
        _module(mdir / "codexplan", "codexplan", {"runtimes": [{**plain, "harness": "codex", "login": "chatgpt"}],
                "builtinTools": False, "limits": TEST_LIMITS}, copy_from=agent),
        # a harness that cannot resume a conversation
        _module(mdir / "noload", "noload", {"runtimes": [{**plain, "env": {"FAKE_NO_LOAD": "1"}}], "limits": TEST_LIMITS},
                copy_from=agent),
        # the harness starts on another model but offers the module's
        _module(mdir / "pickmodel", "pickmodel", {"runtimes": [{**plain, "env": {
            "FAKE_MODELS": "other-model,test-model-1", "FAKE_CURRENT": "other-model"}}], "limits": TEST_LIMITS},
                copy_from=agent),
        # the harness does not offer the module's model at all
        _module(mdir / "nomodel", "nomodel", {"runtimes": [{**plain, "env": {
            "FAKE_MODELS": "other-model", "FAKE_CURRENT": "other-model"}}], "limits": TEST_LIMITS}, copy_from=agent),
        _module(mdir / "notools", "notools", {"runtimes": [plain], "tools": False, "limits": TEST_LIMITS},
                copy_from=agent),
    ]
    return ([{"local": str(m)} for m in mods]
            + [{"local": str(mrepo), "path": "agent"}, {"repo": str(mrepo), "ref": mcommit, "path": "peer"}])


class RunningBroker:
    """A place (broker + uvicorn on a free port) inside the test's event loop.
    Several can run side by side, each in its own directory, as peers."""

    def __init__(self, tmp: Path, repo: Path, commit: str, docker_image: str | None = None, place: str = "pc1",
                 llm_gateway: dict | None = None, telemetry: Any = None):
        if not (tmp / "module-repo").exists():
            make_module_repo(tmp)
        module_repo = (tmp / "module-repo", git(tmp / "module-repo", "rev-parse", "HEAD"))
        home = tmp / place
        modules = write_modules(home, module_repo)
        adapter = ["python3", "/opt/agora/fake_acp_agent.py"] if docker_image else [sys.executable, str(FAKE_AGENT)]
        fake = {"adapter": adapter, **({"image": docker_image} if docker_image else {})}
        self.place = place
        # Fixture convenience: echo and locked work in this repository unless told otherwise.
        self.default_workspace = {"repo": str(repo), "ref": commit}
        self.cfg = BrokerConfig(
            place=place,
            state_dir=home / "state",
            modules=modules,
            harnesses={"claude-code": fake, "codex": fake},
            listen_port=0,
            runner="docker" if docker_image else "local",
            allow_local_runner=not docker_image,
            docker={"network": "agora-test-net"},
            max_sessions=6,
            max_spawn_depth=2,
            codex_login_dir=tmp / "codex-login",
            llm_gateway=llm_gateway,
        )
        make_codex_login(tmp / "codex-login")
        self.broker = Broker(self.cfg, telemetry=telemetry)
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

    def address(self, sid: str) -> str:
        return f"{sid}@{self.place}"

    def trust(self, other: "RunningBroker") -> None:
        """Make other a peer of this place (as its `peers` entry would)."""
        from agora.federation import Peer

        fed = other.broker.federation
        self.broker.federation.add_peer(Peer(other.place, other.url, fed.key.public_hex))

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
        if tool_name == "spawn" and args.get("module") in ("echo", "locked") and "workspace" not in args \
                and not args.get("place"):
            args["workspace"] = self.default_workspace
        code, body = await self.post(f"/api/tools/{tool_name}", args, token, timeout=args.get("wait", 0) + 120)
        if code != 200:
            raise AssertionError(f"{tool_name} -> {code}: {body}")
        return body

    def bridge_env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGORA_")}
        env.update({"AGORA_BROKER_URL": self.url, "AGORA_BROKER_KEY": self.broker.key, **extra})
        return env

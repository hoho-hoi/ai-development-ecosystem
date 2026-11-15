"""Per-harness configuration for a session.

Each session gets its own harness config directory, built from the profile
alone, so nothing from the host's ~/.claude, ~/.codex or the project leaks in.
`host_dir` is where the files are written on the host; `runtime_dir` is the
path the same directory has inside the session (container or local run).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .modules import ModuleCache, ResolvedModule
from .profiles import Profile


def _copy_skills(skill_dirs: list[Path], dest: Path) -> list[str]:
    names = []
    for src in skill_dirs:
        if not (src / "SKILL.md").is_file():
            raise ValueError(f"bundled skill {src} has no SKILL.md")
        # Keep symlinks as links: following them could copy host files into the session.
        shutil.copytree(src, dest / src.name, symlinks=True)
        names.append(src.name)
    return names


BRIDGE_SERVER = "agora"  # MCP server name of the broker bridge in every session
OPENCODE_AGENT = "agora"  # the one agent of an OpenCode session


def prepare_harness_config(
    profile: Profile,
    host_dir: Path,
    runtime_dir: str,
    instructions: str | None,
    skill_dirs: list[Path],
) -> dict[str, str]:
    """Write the harness config under host_dir and return the env vars that
    point the harness at it. `instructions` and `skill_dirs` come from the
    profile's module (or its inline definition)."""
    rt = profile.runtime
    env: dict[str, str] = {"AGORA_MODEL": rt.model}

    if rt.harness == "claude-code":
        d = host_dir / "claude"
        (d / "skills").mkdir(parents=True)
        if instructions is not None:
            (d / "CLAUDE.md").write_text(instructions, encoding="utf-8")
        _copy_skills(skill_dirs, d / "skills")
        settings: dict[str, object] = {}
        perms: dict[str, list[str]] = {}
        # The broker tools the profile grants are allowed outright: they are
        # how the agent talks, so a "deny" profile must not block them.
        allow = list(rt.permissions.allowed_tools) + [f"mcp__{BRIDGE_SERVER}__{t}" for t in rt.broker_tools]
        if allow:
            perms["allow"] = allow
        if rt.permissions.denied_tools:
            perms["deny"] = rt.permissions.denied_tools
        if perms:
            settings["permissions"] = perms
        (d / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
        env.update(
            {
                "CLAUDE_CONFIG_DIR": f"{runtime_dir}/claude",
                "ANTHROPIC_MODEL": rt.model,
                "DISABLE_AUTOUPDATER": "1",
            }
        )
    elif rt.harness == "codex":
        d = host_dir / "codex"
        (d / "skills").mkdir(parents=True)
        lines = [
            f"model = {json.dumps(rt.model)}",
            # Credentials live in $CODEX_HOME/auth.json, never an OS keyring.
            'cli_auth_credentials_store = "file"',
        ]
        if rt.permissions.mode == "deny":
            lines.append('web_search = "disabled"')
        if instructions is not None:
            (d / "AGENTS.md").write_text(instructions, encoding="utf-8")
        else:
            # No profile instructions: make sure no AGENTS.md is read at all.
            lines.append("project_doc_max_bytes = 0")
        _copy_skills(skill_dirs, d / "skills")
        (d / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        env["CODEX_HOME"] = f"{runtime_dir}/codex"
        # codex-acp: no browser login offered; approvals go to the broker
        # (read-only for deny profiles, workspace writes otherwise), not to
        # Codex's own auto reviewer.
        env["NO_BROWSER"] = "1"
        env["INITIAL_AGENT_MODE"] = "read-only" if rt.permissions.mode == "deny" else "workspace-write"
    elif rt.harness == "opencode":
        # OpenCode V2 (`opencode acp`). The whole config comes from the profile:
        # one agent built from the module's instructions, the module's skills,
        # and private XDG dirs so no host or project config, auth or session
        # store is read. Tool permissions are asked over ACP and answered by
        # the broker from runtime.permissions, as for the other harnesses.
        d = host_dir / "opencode"
        (d / "skills").mkdir(parents=True)
        _copy_skills(skill_dirs, d / "skills")
        root = f"{runtime_dir}/opencode"
        agent: dict[str, object] = {"mode": "all", "model": rt.model}
        if instructions is not None:
            agent["system"] = instructions
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": rt.model,
            "default_agent": OPENCODE_AGENT,
            "agents": {OPENCODE_AGENT: agent},
            "skills": [f"{root}/skills"],
            "share": "disabled",
            "update": "disable",
            "compaction": {"auto": False},
        }
        (d / "opencode.json").write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        env.update(
            {
                "OPENCODE_CONFIG": f"{root}/opencode.json",
                "OPENCODE_CONFIG_DIR": root,
                "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
                "OPENCODE_CONFIG_PROJECT_DISABLE": "true",
                "OPENCODE_DISABLE_CLAUDE_CODE": "true",
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "XDG_CONFIG_HOME": f"{root}/xdg/config",
                "XDG_DATA_HOME": f"{root}/xdg/data",
                "XDG_STATE_HOME": f"{root}/xdg/state",
                "XDG_CACHE_HOME": f"{root}/xdg/cache",
            }
        )
    else:
        # gemini / custom: files are staged, wiring them in is up to the image.
        d = host_dir / rt.harness
        (d / "skills").mkdir(parents=True)
        if instructions is not None:
            (d / "INSTRUCTIONS.md").write_text(instructions, encoding="utf-8")
            env["AGORA_INSTRUCTIONS_FILE"] = f"{runtime_dir}/{rt.harness}/INSTRUCTIONS.md"
        _copy_skills(skill_dirs, d / "skills")
        env["AGORA_SKILLS_DIR"] = f"{runtime_dir}/{rt.harness}/skills"

    env.update(rt.env)
    return env


@dataclass
class Definition:
    """What the agent is: from the profile's module, or its inline definition."""

    instructions: str | None
    skill_dirs: list[Path]
    manifest: dict | None = None  # the module's source, plugin.json and file hashes
    module: ResolvedModule | None = None


def resolve_definition(profile: Profile, cache: "ModuleCache") -> Definition:
    if profile.module:
        base = profile.source.parent if profile.source else None
        mod = cache.resolve(profile.module, base)
        return Definition(mod.instructions_text(), mod.skill_dirs(), mod.manifest(), mod)
    return Definition(profile.instructions_text(), profile.skill_dirs())


# Files in a harness dir that hold credentials: never collected into history.
CREDENTIAL_FILES = {"codex": ("auth.json",)}


def harness_record_dir(profile: Profile) -> str:
    """Subdirectory (under the session's agora dir) that holds the harness's own
    session records, collected into history when the session ends."""
    return {"claude-code": "claude", "codex": "codex", "opencode": "opencode"}.get(
        profile.runtime.harness, profile.runtime.harness
    )

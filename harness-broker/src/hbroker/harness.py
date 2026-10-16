"""Per-harness configuration for a session.

Each session gets its own harness config directory, built from the profile
alone, so nothing from the host's ~/.claude, ~/.codex or the project leaks in.
`host_dir` is where the files are written on the host; `runtime_dir` is the
path the same directory has inside the session (container or local run).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .profiles import Profile


def _copy_skills(profile: Profile, dest: Path) -> list[str]:
    names = []
    for src in profile.skill_dirs():
        if not (src / "SKILL.md").is_file():
            raise ValueError(f"bundled skill {src} has no SKILL.md")
        shutil.copytree(src, dest / src.name)
        names.append(src.name)
    return names


def prepare_harness_config(profile: Profile, host_dir: Path, runtime_dir: str) -> dict[str, str]:
    """Write the harness config under host_dir and return the env vars that
    point the harness at it."""
    rt = profile.runtime
    instructions = profile.instructions_text()
    env: dict[str, str] = {"HB_MODEL": rt.model}

    if rt.harness == "claude-code":
        d = host_dir / "claude"
        (d / "skills").mkdir(parents=True)
        if instructions is not None:
            (d / "CLAUDE.md").write_text(instructions, encoding="utf-8")
        _copy_skills(profile, d / "skills")
        settings: dict[str, object] = {}
        if rt.permissions.allowed_tools:
            settings["permissions"] = {"allow": rt.permissions.allowed_tools}
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
        lines = [f"model = {json.dumps(rt.model)}"]
        if instructions is not None:
            (d / "AGENTS.md").write_text(instructions, encoding="utf-8")
        else:
            # No profile instructions: make sure no AGENTS.md is read at all.
            lines.append("project_doc_max_bytes = 0")
        _copy_skills(profile, d / "skills")
        (d / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        env["CODEX_HOME"] = f"{runtime_dir}/codex"
    else:
        # gemini / custom: files are staged, wiring them in is up to the image.
        d = host_dir / rt.harness
        (d / "skills").mkdir(parents=True)
        if instructions is not None:
            (d / "INSTRUCTIONS.md").write_text(instructions, encoding="utf-8")
            env["HB_INSTRUCTIONS_FILE"] = f"{runtime_dir}/{rt.harness}/INSTRUCTIONS.md"
        _copy_skills(profile, d / "skills")
        env["HB_SKILLS_DIR"] = f"{runtime_dir}/{rt.harness}/skills"

    env.update(rt.env)
    return env


def harness_record_dir(profile: Profile) -> str:
    """Subdirectory (under the session's hb dir) that holds the harness's own
    session records, collected into history when the session ends."""
    return {"claude-code": "claude", "codex": "codex"}.get(profile.runtime.harness, profile.runtime.harness)

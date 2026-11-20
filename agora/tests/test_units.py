import json
import tarfile
import tempfile
import unittest
from pathlib import Path

import os
from unittest import mock

from helpers import MCP_SCHEMA, NS, PLUGIN_SCHEMA, plugin_json, FAKE_REFRESH_TOKEN, ROOT, fake_jwt, make_codex_login, make_module_repo, make_repo

from agora.acp_client import decide_permission
from agora.harness import harness_model_id, harness_record_dir, prepare_harness_config, resolve_definition
from agora.logins import LoginError, load_chatgpt_login, write_access_only
from agora.modules import ModuleCache, load_local
from agora.spec import (BROKER_TOOLS, CLAUDE_BUILTINS, DEFAULT_STRIP, Catalog, HarnessOverride, ModuleSource,
                        Network, Permissions, Workspace, load_module_dir, resolve)
from agora.runner.docker import build_create_args, pack_bundle
from agora.store import HistoryStore
from agora.workspace import WorkspaceError, prepare_workspace


_counter = [0]


def make_spec(root: Path, runtime: dict | None = None, ext: dict | None = None, instructions: str | None = None,
              skill: bool = False, workspace: dict | None = None, network: dict | None = None,
              harnesses: dict | None = None, name: str = "m"):
    """A module directory with the given agora extension, resolved for a spawn."""
    _counter[0] += 1
    d = root / f"{name}-{_counter[0]}"
    d.mkdir(parents=True)
    rt = {"harness": "claude-code", "version": "1.0.0", "model": "m", **(runtime or {})}
    extension = {"runtimes": [rt], **(ext or {})}
    (d / "plugin.json").write_text(plugin_json(name, "d", version="1", extensions={NS: extension}))
    if instructions is not None:
        (d / NS).mkdir()
        (d / NS / "AGENTS.md").write_text(instructions)
    if skill:
        (d / "skills" / "echo-skill").mkdir(parents=True)
        (d / "skills" / "echo-skill" / "SKILL.md").write_text("---\nname: echo-skill\ndescription: test\n---\n")
    return resolve(
        load_module_dir(d), ModuleSource(local=str(d)),
        workspace=Workspace.model_validate(workspace or {}),
        network=Network.model_validate(network or {}),
        harnesses={k: HarnessOverride.model_validate(v) for k, v in (harnesses or {}).items()},
    )


class SpecTests(unittest.TestCase):
    def test_example_modules_load(self):
        for d in (ROOT / "examples" / "modules").iterdir():
            mod = load_module_dir(d)
            self.assertTrue(mod.plugin.agora.runtimes, d)

    def test_first_runtime_this_place_can_run(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"HAVE_IT": "tok-123456"}, clear=False):
            os.environ.pop("MISSING_VAR", None)
            ext = {"runtimes": [
                {"harness": "nope", "version": "1", "model": "x"},
                {"harness": "claude-code", "version": "1", "model": "sonnet", "env": {"CLAUDE_CODE_OAUTH_TOKEN": "${MISSING_VAR}"}},
                {"harness": "opencode", "version": "2", "model": "opencode/free", "env": {"T": "${HAVE_IT}", "MODE": "x"}},
            ]}
            spec = make_spec(Path(t), ext=ext)
            rt = spec.runtime
            self.assertEqual((rt.harness, rt.model, rt.option_index), ("opencode", "opencode/free", 2))
            self.assertEqual(rt.adapter, ["opencode", "acp"])
            self.assertEqual((rt.env, rt.secret_env), ({"MODE": "x"}, {"T": "HAVE_IT"}))
            self.assertEqual(rt.secret_values(), {"T": "tok-123456"})
            with self.assertRaisesRegex(ValueError, "MISSING_VAR"):
                make_spec(Path(t), ext={"runtimes": ext["runtimes"][:2]})
            with self.assertRaisesRegex(ValueError, "no runtimes"):
                make_spec(Path(t), ext={"runtimes": []})

    def test_env_values_are_literal_or_one_reference(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"A": "1"}):
            with self.assertRaisesRegex(ValueError, "literal or exactly one"):
                make_spec(Path(t), runtime={"env": {"X": "Bearer ${A}"}})
            with self.assertRaises(ValueError):
                make_spec(Path(t), runtime={"env": {"bad name": "x"}})

    def test_workspace_ref_must_be_a_commit(self):
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            Workspace.model_validate({"repo": "/r", "ref": "main"})
        with self.assertRaisesRegex(ValueError, "required"):
            Workspace.model_validate({"repo": "/r"})
        self.assertEqual(Workspace().strip, list(DEFAULT_STRIP))

    def test_unknown_extension_fields_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):
                make_spec(Path(t), ext={"permissions": {"mode": "deny"}})
            with self.assertRaises(ValueError):
                make_spec(Path(t), runtime={"image": "sha256:" + "a" * 64})

    def test_tools_and_builtin_tools(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            spec = make_spec(t)
            self.assertEqual(spec.runtime.broker_tools, list(BROKER_TOOLS))
            self.assertEqual((spec.runtime.permissions.mode, spec.runtime.permissions.denied_tools), ("allow", []))
            spec = make_spec(t, ext={"tools": ["send"], "builtinTools": False})
            self.assertEqual(spec.runtime.broker_tools, ["send"])
            self.assertEqual(spec.runtime.permissions.mode, "deny")
            self.assertEqual(spec.runtime.permissions.denied_tools, list(CLAUDE_BUILTINS))
            self.assertEqual(make_spec(t, ext={"tools": False}).runtime.broker_tools, [])
            with self.assertRaisesRegex(ValueError, "unknown agora tool"):
                make_spec(t, ext={"tools": ["list_profiles"]})

    def test_place_can_name_an_image_or_command(self):
        with tempfile.TemporaryDirectory() as t:
            spec = make_spec(Path(t), harnesses={"claude-code": {"image": "sha256:" + "b" * 64, "adapter": ["x", "y"]}})
            self.assertEqual((spec.runtime.image, spec.runtime.adapter), ("sha256:" + "b" * 64, ["x", "y"]))
            self.assertIsNone(make_spec(Path(t)).runtime.image)

    def test_fingerprint_tracks_module_files_and_runtime(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            spec = make_spec(t, instructions="one")
            d = Path(spec.source.local)
            again = resolve(load_module_dir(d), spec.source)
            self.assertEqual(spec.fingerprint, again.fingerprint)
            (d / NS / "AGENTS.md").write_text("two")
            self.assertNotEqual(spec.fingerprint, resolve(load_module_dir(d), spec.source).fingerprint)
            ws = resolve(load_module_dir(d), spec.source, workspace=Workspace(repo="/r", ref="a" * 40))
            self.assertNotEqual(ws.fingerprint, resolve(load_module_dir(d), spec.source).fingerprint)

    def test_catalog_lists_registered_modules(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            mrepo, commit = make_module_repo(t)
            cat = Catalog([ModuleSource(local=str(mrepo), path="agent"),
                           ModuleSource(repo=str(mrepo), ref=commit, path="peer"),
                           ModuleSource(local=str(t / "missing"))], ModuleCache(t / "cache"))
            cards, errors = cat.cards()
            self.assertEqual(sorted(c["id"] for c in cards), ["mod", "peer"])
            self.assertEqual(len(errors), 1)
            self.assertEqual(cat.get("peer").source.ref, commit)
            with self.assertRaises(KeyError):
                cat.get("nope")


class WorkspaceTests(unittest.TestCase):
    def test_pinned_and_stripped(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            repo, a, b = make_repo(t)
            m = prepare_workspace(Workspace(repo=str(repo), ref=a), t / "ws")
            self.assertEqual(m["head"], a)
            files = sorted(str(f.relative_to(t / "ws")) for f in (t / "ws").rglob("*") if ".git" not in f.parts)
            self.assertEqual(files, ["README.md", "docs", "src", "src/app.py"])
            self.assertIn(".claude/", m["removed"])
            self.assertIn("src/AGENTS.md", m["removed"])
            self.assertIn("src/.codex/", m["removed"])
            self.assertFalse((t / "ws" / "later.txt").exists())  # commit b is not there
            self.assertTrue((t / "ws" / ".git").is_dir())

            m2 = prepare_workspace(Workspace(repo=str(repo), ref=b), t / "ws2")
            self.assertEqual(m2["head"], b)
            self.assertTrue((t / "ws2" / "later.txt").exists())

    def test_no_repo_gives_empty_workspace(self):
        with tempfile.TemporaryDirectory() as t:
            m = prepare_workspace(Workspace(), Path(t) / "ws")
            self.assertEqual(m["removed"], [])
            self.assertEqual(list((Path(t) / "ws").iterdir()), [])


class HarnessConfigTests(unittest.TestCase):
    def test_claude_code_config(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            spec = make_spec(t, runtime={"model": "test-model-1"}, instructions="You are the echo module.\n", skill=True)
            d = resolve_definition(spec)
            env = prepare_harness_config(spec, t / "agora", "/home/agent/.agora", d.instructions, d.skill_dirs)
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/agent/.agora/claude")
            self.assertEqual(env["ANTHROPIC_MODEL"], "test-model-1")
            self.assertEqual((t / "agora/claude/CLAUDE.md").read_text(), "You are the echo module.\n")
            self.assertTrue((t / "agora/claude/skills/echo-skill/SKILL.md").is_file())

    def test_codex_without_instructions_disables_agents_md(self):
        with tempfile.TemporaryDirectory() as t:
            spec = make_spec(Path(t), runtime={"harness": "codex"})
            env = prepare_harness_config(spec, Path(t) / "o", "/rt", None, [])
            self.assertEqual(env["CODEX_HOME"], "/rt/codex")
            self.assertIn("project_doc_max_bytes = 0", (Path(t) / "o/codex/config.toml").read_text())


class OpencodeHarnessTests(unittest.TestCase):
    def test_opencode_config_is_isolated_and_built_from_the_module(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            skill = t / "src-skill" / "hello"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: hello\ndescription: x\n---\n")
            p = make_spec(t, runtime={"harness": "opencode", "model": "anthropic/claude-haiku-5-5"})
            env = prepare_harness_config(p, t / "out", "/rt", "You are the role.", [skill])
            d = t / "out" / "opencode"
            cfg = json.loads((d / "opencode.json").read_text())
            self.assertEqual(cfg["model"], "anthropic/claude-haiku-5-5")
            self.assertEqual(cfg["default_agent"], "agora")
            agent = cfg["agents"]["agora"]
            self.assertEqual((agent["mode"], agent["system"], agent["model"]),
                             ("all", "You are the role.", "anthropic/claude-haiku-5-5"))
            self.assertEqual(cfg["skills"], ["/rt/opencode/skills"])
            self.assertEqual(cfg["share"], "disabled")
            self.assertTrue((d / "skills" / "hello" / "SKILL.md").is_file())
            # nothing from the host or the project is read; state stays in the session
            self.assertEqual(env["OPENCODE_CONFIG"], "/rt/opencode/opencode.json")
            self.assertEqual(env["OPENCODE_CONFIG_DIR"], "/rt/opencode")
            for flag in ("OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_CONFIG_PROJECT_DISABLE",
                         "OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_AUTOUPDATE"):
                self.assertEqual(env[flag], "true", flag)
            for xdg in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
                self.assertTrue(env[xdg].startswith("/rt/opencode/"), xdg)
            self.assertEqual(harness_record_dir(p), "opencode")

    def test_opencode_without_instructions_has_no_system(self):
        with tempfile.TemporaryDirectory() as t:
            p = make_spec(Path(t), runtime={"harness": "opencode", "model": "anthropic/claude-haiku-5-5"})
            prepare_harness_config(p, Path(t) / "o", "/rt", None, [])
            cfg = json.loads((Path(t) / "o/opencode/opencode.json").read_text())
            self.assertNotIn("system", cfg["agents"]["agora"])


class PiHarnessTests(unittest.TestCase):
    def test_pi_gets_the_module_alone_through_agora_pi(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            skill = t / "src-skill" / "hello"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: hello\ndescription: x\n---\n")
            p = make_spec(t, runtime={"harness": "pi", "model": "claude-haiku-5-5"}, ext={"builtinTools": False})
            env = prepare_harness_config(p, t / "out", "/rt", "You are the role.", [skill])
            d = t / "out" / "pi"
            self.assertEqual((d / "AGENTS.md").read_text(), "You are the role.")
            self.assertTrue((d / "skills" / "hello" / "SKILL.md").is_file())
            self.assertEqual(env["AGORA_INSTRUCTIONS_FILE"], "/rt/pi/AGENTS.md")
            self.assertEqual(env["AGORA_SKILLS_DIR"], "/rt/pi/skills")
            self.assertEqual(env["AGORA_PI_MODEL"], "anthropic/claude-haiku-5-5")
            self.assertEqual(env["AGORA_BUILTIN_TOOLS"], "0")
            self.assertEqual(env["PI_OFFLINE"], "1")
            self.assertEqual(p.runtime.adapter, ["agora-pi"])
            self.assertEqual(harness_model_id(p), "anthropic/claude-haiku-5-5")
            self.assertEqual(harness_record_dir(p), "pi")

    def test_pi_without_instructions_and_with_builtin_tools(self):
        with tempfile.TemporaryDirectory() as t:
            p = make_spec(Path(t), runtime={"harness": "pi", "model": "openai/gpt-x"})
            env = prepare_harness_config(p, Path(t) / "o", "/rt", None, [])
            self.assertNotIn("AGORA_INSTRUCTIONS_FILE", env)
            self.assertFalse((Path(t) / "o/pi/AGENTS.md").exists())
            self.assertEqual(env["AGORA_BUILTIN_TOOLS"], "1")
            self.assertEqual(env["AGORA_PI_MODEL"], "openai/gpt-x")

    def test_other_harnesses_keep_the_module_model_name(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(harness_model_id(make_spec(Path(t))), "m")

class ModuleTests(unittest.TestCase):
    def write(self, root: Path, files: dict[str, str]) -> Path:
        for rel, text in files.items():
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        return root

    def test_module_source_validation(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            ModuleSource(path="agent")
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            ModuleSource(repo="https://x/y.git", ref="main")
        with self.assertRaisesRegex(ValueError, "relative path"):
            ModuleSource(repo="https://x/y.git", ref="a" * 40, path="../x")
        self.assertEqual(ModuleSource(repo="https://x/y.git", ref="a" * 40).path, ".")

    def test_repo_module_fetch_and_definition(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            mrepo, commit = make_module_repo(t)
            cache = ModuleCache(t / "cache")
            src = ModuleSource(repo=str(mrepo), ref=commit, path="agent")
            spec = resolve(cache.resolve(src, None), src)
            d = resolve_definition(spec)
            self.assertEqual(d.instructions, "You are the test module.\n")
            self.assertEqual([x.name for x in d.skill_dirs], ["mod-skill"])
            self.assertEqual(d.manifest["source"]["ref"], commit)
            self.assertEqual(d.manifest["plugin"]["version"], "1.2.0")
            self.assertIn(f"{NS}/AGENTS.md", d.manifest["files"])
            self.assertNotIn("../docs/purpose.md", d.manifest["files"])
            self.assertTrue(cache.cached(str(mrepo), commit))

    def test_plugin_json_is_the_marker_and_is_closed(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            with self.assertRaisesRegex(ValueError, "plugin.json not found"):
                load_local(self.write(t / "a", {"AGENTS.md": "x"}), {})
            old = self.write(t / "b", {"module.json": json.dumps({"schemaVersion": 1, "id": "m", "name": "m"})})
            with self.assertRaisesRegex(ValueError, "module.json is no longer supported"):
                load_local(old, {})
            bad = {
                "c": json.dumps({"$schema": PLUGIN_SCHEMA, "name": "m", "hooks": "hooks.json"}),  # closed schema
                "d": json.dumps({"$schema": "https://example.com/other.json", "name": "m"}),
                "e": json.dumps({"$schema": PLUGIN_SCHEMA, "name": "Bad Name"}),
                "f": json.dumps({"$schema": PLUGIN_SCHEMA, "name": "a--b"}),
                "g": plugin_json("m", extensions={NS: {"unknown": 1}}),
            }
            for d, text in bad.items():
                with self.subTest(d), self.assertRaises(ValueError):
                    load_local(self.write(t / d, {"plugin.json": text}), {})
            # other clients' extensions are ignored
            ok = self.write(t / "h", {"plugin.json": plugin_json("m", extensions={"com.vendor.x": {"a": 1}})})
            self.assertIsNone(load_local(ok, {}).instructions_text())

    def test_instructions_from_the_extension(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            m = self.write(t / "m", {"plugin.json": plugin_json("m", extensions={NS: {"instructions": "role.md"}}),
                                     f"{NS}/role.md": "role"})
            self.assertEqual(load_local(m, {}).instructions_text(), "role")
            missing = self.write(t / "n", {"plugin.json": plugin_json("m", extensions={NS: {"instructions": "x.md"}})})
            with self.assertRaisesRegex(ValueError, "instructions not found"):
                load_local(missing, {})

    def test_module_instructions_cannot_escape(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            m = self.write(t / "m", {"plugin.json": plugin_json("m", extensions={NS: {"instructions": "../../secret.md"}})})
            with self.assertRaises(ValueError):
                load_local(m, {})

    def test_skill_name_must_match_its_directory(self):
        with tempfile.TemporaryDirectory() as t:
            m = self.write(Path(t), {"plugin.json": plugin_json("m"),
                                     "skills/one/SKILL.md": "---\nname: two\ndescription: x\n---\n"})
            with self.assertRaisesRegex(ValueError, "name"):
                load_local(m, {})

    def test_skill_symlink_cannot_escape(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "secret").mkdir()
            (t / "secret" / "key").write_text("s3cret")
            m = self.write(t / "m", {"plugin.json": plugin_json("m")})
            (m / "skills").mkdir()
            (m / "skills" / "evil").symlink_to(t / "secret")
            with self.assertRaisesRegex(ValueError, "outside the module"):
                load_local(m, {})

    def test_mcp_servers_from_mcp_json(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            mrepo, _ = make_module_repo(t)
            mod = load_local(mrepo / "agent", {})
            self.assertEqual(
                mod.mcp_servers("/rt/module"),
                [{"name": "tool", "command": "/rt/module/bin/tool", "args": ["--root", "/rt/module"],
                  "env": [{"name": "MODE", "value": "x"}]}],
            )
            remote = self.write(t / "r", {"plugin.json": plugin_json("r"), "mcp.json": json.dumps(
                {"$schema": MCP_SCHEMA, "mcpServers": {"web": {"type": "http", "url": "https://mcp.example.com/mcp"}}})})
            self.assertEqual(load_local(remote, {}).mcp_servers("/x"),
                             [{"type": "http", "name": "web", "url": "https://mcp.example.com/mcp", "headers": []}])
            bad = {
                "reserved": {"agora": {"type": "stdio", "command": "x"}},
                "shell": {"s": {"type": "stdio", "command": "node server.js"}},
                "cwd": {"s": {"type": "stdio", "command": "x", "cwd": "${PLUGIN_ROOT}"}},
                "plain-http": {"s": {"type": "http", "url": "http://example.com/mcp"}},
                "type": {"s": {"type": "sse", "url": "https://example.com"}},
            }
            for d, servers in bad.items():
                with self.subTest(d), self.assertRaises(ValueError):
                    load_local(self.write(t / d, {"plugin.json": plugin_json("m"), "mcp.json": json.dumps(
                        {"$schema": MCP_SCHEMA, "mcpServers": servers})}), {})

    def test_builtin_tools_off_in_claude_settings(self):
        with tempfile.TemporaryDirectory() as t:
            p = make_spec(Path(t), ext={"tools": ["send", "receive"], "builtinTools": False})
            prepare_harness_config(p, Path(t) / "o", "/rt", "hi", [])
            settings = json.loads((Path(t) / "o/claude/settings.json").read_text())
            self.assertEqual(settings["permissions"]["deny"], list(CLAUDE_BUILTINS))
            # the agora tools the module gets are allowed outright
            self.assertEqual(settings["permissions"]["allow"], ["mcp__agora__send", "mcp__agora__receive"])


class DockerArgsTests(unittest.TestCase):
    def test_create_args(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"MY_TOKEN": "tok-123456"}):
            p = make_spec(Path(t), runtime={"env": {"CLAUDE_CODE_OAUTH_TOKEN": "${MY_TOKEN}"}})
        p.limits.memory, p.limits.cpus = "2g", 1.5
        image = "sha256:" + "a" * 64
        args = build_create_args("docker", "s-1", p, {"CLAUDE_CONFIG_DIR": "/home/agent/.agora/claude"}, "agora-net",
                                 image=image)
        self.assertEqual(args[:2], ["docker", "create"])
        self.assertEqual(args[-2:], [image, "claude-agent-acp"])
        self.assertNotIn("tok-123456", " ".join(args))
        joined = " ".join(args)
        for flag in ("--volume", "-v ", "--mount", "--privileged"):
            self.assertNotIn(flag, joined)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("--security-opt no-new-privileges", joined)
        self.assertIn("--network agora-net", joined)
        self.assertIn("--memory 2g", joined)
        i = args.index("CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(args[i - 1], "--env")  # name only, value comes from the CLI env

    def test_network_none(self):
        with tempfile.TemporaryDirectory() as t:
            p = make_spec(Path(t), network={"mode": "none"})
        args = build_create_args("docker", "s-1", p, {}, "agora-net")
        self.assertIn("none", args)
        self.assertNotIn("host.docker.internal:host-gateway", args)

    def test_pack_bundle_layout(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "b/workspace").mkdir(parents=True)
            (t / "b/workspace/a.txt").write_text("x")
            (t / "b/agora/claude").mkdir(parents=True)
            pack_bundle(t / "b", t / "b.tar")
            with tarfile.open(t / "b.tar") as tar:
                names = tar.getnames()
            self.assertIn("workspace/a.txt", names)
            self.assertIn("home/agent/.agora/claude", names)


class PermissionTests(unittest.TestCase):
    opts = [
        {"optionId": "a1", "kind": "allow_always"},
        {"optionId": "a", "kind": "allow_once"},
        {"optionId": "r", "kind": "reject_once"},
    ]

    def test_allow_prefers_once(self):
        out = decide_permission({"options": self.opts, "toolCall": {"kind": "edit"}}, Permissions())
        self.assertEqual(out["outcome"], {"outcome": "selected", "optionId": "a"})

    def test_kind_not_allowed(self):
        pol = Permissions(mode="allow", allow_kinds=["read"])
        out = decide_permission({"options": self.opts, "toolCall": {"kind": "execute"}}, pol)
        self.assertEqual(out["outcome"]["optionId"], "r")

    def test_deny_without_reject_option_cancels(self):
        out = decide_permission({"options": self.opts[:2], "toolCall": {}}, Permissions(mode="deny"))
        self.assertEqual(out["outcome"], {"outcome": "cancelled"})


class StoreTests(unittest.TestCase):
    def test_jsonl_and_index_agree_and_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as t:
            st = HistoryStore(t)
            st.add_secret("TOKEN", "super-secret-value")
            st.record("x", session_id="s1", text="token=super-secret-value")
            st.record("y", session_id="s2", n=1)
            lines = [json.loads(l) for f in sorted((Path(t) / "events").glob("*.jsonl")) for l in f.read_text().splitlines()]
            self.assertEqual([l["kind"] for l in lines], ["x", "y"])
            self.assertEqual(lines[0]["payload"]["text"], "token=[REDACTED:TOKEN]")
            self.assertEqual(st.events(session_id="s1")[0]["payload"], lines[0]["payload"])
            self.assertNotIn("super-secret-value", "".join(f.read_text() for f in (Path(t) / "events").glob("*")))
            st.close()

    def test_stale_sessions_marked_lost(self):
        with tempfile.TemporaryDirectory() as t:
            st = HistoryStore(t)
            base = {"name": "n", "kind": "managed", "depth": 1, "created_at": "2026-01-01", "meta": {}}
            st.upsert_session({**base, "id": "a", "state": "idle", "token_hash": "h1"})
            st.upsert_session({**base, "id": "b", "state": "terminated", "token_hash": "h2"})
            self.assertEqual(st.mark_stale_sessions(["idle", "busy"]), ["a"])
            states = {r["id"]: r["state"] for r in st.session_rows()}
            self.assertEqual(states, {"a": "lost", "b": "terminated"})
            st.close()

    def test_mailbox_filters(self):
        with tempfile.TemporaryDirectory() as t:
            st = HistoryStore(t)
            row = {"to_id": "me", "thread_id": "t", "delivery": "mailbox"}
            st.insert_message({**row, "id": "m1", "ts": "2026-01-02", "from_id": "a", "reply_to": None, "body": "1"})
            st.insert_message({**row, "id": "m2", "ts": "2026-01-03", "from_id": "b", "reply_to": "m9", "body": "2"})
            st.insert_message({**row, "id": "m3", "ts": "2026-01-04", "from_id": "a", "reply_to": None, "body": "3"})
            self.assertEqual([m["id"] for m in st.unread("me", from_id="a")], ["m1", "m3"])
            self.assertEqual([m["id"] for m in st.unread("me", reply_to="m9")], ["m2"])
            self.assertEqual([m["id"] for m in st.unread("me", limit=1)], ["m1"])
            self.assertEqual(st.unread_by_sender("me"), [("a", None, 2), ("b", None, 1)])
            st.mark_delivered("m2", "receive")
            self.assertEqual(st.message("m2")["delivery"], "receive")
            self.assertEqual(st.unread_by_sender("me"), [("a", None, 2)])
            st.close()

class CodexLoginTests(unittest.TestCase):
    def test_load_and_access_only_copy(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            make_codex_login(t / "login")
            login = load_chatgpt_login(t / "login")
            self.assertEqual((login.plan, login.email), ("plus", "me@example.com"))
            path = write_access_only(login, t / "session")
            copy = json.loads(path.read_text())
            self.assertEqual(copy["tokens"]["refresh_token"], "")
            self.assertEqual(copy["tokens"]["access_token"], login.auth["tokens"]["access_token"])
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(login.auth["tokens"]["refresh_token"], FAKE_REFRESH_TOKEN)  # original untouched
            self.assertIn("codex:refresh_token", login.secrets)
            self.assertNotIn("account_id", "".join(login.secrets))

    def test_expiry_and_bad_logins(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            make_codex_login(t / "soon", expires_in=3600)
            login = load_chatgpt_login(t / "soon")
            login.check_fresh(10 * 60)  # 10 min run + 30 min margin < 1 h
            with self.assertRaisesRegex(LoginError, "agora codex-login"):
                login.check_fresh(45 * 60)
            with self.assertRaisesRegex(LoginError, "no Codex login"):
                load_chatgpt_login(t / "missing")
            (t / "apikey").mkdir()
            (t / "apikey" / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-x", "tokens": None}))
            with self.assertRaisesRegex(LoginError, "not a ChatGPT login"):
                load_chatgpt_login(t / "apikey")
            (t / "noexp").mkdir()
            (t / "noexp" / "auth.json").write_text(json.dumps({"tokens": {"access_token": fake_jwt({"sub": "x"})}}))
            with self.assertRaisesRegex(LoginError, "no expiry"):
                load_chatgpt_login(t / "noexp")

    def test_codex_harness_config(self):
        with tempfile.TemporaryDirectory() as t:
            p = make_spec(Path(t), runtime={"harness": "codex", "login": "chatgpt"}, ext={"builtinTools": False})
            env = prepare_harness_config(p, Path(t) / "o", "/rt", "be brief", [])
            conf = (Path(t) / "o/codex/config.toml").read_text()
            self.assertIn('cli_auth_credentials_store = "file"', conf)
            self.assertIn('web_search = "disabled"', conf)
            self.assertEqual((env["NO_BROWSER"], env["INITIAL_AGENT_MODE"]), ("1", "read-only"))
            allow = make_spec(Path(t), runtime={"harness": "codex"})
            self.assertEqual(prepare_harness_config(allow, Path(t) / "b", "/rt", None, [])["INITIAL_AGENT_MODE"],
                             "workspace-write")

    def test_login_needs_codex(self):
        with tempfile.TemporaryDirectory() as t, self.assertRaisesRegex(Exception, "needs harness: codex"):
            make_spec(Path(t), runtime={"login": "chatgpt"})

    def test_bridge_tools_are_annotated(self):
        from agora.bridge import TOOLS

        ann = {t["name"]: t["annotations"] for t in TOOLS}
        for name in ("send", "receive", "spawn", "clone"):
            self.assertEqual((ann[name]["destructiveHint"], ann[name]["openWorldHint"]), (False, False), name)
        self.assertTrue(ann["terminate"]["destructiveHint"])
        for name in ("list_modules", "list_sessions", "status", "history"):
            self.assertTrue(ann[name]["readOnlyHint"], name)


class IdentityTests(unittest.TestCase):
    def test_ids_are_self_certifying(self):
        from agora.identity import Key, session_id_for, verify

        k = Key.generate()
        sid = session_id_for(k.public)
        self.assertRegex(sid, r"^s-[0-9a-f]{16}$")
        self.assertEqual(session_id_for(Key.from_hex(k.private_hex()).public), sid)
        sig = k.sign(b"data")
        self.assertTrue(verify(k.public_hex, sig, b"data"))
        self.assertFalse(verify(k.public_hex, sig, b"other"))
        self.assertFalse(verify(Key.generate().public_hex, sig, b"data"))
        self.assertFalse(verify("zz", sig, b"data"))

    def test_message_signature_covers_the_message_not_the_transport(self):
        from agora.identity import message_bytes

        env = {"id": "m-1", "from": "s-a@pc1", "to": "s-b@pc2", "thread_id": "t", "reply_to": None,
               "body": "こんにちは", "sent_at": "2026-10-21T00:00:00+00:00"}
        same = {**dict(reversed(list(env.items()))), "from_name": "a", "sig": "x"}
        self.assertEqual(message_bytes(env), message_bytes(same))
        self.assertNotEqual(message_bytes(env), message_bytes({**env, "to": "s-c@pc2"}))

    def test_addresses(self):
        from agora.identity import split_address

        self.assertEqual(split_address("s-1@pc1"), ("s-1", "pc1"))
        self.assertEqual(split_address("echo"), ("echo", None))
        for bad in ("@pc1", "s-1@"):
            with self.assertRaises(ValueError):
                split_address(bad)

    def test_place_key_file(self):
        import os
        import stat

        from agora.identity import load_or_create_key

        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "state" / "place.key"
            k = load_or_create_key(f)
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
            self.assertEqual(load_or_create_key(f).public_hex, k.public_hex)

    def test_place_requests_are_signed(self):
        from agora.federation import Federation, Peer
        from agora.identity import Key

        a, b = Federation("pc1", Key.generate()), Federation("pc2", Key.generate())
        b.add_peer(Peer("pc1", "http://x", a.key.public_hex))
        body = b'{"x": 1}'
        h = {k.lower(): v for k, v in a.sign_headers("POST", "/federation/deliver", body).items()}
        self.assertEqual(b.verify_request(h, "POST", "/federation/deliver", body), "pc1")
        for what, args in {
            "body": (h, "POST", "/federation/deliver", b"{}"),
            "path": (h, "POST", "/federation/call", body),
            "time": ({**h, "x-agora-time": str(int(h["x-agora-time"]) - 1000)}, "POST", "/federation/deliver", body),
            "place": ({**h, "x-agora-place": "pc3"}, "POST", "/federation/deliver", body),
        }.items():
            with self.assertRaises(PermissionError, msg=what):
                b.verify_request(*args)

    def test_place_config(self):
        from pydantic import ValidationError

        from agora.config import BrokerConfig, PeerSettings
        from agora.identity import Key

        self.assertRegex(BrokerConfig().place, r"^[a-z0-9][a-z0-9-]*$")
        with self.assertRaises(ValidationError):
            BrokerConfig(place="My PC")
        with self.assertRaises(ValidationError):
            PeerSettings(name="pc2", url="http://x", public_key="abc")
        ok = PeerSettings(name="pc2", url="http://x", public_key=Key.generate().public_hex.upper())
        self.assertEqual(ok.public_key, ok.public_key.lower())

    def test_store_keeps_signatures_and_migrates(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as t:
            db = sqlite3.connect(Path(t) / "index.sqlite3")
            db.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, ts TEXT NOT NULL, from_id TEXT NOT NULL, "
                       "to_id TEXT NOT NULL, thread_id TEXT NOT NULL, reply_to TEXT, body TEXT NOT NULL, "
                       "idempotency_key TEXT, delivery TEXT NOT NULL, delivered_at TEXT)")
            db.commit()
            db.close()
            st = HistoryStore(t)  # a v0.5 store gains the new columns
            seen = []
            st.listeners.append(lambda: seen.append(1))
            st.insert_message({"id": "m1", "ts": "1", "from_id": "s-a@pc1", "from_name": "a", "to_id": "me",
                               "thread_id": "t", "reply_to": None, "body": "b", "delivery": "mailbox", "sig": "ab"})
            self.assertEqual((st.message("m1")["sig"], st.message("m1")["from_name"]), ("ab", "a"))
            self.assertEqual(st.unread_by_sender("me"), [("s-a@pc1", "a", 1)])
            st.record("x")
            self.assertEqual(seen, [1])
            st.close()


if __name__ == "__main__":
    unittest.main()

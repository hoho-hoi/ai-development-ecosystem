import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from helpers import FAKE_DIGEST, ROOT, make_repo, write_profiles

from hbroker.acp_client import decide_permission
from hbroker.harness import prepare_harness_config
from hbroker.profiles import DEFAULT_STRIP, Permissions, load_profile, profile_from_dict
from hbroker.runner.docker import build_create_args, pack_bundle
from hbroker.store import HistoryStore
from hbroker.workspace import WorkspaceError, prepare_workspace


def minimal(**over):
    data = {
        "id": "p",
        "version": "1",
        "name": "P",
        "description": "d",
        "runtime": {"harness": "claude-code", "adapter": "claude-agent-acp", "model": "m", "image": FAKE_DIGEST},
    }
    for k, v in over.items():
        data[k] = {**data.get(k, {}), **v} if isinstance(v, dict) else v
    return data


class ProfileTests(unittest.TestCase):
    def test_examples_load(self):
        for f in (ROOT / "profiles" / "examples").glob("*.yaml"):
            p = load_profile(f)
            self.assertTrue(p.fingerprint.startswith("sha256:"), f)
            self.assertEqual(p.agent_card()["id"], p.id)

    def test_image_must_be_pinned(self):
        with self.assertRaisesRegex(ValueError, "pinned by digest"):
            profile_from_dict(minimal(runtime={"image": "harness-broker/claude-code:latest"}))
        profile_from_dict(minimal(runtime={"image": "reg.example/x@sha256:" + "b" * 64}))

    def test_ref_must_be_a_commit(self):
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            profile_from_dict(minimal(workspace={"repo": "/r", "ref": "main"}))
        with self.assertRaisesRegex(ValueError, "required"):
            profile_from_dict(minimal(workspace={"repo": "/r"}))

    def test_secret_names_only(self):
        with self.assertRaises(ValueError):
            profile_from_dict(minimal(runtime={"secrets": ["sk-ant-123"]}))

    def test_default_strip_and_extra(self):
        p = profile_from_dict(minimal(workspace={"stripExtra": ["*.local.json"]}))
        self.assertEqual(p.workspace.strip_names(), [*DEFAULT_STRIP, "*.local.json"])

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            profile_from_dict(minimal(runtime={"imgae": FAKE_DIGEST}))

    def test_fingerprint_tracks_referenced_files(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            repo, a, _ = make_repo(t)
            write_profiles(t / "profiles", repo, a)
            before = load_profile(t / "profiles" / "echo.yaml").fingerprint
            (t / "profiles" / "echo-instructions.md").write_text("changed\n")
            self.assertNotEqual(before, load_profile(t / "profiles" / "echo.yaml").fingerprint)


class WorkspaceTests(unittest.TestCase):
    def test_pinned_and_stripped(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            repo, a, b = make_repo(t)
            p = profile_from_dict(minimal(workspace={"repo": str(repo), "ref": a}))
            m = prepare_workspace(p, t / "ws")
            self.assertEqual(m["head"], a)
            files = sorted(str(f.relative_to(t / "ws")) for f in (t / "ws").rglob("*") if ".git" not in f.parts)
            self.assertEqual(files, ["README.md", "docs", "src", "src/app.py"])
            self.assertIn(".claude/", m["removed"])
            self.assertIn("src/AGENTS.md", m["removed"])
            self.assertIn("src/.codex/", m["removed"])
            self.assertFalse((t / "ws" / "later.txt").exists())  # commit b is not there
            self.assertTrue((t / "ws" / ".git").is_dir())

            m2 = prepare_workspace(p, t / "ws2", ref_override=b)
            self.assertEqual(m2["head"], b)
            self.assertTrue((t / "ws2" / "later.txt").exists())

    def test_override_must_be_sha(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            repo, a, _ = make_repo(t)
            p = profile_from_dict(minimal(workspace={"repo": str(repo), "ref": a}))
            with self.assertRaises(WorkspaceError):
                prepare_workspace(p, t / "ws", ref_override="main")

    def test_no_repo_gives_empty_workspace(self):
        with tempfile.TemporaryDirectory() as t:
            p = profile_from_dict(minimal())
            m = prepare_workspace(p, Path(t) / "ws")
            self.assertEqual(m["removed"], [])
            self.assertEqual(list((Path(t) / "ws").iterdir()), [])


class HarnessConfigTests(unittest.TestCase):
    def test_claude_code_config(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            repo, a, _ = make_repo(t)
            write_profiles(t / "profiles", repo, a)
            p = load_profile(t / "profiles" / "echo.yaml")
            env = prepare_harness_config(p, t / "hb", "/home/agent/.hb")
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/agent/.hb/claude")
            self.assertEqual(env["ANTHROPIC_MODEL"], "test-model-1")
            self.assertEqual((t / "hb/claude/CLAUDE.md").read_text(), "You are the echo profile.\n")
            self.assertTrue((t / "hb/claude/skills/echo-skill/SKILL.md").is_file())

    def test_codex_without_instructions_disables_agents_md(self):
        with tempfile.TemporaryDirectory() as t:
            p = profile_from_dict(minimal(runtime={"harness": "codex", "adapter": "codex-acp"}))
            env = prepare_harness_config(p, Path(t), "/rt")
            self.assertEqual(env["CODEX_HOME"], "/rt/codex")
            self.assertIn("project_doc_max_bytes = 0", (Path(t) / "codex/config.toml").read_text())


class DockerArgsTests(unittest.TestCase):
    def test_create_args(self):
        p = profile_from_dict(
            minimal(runtime={"secrets": ["CLAUDE_CODE_OAUTH_TOKEN"]}, limits={"memory": "2g", "cpus": 1.5})
        )
        args = build_create_args("docker", "s-1", p, {"CLAUDE_CONFIG_DIR": "/home/agent/.hb/claude"}, "hb-net")
        self.assertEqual(args[:2], ["docker", "create"])
        self.assertEqual(args[-2:], [FAKE_DIGEST, "claude-agent-acp"])
        joined = " ".join(args)
        for flag in ("--volume", "-v ", "--mount", "--privileged"):
            self.assertNotIn(flag, joined)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("--security-opt no-new-privileges", joined)
        self.assertIn("--network hb-net", joined)
        self.assertIn("--memory 2g", joined)
        i = args.index("CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(args[i - 1], "--env")  # name only, value comes from the CLI env

    def test_network_none(self):
        p = profile_from_dict(minimal(network={"mode": "none"}))
        args = build_create_args("docker", "s-1", p, {}, "hb-net")
        self.assertIn("none", args)
        self.assertNotIn("host.docker.internal:host-gateway", args)

    def test_pack_bundle_layout(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "b/workspace").mkdir(parents=True)
            (t / "b/workspace/a.txt").write_text("x")
            (t / "b/hb/claude").mkdir(parents=True)
            pack_bundle(t / "b", t / "b.tar")
            with tarfile.open(t / "b.tar") as tar:
                names = tar.getnames()
            self.assertIn("workspace/a.txt", names)
            self.assertIn("home/agent/.hb/claude", names)


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
        pol = Permissions(mode="allow", allowKinds=["read"])
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


if __name__ == "__main__":
    unittest.main()

import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from helpers import FAKE_DIGEST, FAKE_REFRESH_TOKEN, ROOT, fake_jwt, make_codex_login, make_module_repo, make_repo, write_profiles

from hbroker.acp_client import decide_permission
from hbroker.harness import prepare_harness_config, resolve_definition
from hbroker.logins import LoginError, load_chatgpt_login, write_access_only
from hbroker.modules import ModuleCache, load_local, read_spec
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
            env = prepare_harness_config(p, t / "hb", "/home/agent/.hb", p.instructions_text(), p.skill_dirs())
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/agent/.hb/claude")
            self.assertEqual(env["ANTHROPIC_MODEL"], "test-model-1")
            self.assertEqual((t / "hb/claude/CLAUDE.md").read_text(), "You are the echo profile.\n")
            self.assertTrue((t / "hb/claude/skills/echo-skill/SKILL.md").is_file())

    def test_codex_without_instructions_disables_agents_md(self):
        with tempfile.TemporaryDirectory() as t:
            p = profile_from_dict(minimal(runtime={"harness": "codex", "adapter": "codex-acp"}))
            env = prepare_harness_config(p, Path(t), "/rt", None, [])
            self.assertEqual(env["CODEX_HOME"], "/rt/codex")
            self.assertIn("project_doc_max_bytes = 0", (Path(t) / "codex/config.toml").read_text())


class ModuleTests(unittest.TestCase):
    def test_module_ref_validation(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            profile_from_dict(minimal(module={"path": "execution"}))
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            profile_from_dict(minimal(module={"repo": "https://x/y.git", "ref": "main"}))
        with self.assertRaisesRegex(ValueError, "relative path"):
            profile_from_dict(minimal(module={"repo": "https://x/y.git", "ref": "a" * 40, "path": "../x"}))
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            profile_from_dict(minimal(module={"repo": "https://x/y.git", "ref": "a" * 40},
                                      runtime={"instructions": "./x.md"}))
        with self.assertRaisesRegex(ValueError, "name and description"):
            data = minimal()
            del data["name"]
            profile_from_dict(data)

    def test_local_module_names_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            mrepo, _ = make_module_repo(t)
            data = minimal(module={"local": str(mrepo)})
            del data["name"], data["description"]
            p = profile_from_dict(data)
            self.assertEqual(p.agent_card()["name"], "Test module")
            self.assertEqual(p.agent_card()["description"], "a module for tests")
            before = p.fingerprint
            (mrepo / "execution" / "AGENTS.md").write_text("changed\n")
            self.assertNotEqual(before, profile_from_dict(data).fingerprint)

    def test_repo_module_fetch_and_definition(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            mrepo, commit = make_module_repo(t)
            cache = ModuleCache(t / "cache")
            bare = minimal(module={"repo": str(mrepo), "ref": commit})
            del bare["description"]
            self.assertIn("not fetched yet", profile_from_dict(bare, cache=cache).agent_card()["description"])
            p = profile_from_dict(minimal(module={"repo": str(mrepo), "ref": commit}), cache=cache)
            instructions, skills, manifest = resolve_definition(p, cache)
            self.assertEqual(instructions, "You are the test module.\n")
            self.assertEqual([d.name for d in skills], ["mod-skill"])
            self.assertEqual(manifest["source"]["ref"], commit)
            self.assertIn("AGENTS.md", manifest["files"])
            self.assertNotIn("../requirements/purpose.md", manifest["files"])
            # cached now: the registry shows the module's own name
            p2 = profile_from_dict(minimal(module={"repo": str(mrepo), "ref": commit}), cache=cache)
            self.assertEqual(p2.agent_card()["description"], "d")  # profile description wins
            data = minimal(module={"repo": str(mrepo), "ref": commit})
            del data["description"]
            self.assertEqual(profile_from_dict(data, cache=cache).agent_card()["description"], "a module for tests")

    def test_module_instructions_cannot_escape(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "m").mkdir()
            (t / "m" / "module.json").write_text(json.dumps(
                {"schemaVersion": 1, "id": "m", "name": "m", "instructions": "../secret.md"}))
            with self.assertRaises(ValueError):
                read_spec(t / "m")

    def test_skill_symlink_cannot_escape(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "secret").mkdir()
            (t / "secret" / "key").write_text("s3cret")
            m = t / "m"
            (m / "skills").mkdir(parents=True)
            (m / "module.json").write_text(json.dumps({"schemaVersion": 1, "id": "m", "name": "m"}))
            (m / "skills" / "evil").symlink_to(t / "secret")
            with self.assertRaisesRegex(ValueError, "outside the module"):
                load_local(m, {"local": str(m)})

    def test_denied_tools_in_claude_settings(self):
        with tempfile.TemporaryDirectory() as t:
            p = profile_from_dict(minimal(runtime={"permissions": {"deniedTools": ["Bash", "WebFetch"]},
                                                   "brokerTools": ["send", "receive"]}))
            prepare_harness_config(p, Path(t), "/rt", "hi", [])
            settings = json.loads((Path(t) / "claude/settings.json").read_text())
            self.assertEqual(settings["permissions"]["deny"], ["Bash", "WebFetch"])
            # the broker tools the profile grants are allowed outright
            self.assertEqual(settings["permissions"]["allow"], ["mcp__hb__send", "mcp__hb__receive"])


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

    def test_broker_tools_setting(self):
        base = {"id": "p", "version": "1", "name": "p", "description": "d",
                "runtime": {"harness": "claude-code", "adapter": "x", "model": "m", "image": FAKE_DIGEST}}
        def tools(v):
            data = json.loads(json.dumps(base))
            if v is not None:
                data["runtime"]["brokerTools"] = v
            return profile_from_dict(data, Path(".")).runtime.broker_tools
        self.assertEqual(tools(None)[:2], ["send", "receive"])
        self.assertEqual(len(tools(True)), 9)
        self.assertEqual(tools(False), [])
        self.assertEqual(tools(["send"]), ["send"])
        with self.assertRaises(Exception):
            tools(["send", "spawm"])
        data = json.loads(json.dumps(base))
        data["runtime"]["messageFormat"] = "body"  # removed in v0.4
        with self.assertRaises(Exception):
            profile_from_dict(data, Path("."))

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
            with self.assertRaisesRegex(LoginError, "hb codex-login"):
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
            p = profile_from_dict(minimal(runtime={"harness": "codex", "adapter": "codex-acp", "login": "chatgpt",
                                                   "permissions": {"mode": "deny"}}))
            env = prepare_harness_config(p, Path(t), "/rt", "be brief", [])
            conf = (Path(t) / "codex/config.toml").read_text()
            self.assertIn('cli_auth_credentials_store = "file"', conf)
            self.assertIn('web_search = "disabled"', conf)
            self.assertEqual((env["NO_BROWSER"], env["INITIAL_AGENT_MODE"]), ("1", "read-only"))
            allow = profile_from_dict(minimal(runtime={"harness": "codex", "adapter": "codex-acp"}))
            self.assertEqual(prepare_harness_config(allow, Path(t) / "b", "/rt", None, [])["INITIAL_AGENT_MODE"],
                             "workspace-write")

    def test_login_needs_codex(self):
        with self.assertRaisesRegex(Exception, "needs runtime.harness: codex"):
            profile_from_dict(minimal(runtime={"login": "chatgpt"}))

    def test_bridge_tools_are_annotated(self):
        from hbroker.bridge import TOOLS

        ann = {t["name"]: t["annotations"] for t in TOOLS}
        for name in ("send", "receive", "spawn", "clone"):
            self.assertEqual((ann[name]["destructiveHint"], ann[name]["openWorldHint"]), (False, False), name)
        self.assertTrue(ann["terminate"]["destructiveHint"])
        for name in ("list_profiles", "list_sessions", "status", "history"):
            self.assertTrue(ann[name]["readOnlyHint"], name)


class IdentityTests(unittest.TestCase):
    def test_ids_are_self_certifying(self):
        from hbroker.identity import Key, session_id_for, verify

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
        from hbroker.identity import message_bytes

        env = {"id": "m-1", "from": "s-a@pc1", "to": "s-b@pc2", "thread_id": "t", "reply_to": None,
               "body": "こんにちは", "sent_at": "2026-10-21T00:00:00+00:00"}
        same = {**dict(reversed(list(env.items()))), "from_name": "a", "sig": "x"}
        self.assertEqual(message_bytes(env), message_bytes(same))
        self.assertNotEqual(message_bytes(env), message_bytes({**env, "to": "s-c@pc2"}))

    def test_addresses(self):
        from hbroker.identity import split_address

        self.assertEqual(split_address("s-1@pc1"), ("s-1", "pc1"))
        self.assertEqual(split_address("echo"), ("echo", None))
        for bad in ("@pc1", "s-1@"):
            with self.assertRaises(ValueError):
                split_address(bad)

    def test_place_key_file(self):
        import os
        import stat

        from hbroker.identity import load_or_create_key

        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "state" / "place.key"
            k = load_or_create_key(f)
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
            self.assertEqual(load_or_create_key(f).public_hex, k.public_hex)

    def test_place_requests_are_signed(self):
        from hbroker.federation import Federation, Peer
        from hbroker.identity import Key

        a, b = Federation("pc1", Key.generate()), Federation("pc2", Key.generate())
        b.add_peer(Peer("pc1", "http://x", a.key.public_hex))
        body = b'{"x": 1}'
        h = {k.lower(): v for k, v in a.sign_headers("POST", "/federation/deliver", body).items()}
        self.assertEqual(b.verify_request(h, "POST", "/federation/deliver", body), "pc1")
        for what, args in {
            "body": (h, "POST", "/federation/deliver", b"{}"),
            "path": (h, "POST", "/federation/call", body),
            "time": ({**h, "x-hb-time": str(int(h["x-hb-time"]) - 1000)}, "POST", "/federation/deliver", body),
            "place": ({**h, "x-hb-place": "pc3"}, "POST", "/federation/deliver", body),
        }.items():
            with self.assertRaises(PermissionError, msg=what):
                b.verify_request(*args)

    def test_place_config(self):
        from pydantic import ValidationError

        from hbroker.config import BrokerConfig, PeerSettings
        from hbroker.identity import Key

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

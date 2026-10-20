"""End-to-end: broker over HTTP, local runner, fake ACP agent, and the stdio
bridge driven by the official MCP SDK client.

The fake agent answers the way every agent must: with send(reply_to=...).
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import (
    BRIDGE,
    FAKE_REFRESH_TOKEN,
    RunningBroker,
    make_codex_login,
    make_repo,
)

SECRET = "hb-secret-value-123456"


class BrokerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo, self.commit_a, self.commit_b = make_repo(self.tmp)
        os.environ["HB_TEST_SECRET"] = SECRET
        os.environ["HB_TEST_HOST_ONLY"] = "should-not-leak"
        self.b = RunningBroker(self.tmp, self.repo, self.commit_a)
        await self.b.start()

    async def asyncTearDown(self):
        await self.b.stop()
        os.environ.pop("HB_TEST_SECRET", None)
        os.environ.pop("HB_TEST_HOST_ONLY", None)
        self._tmp.cleanup()

    async def reply_to(self, message_id, token=None, wait=20, sender=None):
        """receive the reply to `message_id` (from `sender` when given)."""
        args = {"reply_to": message_id, "wait": wait}
        if sender:
            args["from"] = sender
        res = await self.b.tool("receive", token, **args)
        if not res["messages"]:
            self.fail(f"no reply to {message_id}: {res}")
        self.assertEqual(len(res["messages"]), 1, res)
        return res["messages"][0]

    async def ask(self, to, body, token=None, wait=30):
        sent = await self.b.tool("send", token, to=to, body=body)
        return sent, await self.reply_to(sent["message_id"], token, wait, sender=sent["to"])

    def first_message_id(self, session_id):
        ev = self.b.broker.store.events(session_id="operator", kinds=["message.send"])
        return [e for e in ev if e["payload"]["to"] == self.b.address(session_id)][0]["message_id"]

    async def turn_text(self, sid, message_id):
        ev = (await self.b.tool("history", session=sid, kinds=["turn.end"]))["events"]
        return next(e["payload"]["text"] for e in ev if e["message_id"] == message_id)


class LifecycleTests(BrokerCase):
    async def test_spawn_task_reply_history_terminate(self):
        st = await self.b.tool("spawn", profile="echo", task="hello there", wait=True)
        self.assertEqual(st["state"], "idle", st)
        sid = st["id"]
        self.assertEqual(st["task_message_id"], self.first_message_id(sid))
        reply = await self.reply_to(st["task_message_id"], sender=sid)
        self.assertEqual(reply["body"], "echo: hello there")
        self.assertEqual(reply["from"], st["address"])
        self.assertEqual(st["address"], f"{sid}@pc1")

        # history: raw bodies, the exact injected text, the turn's text, ACP frames
        hist = (await self.b.tool("history", thread_id=reply["thread_id"]))["events"]
        kinds = [e["kind"] for e in hist]
        for k in ("message.send", "message.inject", "turn.end", "message.deliver"):
            self.assertIn(k, kinds)
        inject = next(e for e in hist if e["kind"] == "message.inject")
        self.assertEqual(
            inject["payload"]["text"],
            f"from: operator@pc1\nmessage_id: {st['task_message_id']}\nthread_id: {reply['thread_id']}\n\nhello there",
        )
        self.assertEqual([e["payload"]["via"] for e in hist if e["kind"] == "message.deliver"], ["turn", "receive"])
        sends = [e for e in hist if e["kind"] == "message.send"]
        self.assertEqual([e["session_id"] for e in sends], ["operator", sid])  # nothing written by the broker
        acp = (await self.b.tool("history", session=sid, kinds=["acp.out"]))["events"]
        methods = [e["payload"]["frame"].get("method") for e in acp]
        self.assertEqual(methods[:3], ["initialize", "session/new", "session/prompt"])
        spawn_ev = (await self.b.tool("history", session=sid, kinds=["session.spawn"]))["events"][0]
        self.assertEqual(spawn_ev["payload"]["profile"]["id"], "echo")
        self.assertTrue(spawn_ev["payload"]["fingerprint"].startswith("sha256:"))

        # tokens and declared secrets never reach disk
        everything = "".join(f.read_text() for f in (self.b.cfg.history_dir / "events").glob("*.jsonl"))
        session = self.b.broker.manager.sessions[sid]
        self.assertNotIn(session.token, everything)
        self.assertIn("[REDACTED:token:", everything)

        done = await self.b.tool("terminate", session=sid, reason="test over")
        self.assertEqual(done["state"], "terminated")
        transcript = self.b.cfg.history_dir / "sessions" / sid / "harness" / "projects" / "transcript.jsonl"
        self.assertTrue(transcript.is_file(), "harness records were not collected")
        self.assertFalse((self.b.cfg.state_dir / "bundles" / sid).exists(), "bundle not removed")
        listed = (await self.b.tool("list_sessions"))["sessions"]
        self.assertNotIn(sid, [s["id"] for s in listed])

    async def test_isolation_of_environment(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        _, r = await self.ask(st["id"], "ENV")
        env = json.loads(r["body"])
        self.assertTrue(env["home"].startswith(str(self.b.cfg.state_dir / "bundles")), env["home"])
        self.assertEqual(env["files"], ["README.md", "docs", "src", "src/app.py"])  # stripped, pinned to commit a
        self.assertIn("CLAUDE.md", env["config_files"])  # the profile's instructions
        self.assertIn("skills/echo-skill/SKILL.md", env["config_files"])
        self.assertNotIn("skills/evil/SKILL.md", env["config_files"])
        self.assertEqual(env["model"], "test-model-1")
        self.assertEqual(env["secret"], SECRET)  # declared secret passed through
        self.assertIsNone(env["leaked_host_var"])  # nothing else from the host env

    async def test_bridge_entry_given_to_agent(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        _, r = await self.ask(st["id"], "MCP")
        servers = json.loads(r["body"])
        self.assertEqual(servers[0]["name"], "hb")
        env = {e["name"]: e["value"] for e in servers[0]["env"]}
        self.assertEqual(env["HB_TOKEN"], self.b.broker.manager.sessions[st["id"]].token)
        self.assertEqual(set(env), {"HB_BROKER_URL", "HB_TOKEN"})  # all tools: no HB_TOOLS

    async def test_final_text_reaches_no_one(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        sent = await self.b.tool("send", to=st["id"], body="FINAL_ONLY")
        t0 = time.monotonic()
        r = await self.b.tool("receive", **{"from": st["id"], "wait": 20})
        self.assertEqual(r["messages"], [])
        self.assertTrue(r["from_quiet"], r)  # it went quiet: no need to wait the full 20 s
        self.assertEqual(r["from_state"], "idle")
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(await self.turn_text(st["id"], sent["message_id"]), "this final text goes to no one")
        self.assertEqual((await self.b.tool("receive"))["messages"], [])

    async def test_quiet_when_the_other_side_fails(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        await self.b.tool("send", to=st["id"], body="CRASH")
        t0 = time.monotonic()
        r = await self.b.tool("receive", **{"from": st["id"], "wait": 20})
        self.assertEqual((r["messages"], r["from_quiet"], r["from_state"]), ([], True, "failed"))
        self.assertLess(time.monotonic() - t0, 10)
        s = await self.b.tool("status", session=st["id"])
        self.assertTrue("exited" in s["end_reason"] or "prompt failed" in s["end_reason"], s["end_reason"])

    async def test_waiting_is_not_cut_short_while_work_is_pending(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        sent = await self.b.tool("send", to=st["id"], body="SLEEP 1")
        r = await self.b.tool("receive", **{"from": st["id"], "wait": 20})  # busy: wait for the reply
        self.assertEqual([m["reply_to"] for m in r["messages"]], [sent["message_id"]])

    async def test_managed_session_talks_through_bridge(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        _, r = await self.ask(st["id"], 'BRIDGE status {"session": "self"}')
        result = json.loads(r["body"])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(result["content"][0]["text"])["id"], st["id"])
        self.assertEqual(len(result["content"]), 1)  # nothing unread, so no notice

        # a message to someone else, then the answer to the operator
        _, answer = await self.ask(st["id"], 'BRIDGE send {"to": "operator", "body": "hi from inside"}')
        self.assertFalse(json.loads(answer["body"])["isError"])
        got = (await self.b.tool("receive", **{"from": st["id"]}))["messages"]
        self.assertEqual([(m["body"], m["reply_to"]) for m in got], [("hi from inside", None)])

    async def test_reply_without_to_goes_to_the_sender(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        await self.ask(st["id"], 'BRIDGE send {"to": "operator", "body": "a question for you"}')
        question = (await self.b.tool("receive", **{"from": st["id"]}))["messages"][0]
        answer = await self.b.tool("send", reply_to=question["message_id"], body="my answer")
        self.assertEqual((answer["to"], answer["thread_id"]), (st["address"], question["thread_id"]))
        echo = await self.reply_to(answer["message_id"], sender=st["id"])
        self.assertEqual(echo["body"], "echo: my answer")
        inject = (await self.b.tool("history", session=st["id"], kinds=["message.inject"]))["events"][-1]
        self.assertIn(f"in_reply_to: {question['message_id']}", inject["payload"]["text"])
        # a reply needs a message that was addressed to you
        code, body = await self.b.post("/api/tools/send", {"reply_to": answer["message_id"], "body": "x"})
        self.assertEqual(code, 400, body)
        code, body = await self.b.post("/api/tools/send", {"body": "nowhere"})
        self.assertEqual(code, 400, body)

    async def test_notice_lists_unread_mid_turn(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        first = await self.b.tool("send", to=st["id"], body='DELAY 0.5 BRIDGE status {"session": "self"}')
        await asyncio.sleep(0.1)
        second = await self.b.tool("send", to=st["id"], body="waiting in the mailbox")
        self.assertEqual(second["to_state"], "busy")
        result = json.loads((await self.reply_to(first["message_id"], sender=st["id"]))["body"])
        self.assertEqual(len(result["content"]), 2, result)
        notice = result["content"][1]["text"]
        self.assertTrue(notice.startswith("You have 1 unread message: 1 from operator"), notice)
        # the notice does not consume it: it starts the next turn
        self.assertEqual((await self.reply_to(second["message_id"]))["body"], "echo: waiting in the mailbox")

    async def test_receive_mid_turn(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        first = await self.b.tool("send", to=st["id"], body='BRIDGE receive {"from": "operator", "wait": 20}')
        await asyncio.sleep(0.5)
        later = await self.b.tool("send", to=st["id"], body="for your receive")
        result = json.loads((await self.reply_to(first["message_id"], sender=st["id"]))["body"])
        got = json.loads(result["content"][0]["text"])["messages"]
        self.assertEqual([m["message_id"] for m in got], [later["message_id"]])
        deliver = (await self.b.tool("history", session=st["id"], kinds=["message.deliver"]))["events"]
        self.assertEqual({e["message_id"]: e["payload"]["via"] for e in deliver}[later["message_id"]], "receive")
        # taken mid-turn, so it started no turn of its own and nothing answers it
        r = await self.b.tool("receive", **{"from": st["id"], "reply_to": later["message_id"], "wait": 10})
        self.assertEqual((r["messages"], r["from_quiet"]), ([], True))

    async def test_unread_messages_dropped_at_end(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        await self.b.tool("send", to=st["id"], body="SLEEP 5")
        await asyncio.sleep(0.2)
        waiting = await self.b.tool("send", to=st["id"], body="never read")
        await self.b.tool("terminate", session=st["id"], reason="stop now")
        dropped = (await self.b.tool("history", session=st["id"], kinds=["message.dropped"]))["events"]
        self.assertEqual([e["message_id"] for e in dropped], [waiting["message_id"]])
        self.assertEqual((await self.b.tool("receive"))["messages"], [])  # the broker sends nobody anything
        code, _ = await self.b.post("/api/tools/send", {"to": st["id"], "body": "too late"})
        self.assertEqual(code, 404)

    async def test_messages_queue_while_busy(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        first = await self.b.tool("send", to=st["id"], body="SLEEP 1")
        await asyncio.sleep(0.2)
        second = await self.b.tool("send", to=st["id"], body="second")
        third = await self.b.tool("send", to=st["id"], body="third")
        self.assertEqual((second["to_state"], second["queued_ahead"]), ("busy", 0))
        self.assertEqual(third["queued_ahead"], 1)
        self.assertEqual((await self.b.tool("status", session=st["id"]))["unread"], 2)
        r1 = await self.reply_to(first["message_id"])
        r2 = await self.reply_to(second["message_id"])
        r3 = await self.reply_to(third["message_id"])
        self.assertEqual([r["body"] for r in (r1, r2, r3)], ["echo: SLEEP 1", "echo: second", "echo: third"])
        self.assertLess(r1["sent_at"], r2["sent_at"])
        self.assertLess(r2["sent_at"], r3["sent_at"])

    async def test_permissions_follow_profile(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        _, ok = await self.ask(st["id"], "PERMISSION edit")
        _, no = await self.ask(st["id"], "PERMISSION execute")
        self.assertEqual(json.loads(ok["body"])["outcome"]["optionId"], "allow-once")
        self.assertEqual(json.loads(no["body"])["outcome"]["optionId"], "reject-once")
        perms = (await self.b.tool("history", session=st["id"], kinds=["acp.permission"]))["events"]
        self.assertEqual(len(perms), 2)

    async def test_idempotent_send(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        a = await self.b.tool("send", to=st["id"], body="once", idempotency_key="k1")
        b = await self.b.tool("send", to=st["id"], body="once", idempotency_key="k1")
        self.assertEqual(a["message_id"], b["message_id"])
        self.assertTrue(b.get("duplicate"))


class ModuleProfileTests(BrokerCase):
    async def test_local_module_per_message_with_send_only(self):
        st = await self.b.tool("spawn", profile="modlocal", wait=True)
        self.assertEqual(st["state"], "idle", st)
        sid = st["id"]

        # the prompt is the header plus the body
        sent, r = await self.ask(sid, "RAW")
        self.assertEqual(
            json.loads(r["body"])["prompt"],
            f"from: operator@pc1\nmessage_id: {sent['message_id']}\nthread_id: {sent['thread_id']}\n\nRAW",
        )

        # the module's instructions and skills, and the denied tools, reach the harness
        _, r = await self.ask(sid, "INSTRUCTIONS")
        found = json.loads(r["body"])
        self.assertEqual(found["CLAUDE.md"], "You are the test module.\n")
        self.assertEqual(found["skills"], ["mod-skill"])
        self.assertEqual(found["settings"]["permissions"]["deny"], ["Bash", "WebSearch"])
        self.assertEqual(found["settings"]["permissions"]["allow"], ["mcp__hb__send"])  # never blocked by "deny"

        # per_message: every message gets a fresh conversation; the old one is closed
        _, r = await self.ask(sid, "SESSION")
        info = json.loads(r["body"])
        self.assertEqual(info["prompts_in_session"], 1)
        self.assertEqual(info["session"], "fake-session-3")  # 3rd message, 3rd conversation
        self.assertEqual(info["closed"], ["fake-session-1", "fake-session-2"])

        # brokerTools: [send] -> the bridge offers send only, and the broker enforces it
        _, r = await self.ask(sid, "MCP")
        env = {e["name"]: e["value"] for e in json.loads(r["body"])[0]["env"]}
        self.assertEqual(env["HB_TOOLS"], "send")
        _, r = await self.ask(sid, 'BRIDGE spawn {"profile": "echo"}')
        self.assertEqual(json.loads(r["body"])["rpc_error"]["code"], -32602)  # not offered
        token = self.b.broker.manager.sessions[sid].token
        code, body = await self.b.post("/api/tools/spawn", {"profile": "echo"}, token)
        self.assertEqual(code, 403, body)
        self.assertIn("not among this session's tools", body["error"])

        # module source and file hashes are in the history
        mod = (await self.b.tool("history", session=sid, kinds=["session.module"]))["events"][0]["payload"]
        self.assertEqual(mod["spec"]["id"], "mod")
        self.assertIn("AGENTS.md", mod["files"])

    async def test_no_broker_tools_means_no_bridge(self):
        st = await self.b.tool("spawn", profile="notools", wait=True)
        sent = await self.b.tool("send", to=st["id"], body="MCP")
        r = await self.b.tool("receive", **{"from": st["id"], "wait": 20})
        self.assertEqual((r["messages"], r["from_quiet"]), ([], True))  # it cannot answer at all
        self.assertEqual(json.loads(await self.turn_text(st["id"], sent["message_id"])), [])

    async def test_repo_module_pinned_by_commit(self):
        profiles = {p["id"]: p for p in (await self.b.tool("list_profiles"))["profiles"]}
        self.assertEqual(profiles["modlocal"]["name"], "Test module")
        self.assertIn("not fetched yet", profiles["modrepo"]["description"])
        st = await self.b.tool("spawn", profile="modrepo", wait=True)
        self.assertEqual(st["state"], "idle", st)
        _, r = await self.ask(st["id"], "INSTRUCTIONS")
        self.assertEqual(json.loads(r["body"])["CLAUDE.md"], "You are the peer.\n")
        mod = (await self.b.tool("history", session=st["id"], kinds=["session.module"]))["events"][0]["payload"]
        self.assertEqual(mod["source"]["path"], "requirements/peer")
        self.assertEqual(len(mod["source"]["ref"]), 40)
        profiles = {p["id"]: p for p in (await self.b.tool("list_profiles"))["profiles"]}
        self.assertEqual(profiles["modrepo"]["name"], "Peer")  # cached now

    async def test_receive_wait_is_bounded_and_filters(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        slow = await self.b.tool("send", to=st["id"], body="SLEEP 1.5")
        other = await self.b.tool("spawn", profile="echo", name="other", wait=True)
        quick = await self.b.tool("send", to=other["id"], body="fast")
        r = await self.b.tool("receive", reply_to=slow["message_id"], wait=0.3)
        self.assertEqual(r["messages"], [])  # timed out; the fast reply is not taken by this filter
        late = await self.reply_to(slow["message_id"])
        self.assertEqual(late["body"], "echo: SLEEP 1.5")
        got = (await self.b.tool("receive", **{"from": "other"}))["messages"]
        self.assertEqual([m["reply_to"] for m in got], [quick["message_id"]])


class CodexLoginTests(BrokerCase):
    """runtime.login: chatgpt — sessions get an access-only copy of the
    broker's plan login; the refresh token never leaves the broker."""

    async def test_session_gets_access_only_copy(self):
        st = await self.b.tool("spawn", profile="codexplan", wait=True)
        self.assertEqual(st["state"], "idle", st)
        _, r = await self.ask(st["id"], "CODEX_AUTH")
        seen = json.loads(r["body"])
        self.assertTrue(seen["auth_file"])
        self.assertTrue(seen["has_access_token"])
        self.assertEqual(seen["refresh_token"], "")  # blanked
        self.assertIn('cli_auth_credentials_store = "file"', seen["config"])
        self.assertIn('web_search = "disabled"', seen["config"])
        self.assertEqual(seen["env"], {"NO_BROWSER": "1", "INITIAL_AGENT_MODE": "read-only"})
        login = (await self.b.tool("history", session=st["id"], kinds=["session.login"]))["events"][0]["payload"]
        self.assertEqual((login["login"], login["plan"]), ("chatgpt", "plus"))
        # the broker's own login is untouched
        own = json.loads((self.b.cfg.codex_login_path / "auth.json").read_text())
        self.assertEqual(own["tokens"]["refresh_token"], FAKE_REFRESH_TOKEN)

        await self.b.tool("terminate", session=st["id"])
        records = self.b.cfg.history_dir / "sessions" / st["id"] / "harness"
        self.assertTrue((records / "projects" / "transcript.jsonl").is_file())
        self.assertEqual(list(records.rglob("auth.json")), [])  # never collected
        everything = "".join(f.read_text() for f in (self.b.cfg.history_dir / "events").glob("*.jsonl"))
        self.assertNotIn(FAKE_REFRESH_TOKEN, everything)
        self.assertNotIn(own["tokens"]["access_token"], everything)

    async def test_session_refused_when_login_would_expire(self):
        make_codex_login(self.b.cfg.codex_login_path, expires_in=600)  # 10 min left, run may take 5 min + margin
        st = await self.b.tool("spawn", profile="codexplan", wait=True)
        self.assertEqual(st["state"], "failed", st)
        self.assertIn("hb codex-login", st["end_reason"])

    async def test_session_refused_without_login(self):
        (self.b.cfg.codex_login_path / "auth.json").unlink()
        st = await self.b.tool("spawn", profile="codexplan", wait=True)
        self.assertEqual(st["state"], "failed", st)
        self.assertIn("no Codex login", st["end_reason"])


class PolicyTests(BrokerCase):
    async def register(self, name="ext"):
        code, body = await self.b.post("/api/register", {"name": name, "harness": "codex"})
        self.assertEqual(code, 200, body)
        return body

    async def test_spawn_policy_and_depth(self):
        ext = await self.register()
        # external may spawn echo, not locked
        st = await self.b.tool("spawn", ext["token"], profile="echo", wait=True)
        self.assertEqual(st["state"], "idle")
        code, body = await self.b.post("/api/tools/spawn", {"profile": "locked"}, ext["token"])
        self.assertEqual(code, 403, body)
        # a managed echo session (depth 1, budget 1) may spawn echo once more,
        # but that child (budget 0) may not spawn again
        child_token = self.b.broker.manager.sessions[st["id"]].token
        grand = await self.b.tool("spawn", child_token, profile="echo", wait=True)
        self.assertEqual(grand["depth"], 2)
        grand_token = self.b.broker.manager.sessions[grand["id"]].token
        code, body = await self.b.post("/api/tools/spawn", {"profile": "echo"}, grand_token)
        self.assertEqual(code, 403)
        self.assertIn("no spawn depth left", body["error"])

    async def test_only_spawner_or_operator_terminates(self):
        a = await self.register("a")
        other = await self.register("b")
        st = await self.b.tool("spawn", a["token"], profile="echo", wait=True)
        code, _ = await self.b.post("/api/tools/terminate", {"session": st["id"]}, other["token"])
        self.assertEqual(code, 403)
        done = await self.b.tool("terminate", a["token"], session=st["id"])
        self.assertEqual(done["state"], "terminated")

    async def test_bad_requests(self):
        code, body = await self.b.post("/api/tools/spawn", {"profile": "echo", "bogus": 1})
        self.assertEqual(code, 400)
        code, _ = await self.b.post("/api/tools/spawn", {"profile": "nope"})
        self.assertEqual(code, 404)
        code, _ = await self.b.post("/api/tools/list_profiles", {}, token="wrong")
        self.assertEqual(code, 403)
        code, _ = await self.b.post("/api/register", {"name": "x"}, token="wrong")
        self.assertEqual(code, 403)


class BridgeTests(BrokerCase):
    async def test_official_mcp_client_through_bridge(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(BRIDGE), "--name", "codex-ext", "--harness", "codex"],
            env=self.b.bridge_env(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = [t.name for t in (await client.list_tools()).tools]
                self.assertEqual(
                    tools, ["send", "receive", "list_profiles", "spawn", "clone", "list_sessions", "status", "terminate",
                            "history"]
                )
                res = await client.call_tool("list_profiles", {})
                profiles = json.loads(res.content[0].text)["profiles"]
                self.assertEqual(
                    sorted(p["id"] for p in profiles), ["codexplan", "echo", "locked", "modlocal", "modrepo", "noload", "notools"]
                )
                # annotations let harnesses call the messaging tools without asking
                listed = {t.name: t.annotations for t in (await client.list_tools()).tools}
                self.assertEqual((listed["send"].destructive_hint, listed["send"].open_world_hint), (False, False))
                self.assertTrue(listed["terminate"].destructive_hint)
                self.assertTrue(listed["history"].read_only_hint)

                res = await client.call_tool("spawn", {"profile": "echo", "task": "via bridge"})
                self.assertFalse(res.is_error, res.content)
                st = json.loads(res.content[0].text)
                self.assertEqual(st["state"], "idle")

                res = await client.call_tool("receive", {"from": st["id"], "reply_to": st["task_message_id"], "wait": 20})
                got = json.loads(res.content[0].text)["messages"]
                self.assertEqual([m["body"] for m in got], ["echo: via bridge"])
                self.assertEqual(len(res.content), 1)  # receive never carries a notice

                # other tool results say what is waiting, without consuming it
                await client.call_tool("send", {"to": st["id"], "body": "piggy"})
                await asyncio.sleep(1.0)
                listed = await client.call_tool("list_sessions", {})
                self.assertEqual(len(listed.content), 2, listed.content)
                self.assertTrue(listed.content[1].text.startswith("You have 1 unread message: 1 from echo"))
                res = await client.call_tool("receive", {})
                self.assertEqual([m["body"] for m in json.loads(res.content[0].text)["messages"]], ["echo: piggy"])

                res = await client.call_tool("status", {"session": "does-not-exist"})
                self.assertTrue(res.is_error)

        ext = [s for s in self.b.broker.manager.sessions.values() if s.name == "codex-ext"][0]
        for _ in range(50):  # the bridge unregisters on exit
            if ext.state == "terminated":
                break
            await asyncio.sleep(0.1)
        self.assertEqual(ext.state, "terminated")

    async def test_tool_list_follows_hb_tools(self):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(BRIDGE), "--name", "limited",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self.b.bridge_env(HB_TOOLS="send,receive"),
        )
        try:
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n").encode())
            await proc.stdin.drain()
            listed = json.loads(await asyncio.wait_for(proc.stdout.readline(), 30))
            self.assertEqual([t["name"] for t in listed["result"]["tools"]], ["send", "receive"])
        finally:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 15)

    async def test_channel_push(self):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(BRIDGE), "--channel", "--name", "claude-ext", "--harness", "claude-code",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self.b.bridge_env(),
        )

        async def send(frame):
            proc.stdin.write((json.dumps(frame) + "\n").encode())
            await proc.stdin.drain()

        async def read():
            return json.loads(await asyncio.wait_for(proc.stdout.readline(), 30))

        try:
            await send({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}})
            self.assertEqual((await read())["error"]["code"], -32601)  # modern clients fall back
            await send({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
            init = (await read())["result"]
            self.assertEqual(init["protocolVersion"], "2025-06-18")
            self.assertIn("claude/channel", init["capabilities"]["experimental"])
            await send({"jsonrpc": "2.0", "method": "notifications/initialized"})

            for _ in range(50):
                names = [s["name"] for s in (await self.b.tool("list_sessions"))["sessions"]]
                if "claude-ext" in names:
                    break
                await asyncio.sleep(0.1)
            sent = await self.b.tool("send", to="claude-ext", body="ping from operator")
            note = await read()
            self.assertEqual(note["method"], "notifications/claude/channel")
            self.assertEqual(note["params"]["content"], "ping from operator")
            self.assertEqual(note["params"]["meta"]["from_id"], "operator@pc1")
            self.assertEqual(note["params"]["meta"]["message_id"], sent["message_id"])
        finally:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 15)


if __name__ == "__main__":
    unittest.main()

"""End-to-end: broker over HTTP, local runner, fake ACP agent, and the stdio
bridge driven by the official MCP SDK client."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import BRIDGE, RunningBroker, make_repo

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

    async def reply_to(self, message_id, token=None, wait=20):
        """receive the reply to `message_id` (the only primitive needed)."""
        res = await self.b.tool("receive", token, reply_to=message_id, wait=wait)
        if not res["messages"]:
            self.fail(f"no reply to {message_id}")
        self.assertEqual(len(res["messages"]), 1, res)
        return res["messages"][0]

    async def ask(self, to, body, token=None, wait=30):
        sent = await self.b.tool("send", token, to=to, body=body)
        return sent, await self.reply_to(sent["message_id"], token, wait)

    def first_message_id(self, session_id):
        ev = self.b.broker.store.events(session_id="operator", kinds=["message.send"])
        return [e for e in ev if e["payload"]["to"] == session_id][0]["message_id"]


class LifecycleTests(BrokerCase):
    async def test_spawn_task_reply_history_terminate(self):
        st = await self.b.tool("spawn", profile="echo", task="hello there", wait=True)
        self.assertEqual(st["state"], "idle", st)
        sid = st["id"]
        self.assertEqual(st["task_message_id"], self.first_message_id(sid))
        reply = await self.reply_to(st["task_message_id"])
        self.assertEqual(reply["body"], "echo: hello there")
        self.assertEqual(reply["from"], sid)
        self.assertEqual(reply["origin"], "final")

        # history: raw body, exact injected text, ACP frames, turn result
        hist = (await self.b.tool("history", thread_id=reply["thread_id"]))["events"]
        kinds = [e["kind"] for e in hist]
        for k in ("message.send", "message.inject", "turn.end", "message.deliver"):
            self.assertIn(k, kinds)
        turn = next(e for e in hist if e["kind"] == "turn.end")
        self.assertEqual(turn["payload"]["outcome"], "final text sent as the reply")
        vias = [e["payload"]["via"] for e in hist if e["kind"] == "message.deliver"]
        self.assertEqual(vias, ["turn", "receive"])
        inject = next(e for e in hist if e["kind"] == "message.inject")
        self.assertIn("hello there", inject["payload"]["text"])
        self.assertIn("[harness-broker] message from operator", inject["payload"]["text"])
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
        sent = await self.b.tool("send", to=st["id"], body="ENV")
        env = json.loads((await self.reply_to(sent["message_id"]))["body"])
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
        sent = await self.b.tool("send", to=st["id"], body="MCP")
        servers = json.loads((await self.reply_to(sent["message_id"]))["body"])
        self.assertEqual(servers[0]["name"], "hb")
        env = {e["name"]: e["value"] for e in servers[0]["env"]}
        self.assertEqual(env["HB_TOKEN"], self.b.broker.manager.sessions[st["id"]].token)
        self.assertEqual(set(env), {"HB_BROKER_URL", "HB_TOKEN"})

    async def test_managed_session_talks_through_bridge(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        _, r = await self.ask(st["id"], 'BRIDGE status {"session": "self"}')
        result = json.loads(r["body"])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(result["content"][0]["text"])["id"], st["id"])
        self.assertEqual(len(result["content"]), 1)  # nothing unread, so no notice

        # a plain send from inside is an ordinary message; the final text still answers
        out, final = await self.ask(st["id"], 'BRIDGE send {"to": "operator", "body": "hi from inside"}')
        self.assertEqual(final["origin"], "final")
        got = (await self.b.tool("receive", **{"from": st["id"]}))["messages"]
        self.assertEqual([m["body"] for m in got], ["hi from inside"])
        self.assertIsNone(got[0]["reply_to"])

    async def test_notice_lists_unread_mid_turn(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        first = await self.b.tool("send", to=st["id"], body='DELAY 0.5 BRIDGE status {"session": "self"}')
        await asyncio.sleep(0.1)
        second = await self.b.tool("send", to=st["id"], body="waiting in the mailbox")
        self.assertEqual(second["to_state"], "busy")
        result = json.loads((await self.reply_to(first["message_id"]))["body"])
        self.assertEqual(len(result["content"]), 2, result)
        notice = result["content"][1]["text"]
        self.assertTrue(notice.startswith("You have 1 unread message: 1 from operator"), notice)
        # the unread message was not consumed by the notice: it starts the next turn
        self.assertEqual((await self.reply_to(second["message_id"]))["body"], "echo: waiting in the mailbox")

    async def test_receive_mid_turn(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        first = await self.b.tool(
            "send", to=st["id"], body='BRIDGE receive {"from": "operator", "wait": 20}'
        )
        await asyncio.sleep(0.5)
        later = await self.b.tool("send", to=st["id"], body="for your receive")
        result = json.loads((await self.reply_to(first["message_id"]))["body"])
        got = json.loads(result["content"][0]["text"])["messages"]
        self.assertEqual([m["message_id"] for m in got], [later["message_id"]])
        deliver = (await self.b.tool("history", session=st["id"], kinds=["message.deliver"]))["events"]
        self.assertEqual({e["message_id"]: e["payload"]["via"] for e in deliver}[later["message_id"]], "receive")
        # taken mid-turn, so it does not start a turn of its own
        self.assertEqual((await self.b.tool("receive", reply_to=later["message_id"], wait=1))["messages"], [])
    async def test_history_access_is_limited_for_sessions(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        own = await self.b.tool("send", to=st["id"], body='BRIDGE history {"session": "self", "kinds": ["session.spawn"]}')
        result = json.loads((await self.reply_to(own["message_id"]))["body"])
        self.assertFalse(result["isError"], result)
        self.assertEqual(len(json.loads(result["content"][0]["text"])["events"]), 1)
        other = await self.b.tool("send", to=st["id"], body='BRIDGE history {"session": "operator"}')
        result = json.loads((await self.reply_to(other["message_id"]))["body"])
        self.assertTrue(result["isError"])
        self.assertIn("403", result["content"][0]["text"])

    async def test_sender_is_told_when_no_answer_comes(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        first = await self.b.tool("send", to=st["id"], body="CRASH")
        notice = await self.reply_to(first["message_id"])
        self.assertIn("ended before answering", notice["body"])
        self.assertEqual(notice["from"], st["id"])
        self.assertEqual(notice["origin"], "notice")

    async def test_unread_messages_dropped_at_end_notify_senders(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        busy = await self.b.tool("send", to=st["id"], body="SLEEP 5")
        await asyncio.sleep(0.2)
        waiting = await self.b.tool("send", to=st["id"], body="never read")
        await self.b.tool("terminate", session=st["id"], reason="stop now")
        for sent in (busy, waiting):
            n = await self.reply_to(sent["message_id"])
            self.assertEqual(n["origin"], "notice")
            self.assertIn("stop now", n["body"])

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
        ok = await self.b.tool("send", to=st["id"], body="PERMISSION edit")
        no = await self.b.tool("send", to=st["id"], body="PERMISSION execute")
        self.assertEqual(json.loads((await self.reply_to(ok["message_id"]))["body"])["outcome"]["optionId"], "allow-once")
        self.assertEqual(json.loads((await self.reply_to(no["message_id"]))["body"])["outcome"]["optionId"], "reject-once")
        perms = (await self.b.tool("history", session=st["id"], kinds=["acp.permission"]))["events"]
        self.assertEqual(len(perms), 2)

    async def test_crash_marks_failed(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        await self.b.tool("send", to=st["id"], body="CRASH")
        for _ in range(100):
            s = await self.b.tool("status", session=st["id"])
            if s["state"] == "failed":
                break
            await asyncio.sleep(0.1)
        self.assertEqual(s["state"], "failed")
        self.assertTrue("exited" in s["end_reason"] or "prompt failed" in s["end_reason"], s["end_reason"])

    async def test_idempotent_send(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        a = await self.b.tool("send", to=st["id"], body="once", idempotency_key="k1")
        b = await self.b.tool("send", to=st["id"], body="once", idempotency_key="k1")
        self.assertEqual(a["message_id"], b["message_id"])
        self.assertTrue(b.get("duplicate"))


class BoundaryRuleTests(BrokerCase):
    """The one translation rule for final responses."""

    async def outcome(self, sid, message_id):
        ev = (await self.b.tool("history", session=sid, kinds=["turn.end"]))["events"]
        return next(e["payload"]["outcome"] for e in ev if e["message_id"] == message_id)

    async def test_reply_during_turn_replaces_final_text(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        sent = await self.b.tool(
            "send", to=st["id"], body='BRIDGE send {"to": "operator", "body": "early answer", "reply_to": "$MSG"}'
        )
        r = await self.reply_to(sent["message_id"])
        self.assertEqual((r["body"], r["origin"]), ("early answer", "send"))
        await asyncio.sleep(0.3)
        self.assertEqual(await self.outcome(st["id"], sent["message_id"]), "replied during the turn: final text recorded only")
        # the final text (the tool result) was recorded, not delivered
        self.assertEqual((await self.b.tool("receive", wait=0.5))["messages"], [])

    async def test_empty_final_text_notifies_sender(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        sent, r = await self.ask(st["id"], "SILENT")
        self.assertEqual(r["origin"], "notice")
        self.assertIn("ended its turn without replying", r["body"])

    async def test_turn_started_by_a_reply_sends_nothing(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        q, a = await self.ask(st["id"], "question")
        # answering the answer starts a turn whose final text is not sent back
        back = await self.b.tool("send", to=st["id"], body="thanks", reply_to=a["message_id"])
        self.assertEqual(back["thread_id"], q["thread_id"])
        self.assertEqual((await self.b.tool("receive", wait=1.5))["messages"], [])
        self.assertEqual(await self.outcome(st["id"], back["message_id"]), "turn started by a reply: final text recorded only")
        inject = (await self.b.tool("history", session=st["id"], kinds=["message.inject"]))["events"][-1]
        self.assertIn("This is a reply", inject["payload"]["text"])

    async def test_final_text_is_what_follows_the_last_tool_call(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        _, r = await self.ask(st["id"], "TOOLCALL")
        self.assertEqual(r["body"], "the answer is 42")


class ModuleProfileTests(BrokerCase):
    async def test_local_module_body_format_per_message(self):
        st = await self.b.tool("spawn", profile="modlocal", wait=True)
        self.assertEqual(st["state"], "idle", st)
        sid = st["id"]

        # body format: the prompt is exactly the message body
        sent, r = await self.ask(sid, "RAW")
        self.assertEqual(json.loads(r["body"]), {"prompt": "RAW"})
        self.assertEqual(r["reply_to"], sent["message_id"])

        # the module's instructions and skills, and the denied tools, reach the harness
        _, r = await self.ask(sid, "INSTRUCTIONS")
        found = json.loads(r["body"])
        self.assertEqual(found["CLAUDE.md"], "You are the test module.\n")
        self.assertEqual(found["skills"], ["mod-skill"])
        self.assertEqual(found["settings"]["permissions"]["deny"], ["Bash", "WebSearch"])

        # per_message: every message gets a fresh conversation; the old one is closed
        _, r = await self.ask(sid, "SESSION")
        info = json.loads(r["body"])
        self.assertEqual(info["prompts_in_session"], 1)
        self.assertEqual(info["session"], "fake-session-3")  # 3rd message, 3rd conversation
        self.assertEqual(info["closed"], ["fake-session-1", "fake-session-2"])

        # brokerTools: false -> no MCP servers at all
        _, r = await self.ask(sid, "MCP")
        self.assertEqual(json.loads(r["body"]), [])

        # every reply was taken exactly once
        self.assertEqual((await self.b.tool("receive"))["messages"], [])

        # module source and file hashes are in the history
        mod = (await self.b.tool("history", session=sid, kinds=["session.module"]))["events"][0]["payload"]
        self.assertEqual(mod["spec"]["id"], "mod")
        self.assertIn("AGENTS.md", mod["files"])

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
        quick_target = await self.b.tool("spawn", profile="echo", name="other", wait=True)
        quick = await self.b.tool("send", to=quick_target["id"], body="fast")
        r = await self.b.tool("receive", reply_to=slow["message_id"], wait=0.3)
        self.assertEqual(r["messages"], [])  # timed out; the fast reply is not taken by this filter
        late = await self.reply_to(slow["message_id"])
        self.assertEqual(late["body"], "echo: SLEEP 1.5")
        got = (await self.b.tool("receive", **{"from": "other"}))["messages"]
        self.assertEqual([m["reply_to"] for m in got], [quick["message_id"]])


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
                tools = {t.name for t in (await client.list_tools()).tools}
                self.assertEqual(
                    tools,
                    {"list_profiles", "spawn", "send", "receive", "list_sessions", "status", "terminate", "history"},
                )
                res = await client.call_tool("list_profiles", {})
                profiles = json.loads(res.content[0].text)["profiles"]
                self.assertEqual(sorted(p["id"] for p in profiles), ["echo", "locked", "modlocal", "modrepo"])

                res = await client.call_tool("spawn", {"profile": "echo", "task": "via bridge"})
                self.assertFalse(res.is_error, res.content)
                st = json.loads(res.content[0].text)
                self.assertEqual(st["state"], "idle")

                res = await client.call_tool("receive", {"reply_to": st["task_message_id"], "wait": 20})
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
            self.assertEqual(note["params"]["meta"]["from_id"], "operator")
            self.assertEqual(note["params"]["meta"]["message_id"], sent["message_id"])
        finally:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 15)


if __name__ == "__main__":
    unittest.main()

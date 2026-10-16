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
        """Wait for the automatic reply to `message_id` in an inbox. Messages
        read on the way are kept, since inbox marks them read."""
        seen = self.__dict__.setdefault("_seen", {}).setdefault(token, {})
        deadline = asyncio.get_running_loop().time() + wait
        while message_id not in seen and asyncio.get_running_loop().time() < deadline:
            res = await self.b.tool("inbox", token, wait=5)
            for m in res["messages"]:
                seen[m["reply_to"]] = m
        if message_id not in seen:
            self.fail(f"no reply to {message_id}")
        return seen.pop(message_id)

    def first_message_id(self, session_id):
        ev = self.b.broker.store.events(session_id="operator", kinds=["message.send"])
        return [e for e in ev if e["payload"]["to"] == session_id][0]["message_id"]


class LifecycleTests(BrokerCase):
    async def test_spawn_task_reply_history_terminate(self):
        st = await self.b.tool("spawn", profile="echo", task="hello there", wait=True)
        self.assertEqual(st["state"], "idle", st)
        sid = st["id"]
        reply = await self.reply_to(self.first_message_id(sid))
        self.assertEqual(reply["body"], "echo: hello there")
        self.assertEqual(reply["from"], sid)

        # history: raw body, exact injected text, ACP frames, turn result
        hist = (await self.b.tool("history", thread_id=reply["thread_id"]))["events"]
        kinds = [e["kind"] for e in hist]
        for k in ("message.send", "message.inject", "turn.end", "message.deliver"):
            self.assertIn(k, kinds)
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
        self.assertEqual(env["HB_PIGGYBACK"], "0")

    async def test_managed_session_talks_through_bridge(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        who = await self.b.tool("send", to=st["id"], body='BRIDGE status {"session": "self"}')
        result = json.loads((await self.reply_to(who["message_id"]))["body"])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(result["content"][0]["text"])["id"], st["id"])
        # managed sessions get messages as prompts, so nothing is piggybacked
        self.assertEqual(len(result["content"]), 1)

        out = await self.b.tool(
            "send", to=st["id"],
            body='BRIDGE send {"to": "operator", "body": "hi from inside", "auto_reply": false}',
        )
        await self.reply_to(out["message_id"])
        inbox = self._seen.get(None, {})
        if None not in inbox:
            msgs = (await self.b.tool("inbox", wait=10))["messages"]
            inbox = {m["reply_to"]: m for m in msgs}
        self.assertEqual(inbox[None]["body"], "hi from inside")
        self.assertEqual(inbox[None]["from"], st["id"])

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

    async def test_messages_queue_while_busy(self):
        st = await self.b.tool("spawn", profile="echo", wait=True)
        first = await self.b.tool("send", to=st["id"], body="SLEEP 1")
        await asyncio.sleep(0.2)
        second = await self.b.tool("send", to=st["id"], body="second")
        self.assertEqual(second["queued_ahead"], 1)
        r1 = await self.reply_to(first["message_id"])
        r2 = await self.reply_to(second["message_id"])
        self.assertEqual(r1["body"], "echo: SLEEP 1")
        self.assertEqual(r2["body"], "echo: second")
        self.assertLess(r1["sent_at"], r2["sent_at"])

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
                    {"list_profiles", "spawn", "send", "inbox", "list_sessions", "status", "terminate", "history"},
                )
                res = await client.call_tool("list_profiles", {})
                profiles = json.loads(res.content[0].text)["profiles"]
                self.assertEqual(sorted(p["id"] for p in profiles), ["echo", "locked"])

                res = await client.call_tool("spawn", {"profile": "echo", "task": "via bridge"})
                self.assertFalse(res.is_error, res.content)
                st = json.loads(res.content[0].text)
                self.assertEqual(st["state"], "idle")

                # The reply may already ride along with the spawn result
                # (unread messages are appended to every tool result);
                # otherwise inbox waits for it.
                bodies = [json.loads(c.text.split("\n", 1)[1])[0]["body"] for c in res.content[1:]]
                if not bodies:
                    res = await client.call_tool("inbox", {"wait": 20})
                    bodies = [m["body"] for m in json.loads(res.content[0].text)["messages"]]
                self.assertEqual(bodies, ["echo: via bridge"])

                # unread messages ride along with the next tool call
                sent = await client.call_tool("send", {"to": st["id"], "body": "piggy"})
                await asyncio.sleep(1.0)
                listed = await client.call_tool("list_sessions", {})
                extra = [c.text for c in [*sent.content[1:], *listed.content[1:]]]
                self.assertEqual(len(extra), 1, extra)
                self.assertTrue(extra[0].startswith("Unread messages for you"))
                self.assertIn("echo: piggy", extra[0])

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
            self.assertEqual(sent["delivery"], "channel")
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

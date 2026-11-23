"""End-to-end: the place over HTTP, local runner, fake ACP agent, and the
place's MCP endpoint driven by the official MCP SDK client.

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
    FAKE_REFRESH_TOKEN,
    NS,
    RunningBroker,
    git,
    make_codex_login,
    make_repo,
)

from agora.spec import CLAUDE_BUILTINS  # noqa: E402

SECRET = "agora-secret-value-123456"


class BrokerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo, self.commit_a, self.commit_b = make_repo(self.tmp)
        os.environ["AGORA_TEST_SECRET"] = SECRET
        os.environ["AGORA_TEST_HOST_ONLY"] = "should-not-leak"
        self.b = RunningBroker(self.tmp, self.repo, self.commit_a)
        await self.b.start()

    async def asyncTearDown(self):
        await self.b.stop()
        os.environ.pop("AGORA_TEST_SECRET", None)
        os.environ.pop("AGORA_TEST_HOST_ONLY", None)
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
        ev = self.b.events(session="operator", kinds=["message.send"])
        return [e for e in ev if e["payload"]["to"] == self.b.address(session_id)][0]["message_id"]

    async def turn_text(self, sid, message_id):
        for _ in range(100):
            ev = [e for e in self.b.events(session=sid, kinds=["turn.end"]) if e["message_id"] == message_id]
            if ev:
                return ev[0]["payload"]["text"]
            await asyncio.sleep(0.1)
        raise AssertionError(f"no turn.end for {message_id}")


class LifecycleTests(BrokerCase):
    async def test_spawn_task_reply_history_terminate(self):
        st = await self.b.tool("spawn", module="echo", task="hello there", wait=True)
        self.assertEqual(st["state"], "idle", st)
        sid = st["id"]
        self.assertEqual(st["task_message_id"], self.first_message_id(sid))
        reply = await self.reply_to(st["task_message_id"], sender=sid)
        self.assertEqual(reply["body"], "echo: hello there")
        self.assertEqual(reply["from"], st["address"])
        self.assertEqual(st["address"], f"{sid}@pc1")

        # history: raw bodies, the exact injected text, the turn's text, ACP frames
        for _ in range(50):  # the turn ends just after the reply is sent
            hist = self.b.events(thread_id=reply["thread_id"])
            if "turn.end" in [e["kind"] for e in hist]:
                break
            await asyncio.sleep(0.1)
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
        self.assertEqual(self.b.events(session=sid, kinds=["acp.out", "acp.in"]), [])  # the harness's own report is not kept
        spawn_ev = self.b.events(session=sid, kinds=["session.spawn"])[0]
        self.assertEqual(spawn_ev["payload"]["spec"]["module"], "echo")
        self.assertTrue(spawn_ev["payload"]["fingerprint"].startswith("sha256:"))

        # tokens never leave agora in its history
        everything = json.dumps([dict(sp.attributes or {}) for sp in self.b.spans.get_finished_spans()])
        session = self.b.broker.manager.sessions[sid]
        self.assertNotIn(session.token, everything)

        done = await self.b.tool("terminate", session=sid, reason="test over")
        self.assertEqual(done["state"], "terminated")
        self.assertFalse((self.b.cfg.history_dir / "sessions" / sid / "harness").exists())  # no records of its own
        self.assertFalse((self.b.cfg.state_dir / "bundles" / sid).exists(), "bundle not removed")
        listed = (await self.b.tool("list_sessions"))["sessions"]
        self.assertNotIn(sid, [s["id"] for s in listed])

    async def test_isolation_of_environment(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
        _, r = await self.ask(st["id"], "ENV")
        env = json.loads(r["body"])
        self.assertTrue(env["home"].startswith(str(self.b.cfg.state_dir / "bundles")), env["home"])
        self.assertEqual(env["files"], ["README.md", "docs", "src", "src/app.py"])  # stripped, pinned to commit a
        self.assertIn("CLAUDE.md", env["config_files"])  # the module's instructions
        self.assertIn("skills/echo-skill/SKILL.md", env["config_files"])
        self.assertNotIn("skills/evil/SKILL.md", env["config_files"])
        self.assertEqual(env["model"], "test-model-1")
        self.assertEqual(env["secret"], SECRET)  # declared secret passed through
        self.assertIsNone(env["leaked_host_var"])  # nothing else from the host env

    async def test_agora_server_given_to_agent(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
        _, r = await self.ask(st["id"], "MCP")
        servers = json.loads(r["body"])
        self.assertEqual((servers[0]["name"], servers[0]["type"]), ("agora", "http"))
        self.assertEqual(servers[0]["url"], f"{self.b.broker.manager.runner.broker_url}/mcp")
        token = self.b.broker.manager.sessions[st["id"]].token
        self.assertEqual(servers[0]["headers"], [{"name": "Authorization", "value": f"Bearer {token}"}])

    async def test_final_text_reaches_no_one(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
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
        st = await self.b.tool("spawn", module="echo", wait=True)
        await self.b.tool("send", to=st["id"], body="CRASH")
        t0 = time.monotonic()
        r = await self.b.tool("receive", **{"from": st["id"], "wait": 20})
        self.assertEqual((r["messages"], r["from_quiet"], r["from_state"]), ([], True, "failed"))
        self.assertLess(time.monotonic() - t0, 10)
        s = await self.b.tool("status", session=st["id"])
        self.assertTrue("exited" in s["end_reason"] or "prompt failed" in s["end_reason"], s["end_reason"])

    async def test_waiting_is_not_cut_short_while_work_is_pending(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
        sent = await self.b.tool("send", to=st["id"], body="SLEEP 1")
        r = await self.b.tool("receive", **{"from": st["id"], "wait": 20})  # busy: wait for the reply
        self.assertEqual([m["reply_to"] for m in r["messages"]], [sent["message_id"]])

    async def test_managed_session_talks_through_bridge(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
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
        st = await self.b.tool("spawn", module="echo", wait=True)
        await self.ask(st["id"], 'BRIDGE send {"to": "operator", "body": "a question for you"}')
        question = (await self.b.tool("receive", **{"from": st["id"]}))["messages"][0]
        answer = await self.b.tool("send", reply_to=question["message_id"], body="my answer")
        self.assertEqual((answer["to"], answer["thread_id"]), (st["address"], question["thread_id"]))
        echo = await self.reply_to(answer["message_id"], sender=st["id"])
        self.assertEqual(echo["body"], "echo: my answer")
        inject = self.b.events(session=st["id"], kinds=["message.inject"])[-1]
        self.assertIn(f"in_reply_to: {question['message_id']}", inject["payload"]["text"])
        # a reply needs a message that was addressed to you
        code, body = await self.b.call("send", {"reply_to": answer["message_id"], "body": "x"})
        self.assertEqual(code, 400, body)
        code, body = await self.b.call("send", {"body": "nowhere"})
        self.assertEqual(code, 400, body)

    async def test_notice_lists_unread_mid_turn(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
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
        st = await self.b.tool("spawn", module="echo", wait=True)
        first = await self.b.tool("send", to=st["id"], body='BRIDGE receive {"from": "operator", "wait": 20}')
        await asyncio.sleep(0.5)
        later = await self.b.tool("send", to=st["id"], body="for your receive")
        result = json.loads((await self.reply_to(first["message_id"], sender=st["id"]))["body"])
        got = json.loads(result["content"][0]["text"])["messages"]
        self.assertEqual([m["message_id"] for m in got], [later["message_id"]])
        deliver = self.b.events(session=st["id"], kinds=["message.deliver"])
        self.assertEqual({e["message_id"]: e["payload"]["via"] for e in deliver}[later["message_id"]], "receive")
        # taken mid-turn, so it started no turn of its own and nothing answers it
        r = await self.b.tool("receive", **{"from": st["id"], "reply_to": later["message_id"], "wait": 10})
        self.assertEqual((r["messages"], r["from_quiet"]), ([], True))

    async def test_unread_messages_dropped_at_end(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
        await self.b.tool("send", to=st["id"], body="SLEEP 5")
        await asyncio.sleep(0.2)
        waiting = await self.b.tool("send", to=st["id"], body="never read")
        await self.b.tool("terminate", session=st["id"], reason="stop now")
        dropped = self.b.events(session=st["id"], kinds=["message.dropped"])
        self.assertEqual([e["message_id"] for e in dropped], [waiting["message_id"]])
        self.assertEqual((await self.b.tool("receive"))["messages"], [])  # the broker sends nobody anything
        code, _ = await self.b.call("send", {"to": st["id"], "body": "too late"})
        self.assertEqual(code, 404)

    async def test_messages_queue_while_busy(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
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

    async def test_permissions_follow_module(self):
        # permissions are always granted...
        st = await self.b.tool("spawn", module="echo", wait=True)
        _, ok = await self.ask(st["id"], "PERMISSION edit")
        _, ok2 = await self.ask(st["id"], "PERMISSION execute")
        self.assertEqual(json.loads(ok["body"])["outcome"]["optionId"], "allow-once")
        self.assertEqual(json.loads(ok2["body"])["outcome"]["optionId"], "allow-once")
        perms = self.b.events(session=st["id"], kinds=["acp.permission"])
        self.assertEqual(len(perms), 2)
        # ...unless the module turned the harness's built-in tools off
        st = await self.b.tool("spawn", module="mod", wait=True)
        _, no = await self.ask(st["id"], "PERMISSION execute")
        self.assertEqual(json.loads(no["body"])["outcome"]["optionId"], "reject-once")

    async def test_send_has_no_idempotency_key(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
        code, body = await self.b.call("send", {"to": st["id"], "body": "x", "idempotency_key": "k1"})
        self.assertEqual(code, 400, body)


class ModuleRunTests(BrokerCase):
    async def test_local_module_per_message_with_send_only(self):
        st = await self.b.tool("spawn", module="mod", wait=True)
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
        self.assertEqual(found["settings"]["permissions"]["deny"], list(CLAUDE_BUILTINS))  # builtinTools: false
        self.assertEqual(found["settings"]["permissions"]["allow"], ["mcp__agora__send"])  # never blocked by "deny"

        # per_message: every message gets a fresh conversation; the old one is closed
        _, r = await self.ask(sid, "SESSION")
        info = json.loads(r["body"])
        self.assertEqual(info["prompts_in_session"], 1)
        self.assertEqual(info["session"], "fake-session-3")  # 3rd message, 3rd conversation
        self.assertEqual(info["closed"], ["fake-session-1", "fake-session-2"])

        # brokerTools: [send] -> the place lists and allows send only
        _, r = await self.ask(sid, "MCP")
        servers = json.loads(r["body"])
        self.assertEqual(servers[0]["name"], "agora")
        token = self.b.broker.manager.sessions[sid].token
        _, listed, _ = await asyncio.to_thread(
            self.b._mcp_sync, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token, 30)
        self.assertEqual([t["name"] for t in listed["result"]["tools"]], ["send"])
        # the module's own MCP server, pointing at the module copied into the session
        tool = servers[1]
        self.assertEqual(tool["name"], "tool")
        root = tool["args"][1]
        self.assertEqual(tool["command"], f"{root}/bin/tool")
        _, r = await self.ask(sid, f"READ {root}/bin/tool")
        self.assertEqual(json.loads(r["body"]), "#!/bin/sh\n")
        _, r = await self.ask(sid, 'BRIDGE spawn {"module": "echo"}')
        res = json.loads(r["body"])
        self.assertTrue(res["isError"])
        self.assertIn("not among this session's tools", res["content"][0]["text"])
        code, body = await self.b.call("spawn", {"module": "echo"}, token)
        self.assertEqual(code, 403, body)
        self.assertIn("not among this session's tools", body["error"])

        # module source and file hashes are in the history
        mod = self.b.events(session=sid, kinds=["session.module"])[0]["payload"]
        self.assertEqual(mod["plugin"]["name"], "mod")
        self.assertIn("io.github.hoho-hoi.agora/AGENTS.md", mod["files"])

    async def test_module_model_is_enforced(self):
        # the harness offers the module's model: agora switches to it
        st = await self.b.tool("spawn", module="pickmodel", wait=True)
        self.assertEqual(st["state"], "idle", st)
        _, r = await self.ask(st["id"], "MODEL")
        self.assertEqual(json.loads(r["body"]), "test-model-1")
        ev = self.b.events(session=st["id"], kinds=["session.model"])[0]["payload"]
        self.assertEqual((ev["reported"], ev["selected"]), ("other-model", "test-model-1"))
        # it does not: the session fails instead of running on another model
        st = await self.b.tool("spawn", module="nomodel", wait=True)
        self.assertEqual(st["state"], "failed", st)
        ended = self.b.events(session=st["id"], kinds=["session.end"])[0]["payload"]
        self.assertIn("test-model-1", ended["reason"])

    async def test_no_broker_tools_means_no_bridge(self):
        st = await self.b.tool("spawn", module="notools", wait=True)
        sent = await self.b.tool("send", to=st["id"], body="MCP")
        r = await self.b.tool("receive", **{"from": st["id"], "wait": 20})
        self.assertEqual((r["messages"], r["from_quiet"]), ([], True))  # it cannot answer at all
        # no bridge: only the module's own MCP server
        servers = json.loads(await self.turn_text(st["id"], sent["message_id"]))
        self.assertEqual([m["name"] for m in servers], ["tool"])

    async def test_repo_module_pinned_by_commit(self):
        # list_modules fetches pinned modules, so callers can choose by description
        mods = {m["id"]: m for m in (await self.b.tool("list_modules"))["modules"]}
        self.assertEqual(mods["mod"]["description"], "a module for tests")
        self.assertEqual(mods["peer"]["description"], "peer module")
        st = await self.b.tool("spawn", module="peer", wait=True)
        self.assertEqual(st["state"], "idle", st)
        _, r = await self.ask(st["id"], "INSTRUCTIONS")
        self.assertEqual(json.loads(r["body"])["CLAUDE.md"], "You are the peer.\n")
        mod = self.b.events(session=st["id"], kinds=["session.module"])[0]["payload"]
        self.assertEqual(mod["source"]["path"], "peer")
        self.assertEqual(len(mod["source"]["ref"]), 40)

    async def test_spawn_with_a_candidate_module_commit(self):
        # a candidate version of the peer module, committed after the pinned one
        mrepo = self.tmp / "module-repo"
        (mrepo / "peer" / NS / "AGENTS.md").write_text("You are the candidate peer.\n")
        git(mrepo, "commit", "-q", "-am", "candidate")
        candidate = git(mrepo, "rev-parse", "HEAD")
        st = await self.b.tool("spawn", module="peer", module_ref=candidate, wait=True)
        self.assertEqual(st["state"], "idle", st)
        _, r = await self.ask(st["id"], "INSTRUCTIONS")
        self.assertEqual(json.loads(r["body"])["CLAUDE.md"], "You are the candidate peer.\n")
        spawn = self.b.events(session=st["id"], kinds=["session.spawn"])[0]["payload"]
        self.assertEqual((spawn["module_ref_override"], spawn["spec"]["source"]["ref"]), (candidate, candidate))
        mod = self.b.events(session=st["id"], kinds=["session.module"])[0]["payload"]
        self.assertEqual(mod["source"]["ref"], candidate)
        # only a full commit, and only for a module registered from a repository
        code, body = await self.b.call("spawn", {"module": "peer", "module_ref": "main"})
        self.assertEqual(code, 400, body)
        code, body = await self.b.call("spawn", {"module": "mod", "module_ref": candidate})
        self.assertEqual(code, 400, body)

    async def test_receive_wait_is_bounded_and_filters(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
        slow = await self.b.tool("send", to=st["id"], body="SLEEP 1.5")
        other = await self.b.tool("spawn", module="echo", name="other", wait=True)
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
        st = await self.b.tool("spawn", module="codexplan", wait=True)
        self.assertEqual(st["state"], "idle", st)
        _, r = await self.ask(st["id"], "CODEX_AUTH")
        seen = json.loads(r["body"])
        self.assertTrue(seen["auth_file"])
        self.assertTrue(seen["has_access_token"])
        self.assertEqual(seen["refresh_token"], "")  # blanked
        self.assertIn('cli_auth_credentials_store = "file"', seen["config"])
        self.assertIn('web_search = "disabled"', seen["config"])
        self.assertEqual(seen["env"], {"NO_BROWSER": "1", "INITIAL_AGENT_MODE": "read-only"})
        login = self.b.events(session=st["id"], kinds=["session.login"])[0]["payload"]
        self.assertEqual((login["login"], login["plan"]), ("chatgpt", "plus"))
        # the broker's own login is untouched
        own = json.loads((self.b.cfg.codex_login_path / "auth.json").read_text())
        self.assertEqual(own["tokens"]["refresh_token"], FAKE_REFRESH_TOKEN)

        await self.b.tool("terminate", session=st["id"])
        everything = json.dumps(self.b.events())
        self.assertNotIn(FAKE_REFRESH_TOKEN, everything)
        self.assertNotIn(own["tokens"]["access_token"], everything)

    async def test_session_refused_when_login_would_expire(self):
        make_codex_login(self.b.cfg.codex_login_path, expires_in=600)  # 10 min left, run may take 5 min + margin
        st = await self.b.tool("spawn", module="codexplan", wait=True)
        self.assertEqual(st["state"], "failed", st)
        self.assertIn("agora codex-login", st["end_reason"])

    async def test_session_refused_without_login(self):
        (self.b.cfg.codex_login_path / "auth.json").unlink()
        st = await self.b.tool("spawn", module="codexplan", wait=True)
        self.assertEqual(st["state"], "failed", st)
        self.assertIn("no Codex login", st["end_reason"])


class PolicyTests(BrokerCase):
    async def register(self, name="ext"):
        code, body = await self.b.register(name)
        self.assertEqual(code, 200, body)
        return body

    async def test_spawn_policy_and_depth(self):
        ext = await self.register()
        # external may spawn echo, not locked
        st = await self.b.tool("spawn", ext["token"], module="echo", wait=True)
        self.assertEqual(st["state"], "idle")
        code, body = await self.b.call("spawn", {"module": "locked"}, ext["token"])
        self.assertEqual(code, 403, body)
        # a managed echo session (depth 1, budget 1) may spawn echo once more,
        # but that child (budget 0) may not spawn again
        child_token = self.b.broker.manager.sessions[st["id"]].token
        grand = await self.b.tool("spawn", child_token, module="echo", wait=True)
        self.assertEqual(grand["depth"], 2)
        grand_token = self.b.broker.manager.sessions[grand["id"]].token
        code, body = await self.b.call("spawn", {"module": "echo"}, grand_token)
        self.assertEqual(code, 403)
        self.assertIn("no spawn depth left", body["error"])

    async def test_only_spawner_or_operator_terminates(self):
        a = await self.register("a")
        other = await self.register("b")
        st = await self.b.tool("spawn", a["token"], module="echo", wait=True)
        code, _ = await self.b.call("terminate", {"session": st["id"]}, other["token"])
        self.assertEqual(code, 403)
        done = await self.b.tool("terminate", a["token"], session=st["id"])
        self.assertEqual(done["state"], "terminated")

    async def test_bad_requests(self):
        code, body = await self.b.call("spawn", {"module": "echo", "bogus": 1})
        self.assertEqual(code, 400)
        code, _ = await self.b.call("spawn", {"module": "nope"})
        self.assertEqual(code, 404)
        code, _ = await self.b.call("list_modules", {}, token="wrong")
        self.assertEqual(code, 403)
        code, _ = await self.b.register("x", token="wrong")
        self.assertEqual(code, 403)


class McpEndpointTests(BrokerCase):
    async def test_official_mcp_client_joins_with_the_broker_key(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        http = create_mcp_http_client(headers={"Authorization": f"Bearer {self.b.broker.key}"})
        async with http, streamable_http_client(f"{self.b.url}/mcp", http_client=http) as (read, write, *_):
            async with ClientSession(read, write) as client:
                init = await client.initialize()
                self.assertIn("send", init.instructions)
                tools = [t.name for t in (await client.list_tools()).tools]
                self.assertEqual(
                    tools, ["send", "receive", "list_modules", "spawn", "clone", "list_sessions", "status", "terminate",
                            "history"]
                )
                res = await client.call_tool("list_modules", {})
                mods = json.loads(res.content[0].text)["modules"]
                self.assertEqual(
                    sorted(m["id"] for m in mods), ["codexplan", "echo", "locked", "mod", "noload", "nomodel", "notools", "peer", "pickmodel"]
                )
                # annotations let harnesses call the messaging tools without asking
                listed = {t.name: t.annotations for t in (await client.list_tools()).tools}
                self.assertEqual((listed["send"].destructive_hint, listed["send"].open_world_hint), (False, False))
                self.assertTrue(listed["terminate"].destructive_hint)
                self.assertTrue(listed["history"].read_only_hint)

                res = await client.call_tool("spawn", {"module": "echo", "task": "via mcp", "workspace": self.b.default_workspace})
                self.assertFalse(res.is_error, res.content)
                st = json.loads(res.content[0].text)
                self.assertEqual(st["state"], "idle")

                res = await client.call_tool("receive", {"from": st["id"], "reply_to": st["task_message_id"], "wait": 20})
                got = json.loads(res.content[0].text)["messages"]
                self.assertEqual([m["body"] for m in got], ["echo: via mcp"])
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

        ext = [s for s in self.b.broker.manager.sessions.values() if s.kind == "external"][0]
        for _ in range(50):  # closing the MCP session ends the external session
            if ext.state == "terminated":
                break
            await asyncio.sleep(0.1)
        self.assertEqual(ext.state, "terminated")


if __name__ == "__main__":
    unittest.main()


class FakeGateway:
    """Stands in for LiteLLM's /key/generate and /key/delete."""

    def __init__(self):
        import http.server
        import threading

        self.requests: list[tuple[str, dict, str]] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body, self.headers.get("Authorization", "")))
                out = {"key": "sk-virtual-" + body.get("key_alias", "x")} if self.path == "/key/generate" else {}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class GatewayAndTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from agora.telemetry import Telemetry

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        repo, commit, _ = make_repo(self.tmp)
        self.gw = FakeGateway()
        os.environ["TEST_LITELLM_MASTER"] = "master-secret-123456"
        os.environ["AGORA_TEST_SECRET"] = SECRET
        self.spans = InMemorySpanExporter()
        self.b = RunningBroker(self.tmp, repo, commit,
                               llm_gateway={"url": self.gw.url, "session_url": "http://gateway.test:4000",
                                            "master_key_env": "TEST_LITELLM_MASTER"},
                               telemetry=Telemetry(exporter=self.spans, place="pc1"))
        self.b.spans = self.spans
        await self.b.start()

    async def asyncTearDown(self):
        await self.b.stop()
        self.gw.close()
        os.environ.pop("TEST_LITELLM_MASTER", None)
        os.environ.pop("AGORA_TEST_SECRET", None)
        self._tmp.cleanup()

    async def ask(self, sid, body):
        sent = await self.b.tool("send", to=sid, body=body)
        r = await self.b.tool("receive", reply_to=sent["message_id"], wait=20)
        return r["messages"][0]["body"]

    async def test_each_session_gets_its_own_virtual_key(self):
        self.b.cfg.network.proxy = "http://proxy.test:3128"  # the gateway must bypass a place's proxy
        st = await self.b.tool("spawn", module="echo", wait=True)
        env = json.loads(await self.ask(st["id"], "ENV"))
        self.assertEqual(env["anthropic_base_url"], "http://gateway.test:4000")
        self.assertIn("gateway.test", env["no_proxy"])
        self.assertEqual(env["anthropic_api_key"], f"sk-virtual-agora-{st['id']}")
        path, body, auth = self.gw.requests[0]
        self.assertEqual((path, auth), ("/key/generate", "Bearer master-secret-123456"))
        self.assertEqual((body["metadata"]["module"], body["metadata"]["agora_session"]), ("echo", f"{st['id']}@pc1"))
        # the key never reaches the history
        hist = json.dumps(self.b.events(session=st["id"]))
        self.assertNotIn(f"sk-virtual-agora-{st['id']}", hist)
        await self.b.tool("terminate", session=st["id"])
        self.assertIn(("/key/delete", {"keys": [f"sk-virtual-agora-{st['id']}"]}),
                      [(p, b) for p, b, _ in self.gw.requests])

    async def test_each_turn_is_a_trace_of_its_session(self):
        st = await self.b.tool("spawn", module="echo", wait=True)
        r = json.loads(await self.ask(st["id"], 'BRIDGE spawn {"module": "peer", "wait": true}'))
        child = json.loads(r["content"][0]["text"])["id"]
        await self.ask(child, "hello peer")
        await self.b.tool("terminate", session=child)
        await self.b.tool("terminate", session=st["id"])
        done = self.spans.get_finished_spans()
        turns = [s for s in done if s.name == "turn echo"]
        self.assertTrue(turns)
        turn = turns[0]
        # a root of its own, shown as a conversation: what was given and what was written
        self.assertIsNone(turn.parent)
        self.assertEqual(turn.attributes["session.id"], st["id"])
        self.assertEqual(turn.attributes["input.value"].split("\n")[0], "from: operator@pc1")
        self.assertIn("output.value", turn.attributes)
        self.assertEqual(turn.attributes["gen_ai.request.model"], "test-model-1")
        # events during the turn hang under it
        sends = [s for s in done if s.name == "message.send" and s.attributes["session.id"] == st["id"]]
        self.assertTrue(sends and all(s.parent is not None for s in sends))
        # the spawned session's turns are their own traces, linked by agora.parent
        kid = [s for s in done if s.name == "turn peer"][0]
        self.assertIsNone(kid.parent)
        self.assertEqual((kid.attributes["session.id"], kid.attributes["agora.parent"]), (child, f"{st['id']}@pc1"))
        # events outside a turn are roots that say what they are
        ends = [s for s in done if s.name == "session.end" and s.attributes["session.id"] == child]
        self.assertTrue(ends and ends[0].parent is None and ends[0].attributes["input.value"] == "session.end")

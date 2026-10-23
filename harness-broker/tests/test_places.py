"""People and places: two places that trust each other (pc1, pc2), messages
signed by their senders, tools that reach another place, cloning a person
with its memories, and open, followable history.

Both places run in this process, each with its own state, keys and port, and
talk over HTTP exactly as two machines would.
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from helpers import RunningBroker, make_repo

from hbroker.federation import PlaceError
from hbroker.identity import Key, message_bytes, request_bytes, session_id_for

SECRET = "hb-secret-value-123456"


class TwoPlaces(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo, self.commit, _ = make_repo(self.tmp)
        os.environ["HB_TEST_SECRET"] = SECRET
        self.pc1 = RunningBroker(self.tmp, self.repo, self.commit, place="pc1")
        self.pc2 = RunningBroker(self.tmp, self.repo, self.commit, place="pc2")
        self.pc1.trust(self.pc2)
        self.pc2.trust(self.pc1)
        await self.pc1.start()
        await self.pc2.start()

    async def asyncTearDown(self):
        await self.pc1.stop()
        await self.pc2.stop()
        os.environ.pop("HB_TEST_SECRET", None)
        self._tmp.cleanup()

    async def reply(self, place, message_id, sender, token=None, wait=20):
        res = await place.tool("receive", token, **{"reply_to": message_id, "from": sender, "wait": wait})
        self.assertEqual(len(res["messages"]), 1, res)
        return res["messages"][0]

    async def ask(self, place, to, body, token=None):
        sent = await place.tool("send", token, to=to, body=body)
        return sent, await self.reply(place, sent["message_id"], sent["to"], token)

    # --- messages between places -------------------------------------------------
    async def test_spawn_elsewhere_and_talk(self):
        st = await self.pc1.tool("spawn", profile="echo", task="hello from pc1", place="pc2")
        self.assertEqual(st["state"], "idle", st)
        self.assertEqual(st["address"], f"{st['id']}@pc2")
        self.assertEqual(st["parent"], "operator@pc1")
        self.assertIn(st["id"], self.pc2.broker.manager.sessions)
        self.assertNotIn(st["id"], self.pc1.broker.manager.sessions)

        got = await self.reply(self.pc1, st["task_message_id"], st["address"])
        self.assertEqual(got["body"], "echo: hello from pc1")
        self.assertEqual(got["from"], st["address"])

        # pc2 gave the person the message with the sender's full address
        inject = (await self.pc2.tool("history", session=st["id"], kinds=["message.inject"]))["events"][0]
        self.assertTrue(inject["payload"]["text"].startswith("from: operator@pc1\n"), inject)
        # pc1 checked the reply's signature against the sender's key, which matches its id
        arrive = self.pc1.broker.store.events(kinds=["message.arrive"])[0]["payload"]
        self.assertEqual((arrive["sender"], arrive["place"]), (st["address"], "pc2"))
        self.assertEqual(session_id_for(bytes.fromhex(st["public_key"])), st["id"])

        # a conversation goes on by address; a reply needs no `to`
        _, again = await self.ask(self.pc1, st["address"], "second")
        self.assertEqual(again["body"], "echo: second")
        sent = await self.pc1.tool("send", reply_to=got["message_id"], body="reply without to")
        self.assertEqual(sent["to"], st["address"])
        self.assertEqual((await self.reply(self.pc1, sent["message_id"], st["address"]))["body"], "echo: reply without to")

    async def test_a_lost_answer_is_retried_by_the_place_not_the_sender(self):
        st = await self.pc1.tool("spawn", profile="echo", place="pc2")
        fed = self.pc1.broker.federation
        real, delivered = fed.post, []

        async def answer_lost_once(place, path, payload, timeout=60):
            res = await real(place, path, payload, timeout)
            if path == "/federation/deliver":
                delivered.append(res)
                if len(delivered) == 1:
                    raise PlaceError("connection reset", 502)  # it arrived, but the answer was lost
            return res

        fed.post = answer_lost_once
        sent = await self.pc1.tool("send", to=st["address"], body="FINAL_ONLY")
        self.assertEqual(sent["to"], st["address"])  # the sender just sees it arrive
        self.assertEqual([d.get("duplicate", False) for d in delivered], [False, True])
        arrived = self.pc2.broker.store.events(kinds=["message.arrive"])
        self.assertEqual([e["message_id"] for e in arrived], [sent["message_id"]])  # once
        retry = self.pc1.broker.store.events(kinds=["message.retry"])
        self.assertEqual(len(retry), 1)

        # a place that stays unreachable: the sender is told, after the retries
        async def down(place, path, payload, timeout=60):
            raise PlaceError("connection refused", 502)

        fed.post = down
        code, body = await self.pc1.post("/api/tools/send", {"to": st["address"], "body": "x"})
        self.assertEqual(code, 502, body)
        self.assertIn("3 attempts", body["error"])

    async def test_receive_from_someone_elsewhere_sees_them_go_quiet(self):
        st = await self.pc1.tool("spawn", profile="echo", place="pc2")
        sent = await self.pc1.tool("send", to=st["address"], body="FINAL_ONLY")
        t0 = time.monotonic()
        r = await self.pc1.tool("receive", **{"from": st["address"], "wait": 30})
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(r["messages"], [])
        self.assertTrue(r["from_quiet"], r)
        self.assertEqual(r["from_state"], "idle")
        # by name, too: pc2 says who that is
        r = await self.pc1.tool("receive", **{"from": "echo@pc2"})
        self.assertTrue(r["from_quiet"], r)
        self.assertTrue(sent["message_id"])

    async def test_residents_of_two_places_talk_through_their_bridges(self):
        a = await self.pc1.tool("spawn", profile="echo")
        b = await self.pc2.tool("spawn", profile="echo")
        cmd = json.dumps({"to": b["address"], "body": "hi b, this is a"})
        _, r = await self.ask(self.pc1, a["id"], f"BRIDGE send {cmd}")
        result = json.loads(json.loads(r["body"])["content"][0]["text"])
        self.assertEqual(result["to"], b["address"])
        # b answered a (with send reply_to), and a's next turn got it
        hist = (await self.pc1.tool("history", session=a["id"], kinds=["message.inject"], wait=20,
                                     after_seq=0))["events"]
        deadline = time.monotonic() + 20
        while len(hist) < 2 and time.monotonic() < deadline:
            last = hist[-1]["seq"]
            hist += (await self.pc1.tool("history", session=a["id"], kinds=["message.inject"], after_seq=last,
                                          wait=10))["events"]
        self.assertTrue(hist[1]["payload"]["text"].startswith(f"from: echo ({b['address']})\n"), hist[1])
        self.assertTrue(hist[1]["payload"]["text"].endswith("\n\necho: hi b, this is a"))

    # --- tools that reach another place ------------------------------------------
    async def test_tools_reach_another_place(self):
        profiles = await self.pc1.tool("list_profiles", place="pc2")
        self.assertEqual(profiles["place"], "pc2")
        self.assertIn("echo", [p["id"] for p in profiles["profiles"]])
        self.assertEqual((await self.pc1.tool("list_profiles"))["places"], ["pc2"])

        st = await self.pc1.tool("spawn", profile="echo", place="pc2")
        listed = await self.pc1.tool("list_sessions", place="pc2")
        self.assertEqual([s["address"] for s in listed["sessions"]], [st["address"]])
        self.assertEqual((await self.pc1.tool("status", session=st["address"]))["state"], "idle")
        self.assertEqual((await self.pc1.tool("status", session="echo@pc2"))["id"], st["id"])
        events = (await self.pc1.tool("history", session=st["address"]))["events"]
        self.assertIn("session.spawn", [e["kind"] for e in events])
        done = await self.pc1.tool("terminate", session=st["address"], reason="from pc1")
        self.assertEqual(done["state"], "terminated")
        self.assertEqual(self.pc2.broker.manager.sessions[st["id"]].end_reason, "from pc1")

    async def test_spawn_rules_and_parents_hold_across_places(self):
        # an external session at pc1 (e.g. a Claude Code the user runs there)
        _, me = await self.pc1.post("/api/register", {"name": "me"})
        token = me["token"]
        a = await self.pc1.tool("whoami", token)
        # echo is spawnableBy external, at another place too
        child = await self.pc1.tool("spawn", token, profile="echo", place="pc2", task="from a")
        self.assertEqual(child["parent"], a["address"])
        self.assertEqual(child["depth"], 1)
        got = await self.reply(self.pc1, child["task_message_id"], child["address"], token)
        self.assertEqual(got["body"], "echo: from a")
        # but not locked, whose spawnableBy is [reviewer]
        code, body = await self.pc1.post("/api/tools/spawn", {"profile": "locked", "place": "pc2"}, token)
        self.assertEqual(code, 403, body)
        # only an operator, the parent or the person itself may terminate
        _, other = await self.pc1.post("/api/register", {"name": "other"})
        code, body = await self.pc1.post("/api/tools/terminate", {"session": child["address"]}, other["token"])
        self.assertEqual(code, 403, body)
        done = await self.pc1.tool("terminate", token, session=child["address"])
        self.assertEqual(done["state"], "terminated")

    async def test_unknown_place(self):
        code, body = await self.pc1.post("/api/tools/send", {"to": "someone@pc3", "body": "x"})
        self.assertEqual(code, 404, body)
        self.assertIn("unknown place", body["error"])
        code, body = await self.pc1.post("/api/tools/spawn", {"profile": "echo", "place": "pc3"})
        self.assertEqual(code, 404, body)

    # --- what a place accepts from another ---------------------------------------
    def _federation_post(self, target, path, payload, signer=None, place=None, ts=None):
        signer = signer or self.pc2.broker.federation
        body = json.dumps(payload).encode()
        headers = signer.sign_headers("POST", path, body)
        if place:
            headers["X-HB-Place"] = place
        if ts is not None:
            headers["X-HB-Time"] = str(ts)
            headers["X-HB-Signature"] = signer.key.sign(request_bytes("POST", path, str(ts), body))
        req = urllib.request.Request(target.url + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _envelope(self, key, sender, to, body="forged?"):
        env = {"id": f"m-{os.urandom(8).hex()}", "from": sender, "from_name": "x", "from_key": key.public_hex,
               "to": to, "thread_id": "t-1", "reply_to": None, "body": body,
               "sent_at": "2026-10-21T00:00:00+00:00"}
        env["sig"] = key.sign(message_bytes(env))
        return env

    async def test_messages_must_be_signed_by_their_sender(self):
        key = Key.generate()
        me = session_id_for(key.public)
        ok = self._envelope(key, f"{me}@pc2", "operator@pc1", "genuine")
        code, res = await asyncio.to_thread(self._federation_post, self.pc1, "/federation/deliver", {"envelope": ok})
        self.assertEqual(code, 200, res)
        again = await asyncio.to_thread(self._federation_post, self.pc1, "/federation/deliver", {"envelope": ok})
        self.assertTrue(again[1].get("duplicate"))
        got = await self.pc1.tool("receive")
        self.assertEqual([m["body"] for m in got["messages"]], ["genuine"])

        cases = {
            "body changed after signing": {**ok, "id": "m-other", "body": "tampered"},
            "someone else's key": {**self._envelope(Key.generate(), f"{me}@pc2", "operator@pc1")},
            "a resident of another place": self._envelope(key, f"{me}@pc3", "operator@pc1"),
            "the operator without the place key": self._envelope(key, "operator@pc2", "operator@pc1"),
        }
        for what, env in cases.items():
            code, res = await asyncio.to_thread(self._federation_post, self.pc1, "/federation/deliver",
                                                {"envelope": env})
            self.assertEqual(code, 403, f"{what}: {res}")
        self.assertEqual((await self.pc1.tool("receive"))["messages"], [])

    async def test_requests_must_come_from_a_trusted_place(self):
        env = self._envelope(Key.generate(), "x@pc2", "operator@pc1")
        stranger = type(self.pc2.broker.federation)("pc3", Key.generate())
        code, _ = await asyncio.to_thread(self._federation_post, self.pc1, "/federation/deliver", {"envelope": env},
                                          stranger)
        self.assertEqual(code, 403)
        # pc2's name with a key pc1 does not know
        imposter = type(self.pc2.broker.federation)("pc2", Key.generate())
        code, _ = await asyncio.to_thread(self._federation_post, self.pc1, "/federation/deliver", {"envelope": env},
                                          imposter)
        self.assertEqual(code, 403)
        # a replayed old request
        code, res = await asyncio.to_thread(self._federation_post, self.pc1, "/federation/deliver", {"envelope": env},
                                            None, None, int(time.time()) - 3600)
        self.assertEqual(code, 403, res)
        self.assertIn("off", res["error"])

    async def test_places_do_not_relay(self):
        caller = {"address": "operator@pc2", "name": "operator", "role": "operator", "operator": True}
        code, res = await asyncio.to_thread(
            self._federation_post, self.pc1, "/federation/call",
            {"caller": caller, "tool": "status", "args": {"session": "someone@pc3"}},
        )
        self.assertEqual(code, 403, res)
        # and a place speaks only for its own residents
        code, res = await asyncio.to_thread(
            self._federation_post, self.pc1, "/federation/call",
            {"caller": {**caller, "address": "operator@pc1"}, "tool": "list_sessions", "args": {}},
        )
        self.assertEqual(code, 403, res)
        # mailboxes are read where they are kept
        code, res = await asyncio.to_thread(
            self._federation_post, self.pc1, "/federation/call", {"caller": caller, "tool": "receive", "args": {}},
        )
        self.assertEqual(code, 403, res)


class Clones(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo, self.commit, _ = make_repo(self.tmp)
        os.environ["HB_TEST_SECRET"] = SECRET
        self.b = RunningBroker(self.tmp, self.repo, self.commit)
        await self.b.start()

    async def asyncTearDown(self):
        await self.b.stop()
        os.environ.pop("HB_TEST_SECRET", None)
        self._tmp.cleanup()

    async def ask(self, to, body, token=None):
        sent = await self.b.tool("send", token, to=to, body=body)
        res = await self.b.tool("receive", token, **{"reply_to": sent["message_id"], "from": to, "wait": 20})
        self.assertEqual(len(res["messages"]), 1, res)
        return res["messages"][0]["body"]

    async def test_clone_keeps_memories_and_workspace_then_diverges(self):
        src = await self.b.tool("spawn", profile="echo", name="thinker")
        await self.ask(src["id"], "REMEMBER apple")
        await self.ask(src["id"], "WRITE note.txt written before the clone")

        twin = await self.b.tool("clone", session=src["id"])
        self.assertEqual(twin["state"], "idle", twin)
        self.assertNotEqual(twin["id"], src["id"])
        self.assertEqual(twin["name"], "thinker-clone")
        self.assertEqual(twin["cloned_from"], src["address"])
        self.assertEqual(twin["parent"], "operator@pc1")
        self.assertEqual(twin["acp_session_id"], src["acp_session_id"])  # the same conversation, resumed

        memory = json.loads(await self.ask(twin["id"], "MEMORY"))
        self.assertEqual(memory["memory"], ["apple"])
        self.assertEqual(memory["loaded"], [twin["acp_session_id"]])  # resumed with ACP session/load
        self.assertEqual(json.loads(await self.ask(twin["id"], "READ note.txt")), "written before the clone")
        acp = (await self.b.tool("history", session=twin["id"], kinds=["acp.out"]))["events"]
        self.assertEqual([e["payload"]["frame"].get("method") for e in acp][:2], ["initialize", "session/load"])

        # from here on they are two people
        await self.ask(src["id"], "REMEMBER banana")
        await self.ask(twin["id"], "REMEMBER cherry")
        self.assertEqual(json.loads(await self.ask(src["id"], "MEMORY"))["memory"], ["apple", "banana"])
        self.assertEqual(json.loads(await self.ask(twin["id"], "MEMORY"))["memory"], ["apple", "cherry"])
        await self.ask(twin["id"], "WRITE note.txt changed by the clone")
        self.assertEqual(json.loads(await self.ask(src["id"], "READ note.txt")), "written before the clone")

        clone_ev = (await self.b.tool("history", session=twin["id"], kinds=["session.clone"]))["events"][0]
        self.assertEqual(clone_ev["payload"]["source"], src["address"])
        # the clone got the source's records but its own config (instructions, settings)
        self.assertIn("You are the echo profile.", await self.ask(twin["id"], "INSTRUCTIONS"))

    async def test_a_person_can_clone_itself_mid_turn(self):
        src = await self.b.tool("spawn", profile="echo")
        await self.ask(src["id"], "REMEMBER who I am")
        res = json.loads(await self.ask(src["id"], 'BRIDGE clone {"session": "self", "name": "me-again"}'))
        twin = json.loads(res["content"][0]["text"])
        self.assertEqual((twin["name"], twin["state"], twin["parent"]), ("me-again", "idle", src["address"]))
        self.assertEqual(json.loads(await self.ask(twin["id"], "MEMORY"))["memory"], ["who I am"])

    async def test_clone_needs_a_harness_that_can_resume(self):
        src = await self.b.tool("spawn", profile="noload")
        twin = await self.b.tool("clone", session=src["id"])
        self.assertEqual(twin["state"], "failed")
        self.assertIn("loadSession", twin["end_reason"])
        self.assertEqual((await self.b.tool("status", session=src["id"]))["state"], "idle")  # the source is untouched


class OpenHistory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo, self.commit, _ = make_repo(self.tmp)
        os.environ["HB_TEST_SECRET"] = SECRET
        self.b = RunningBroker(self.tmp, self.repo, self.commit)
        await self.b.start()

    async def asyncTearDown(self):
        await self.b.stop()
        os.environ.pop("HB_TEST_SECRET", None)
        self._tmp.cleanup()

    async def test_anyone_reads_anyone_and_can_follow_live(self):
        a = await self.b.tool("spawn", profile="echo", name="a")
        b = await self.b.tool("spawn", profile="echo", name="b")
        # a reads b's thoughts although they never met and a did not start b
        sent = await self.b.tool("send", to=a["id"], body=f'BRIDGE history {json.dumps({"session": b["id"]})}')
        res = await self.b.tool("receive", **{"reply_to": sent["message_id"], "wait": 20})
        events = json.loads(json.loads(res["messages"][0]["body"])["content"][0]["text"])["events"]
        self.assertIn("session.spawn", [e["kind"] for e in events])

        # following b: a waiting read returns as soon as b does something
        last = (await self.b.tool("history", session=b["id"]))["events"][-1]["seq"]
        follow = asyncio.create_task(self.b.tool("history", session=b["id"], after_seq=last, wait=30))
        await asyncio.sleep(0.5)
        self.assertFalse(follow.done())
        t0 = time.monotonic()
        await self.b.tool("send", to=b["id"], body="think about this")
        new = (await follow)["events"]
        self.assertLess(time.monotonic() - t0, 10)
        self.assertTrue(new and all(e["seq"] > last for e in new))
        # with nothing new, a waiting read ends empty at its deadline
        last = (await self.b.tool("history", session=a["id"]))["events"][-1]["seq"]
        t0 = time.monotonic()
        self.assertEqual((await self.b.tool("history", session=a["id"], after_seq=last, wait=1))["events"], [])
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)


if __name__ == "__main__":
    unittest.main()

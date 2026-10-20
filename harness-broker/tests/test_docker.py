"""DockerRunner end to end. Needs a Docker daemon and the test image:

    docker build -f tests/docker/Dockerfile -t hb-test-agent .
    HB_TEST_DOCKER_IMAGE=$(docker inspect --format '{{.Id}}' hb-test-agent) \\
        python3 -m unittest test_docker      (from tests/)

Skipped when HB_TEST_DOCKER_IMAGE is not set.
"""

import json
import os
import subprocess
import unittest

from helpers import RunningBroker, make_repo
from test_e2e import SECRET, BrokerCase

IMAGE = os.environ.get("HB_TEST_DOCKER_IMAGE")


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


@unittest.skipUnless(IMAGE, "set HB_TEST_DOCKER_IMAGE to run the Docker tests")
class DockerTests(BrokerCase):
    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo, self.commit_a, self.commit_b = make_repo(self.tmp)
        os.environ["HB_TEST_SECRET"] = SECRET
        os.environ["HB_TEST_HOST_ONLY"] = "should-not-leak"
        self.b = RunningBroker(self.tmp, self.repo, self.commit_a, docker_image=IMAGE)
        await self.b.start()

    async def test_session_in_container(self):
        st = await self.b.tool("spawn", profile="echo", task="hello container", wait=True, wait_timeout=120)
        self.assertEqual(st["state"], "idle", st)
        sid = st["id"]
        name = f"hb-{sid}"
        reply = await self.reply_to(self.first_message_id(sid), wait=60)
        self.assertEqual(reply["body"], "echo: hello container")

        # hardening as created
        info = json.loads(docker("inspect", name).stdout)[0]
        self.assertEqual(info["HostConfig"]["CapDrop"], ["ALL"])
        self.assertIn("no-new-privileges", info["HostConfig"]["SecurityOpt"])
        self.assertEqual(info["Mounts"], [])
        self.assertEqual(info["Config"]["Image"], IMAGE)

        # environment inside the container
        sent = await self.b.tool("send", to=sid, body="ENV")
        env = json.loads((await self.reply_to(sent["message_id"], wait=60))["body"])
        self.assertEqual(env["home"], "/home/agent")
        self.assertEqual(env["uid"], 10001)
        self.assertEqual(env["cwd"], "/workspace")
        self.assertEqual(env["files"], ["README.md", "docs", "src", "src/app.py"])
        self.assertEqual(env["claude_config_dir"], "/home/agent/.hb/claude")
        self.assertIn("CLAUDE.md", env["config_files"])
        self.assertEqual(env["secret"], SECRET)
        self.assertIsNone(env["leaked_host_var"])

        # the container reaches the broker through the bridge with its token
        who = await self.b.tool("send", to=sid, body='BRIDGE status {"session": "self"}')
        result = json.loads((await self.reply_to(who["message_id"], wait=60))["body"])
        self.assertFalse(result["isError"], result)
        self.assertEqual(json.loads(result["content"][0]["text"])["id"], sid)

        done = await self.b.tool("terminate", session=sid)
        self.assertEqual(done["state"], "terminated")
        self.assertEqual(docker("ps", "-aq", "--filter", f"name={name}").stdout.strip(), "")
        transcript = self.b.cfg.history_dir / "sessions" / sid / "harness" / "projects" / "transcript.jsonl"
        self.assertTrue(transcript.is_file())

    async def test_clone_in_container(self):
        st = await self.b.tool("spawn", profile="echo", wait=True, wait_timeout=120)
        _, r = await self.ask(st["id"], "REMEMBER the container", wait=60)
        await self.ask(st["id"], "WRITE note.txt made inside", wait=60)
        twin = await self.b.tool("clone", session=st["id"], wait=True, wait_timeout=120)
        self.assertEqual(twin["state"], "idle", twin)
        _, r = await self.ask(twin["id"], "MEMORY", wait=60)
        self.assertEqual(json.loads(r["body"])["memory"], ["the container"])
        _, r = await self.ask(twin["id"], "READ note.txt", wait=60)
        self.assertEqual(json.loads(r["body"]), "made inside")
        _, r = await self.ask(twin["id"], "WRITE other.txt the clone can write", wait=60)  # workspace owned by agent
        self.assertEqual(r["body"], "wrote other.txt")
        info = json.loads(docker("inspect", f"hb-{twin['id']}").stdout)[0]
        self.assertEqual(info["HostConfig"]["CapDrop"], ["ALL"])
        for sid in (st["id"], twin["id"]):
            await self.b.tool("terminate", session=sid)

    async def test_codex_plan_login_in_container(self):
        st = await self.b.tool("spawn", profile="codexplan", wait=True, wait_timeout=120)
        self.assertEqual(st["state"], "idle", st)
        _, r = await self.ask(st["id"], "CODEX_AUTH", wait=60)
        seen = json.loads(r["body"])
        self.assertEqual((seen["auth_file"], seen["refresh_token"]), (True, ""))
        await self.b.tool("terminate", session=st["id"])
        records = self.b.cfg.history_dir / "sessions" / st["id"] / "harness"
        self.assertTrue(records.is_dir())
        self.assertEqual(list(records.rglob("auth.json")), [])


if __name__ == "__main__":
    unittest.main()

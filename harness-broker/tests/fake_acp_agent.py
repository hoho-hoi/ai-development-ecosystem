#!/usr/bin/env python3
"""A tiny ACP agent for tests (stdlib only).

It behaves like an agent that answers with `send`: for every prompt it works
out a reply and sends it through the broker's MCP bridge (the "hb" server it
was given) as send(reply_to=<message_id>, body=<reply>). It also writes the
reply as its final text, which the broker records and delivers to no one.
Without a bridge it can only write the final text.

The reply is "echo: <body>", except for these bodies:
    SLEEP <s>          wait s seconds, then reply
    DELAY <s> <body>   wait s seconds, then handle <body>
    PERMISSION <kind>  ask session/request_permission for a tool of that kind,
                       reply with the outcome as JSON
    ENV                reply with JSON describing the environment it runs in
    MCP                reply with the mcpServers it was given
    RAW                reply with the exact prompt text
    SESSION            reply with the ACP session id and prompt count
    INSTRUCTIONS       reply with the instruction/skill/settings files it sees
    BRIDGE <tool> <json>
                       call that bridge tool, reply with the tool result;
                       "$MSG" in the JSON becomes the current message id
    FINAL_ONLY         write a final text but send nothing
    SILENT             write nothing and send nothing
    CRASH              exit with code 3
Every prompt is appended to a transcript under $CLAUDE_CONFIG_DIR (or
$CODEX_HOME), standing in for the harness's own session records.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

state = {"mcp": None, "next_id": 1000, "sessions": 0, "prompts": {}, "closed": [], "bridge": None, "rpc": 10}


def write(frame):
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def read_frame():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def transcript(entry):
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CODEX_HOME")
    if not base:
        return
    d = Path(base) / "projects"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "transcript.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def parse(text):
    """Header lines up to the first blank line, then the body."""
    head, _, body = text.partition("\n\n")
    meta = {}
    for line in head.split("\n"):
        k, sep, v = line.partition(": ")
        if sep:
            meta[k] = v
    return meta, body


def chunk(sid, text):
    write(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}},
        }
    )


def ask_permission(session_id, kind):
    rid = state["next_id"]
    state["next_id"] += 1
    write(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {"toolCallId": "call_1", "title": f"do {kind}", "kind": kind},
                "options": [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
    )
    while True:
        f = read_frame()
        if f.get("id") == rid and "method" not in f:
            return f.get("result")


def rpc(frame):
    p = state["bridge"]
    p.stdin.write(json.dumps(frame) + "\n")
    p.stdin.flush()
    if "id" in frame:
        while True:
            reply = json.loads(p.stdout.readline())
            if reply.get("id") == frame["id"]:
                return reply


def bridge():
    """Start the broker bridge once and keep it, as a harness keeps its MCP servers."""
    servers = [s for s in (state["mcp"] or []) if s.get("name") == "hb"]
    if not servers:
        return None
    if state["bridge"] is None:
        srv = servers[0]
        env = os.environ.copy()
        env.update({e["name"]: e["value"] for e in srv.get("env", [])})
        state["bridge"] = subprocess.Popen(
            [srv["command"], *srv["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True
        )
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake", "version": "0"}}})
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return state["bridge"]


def call_tool(tool, args):
    if bridge() is None:
        return {"error": "no bridge"}
    state["rpc"] += 1
    res = rpc({"jsonrpc": "2.0", "id": state["rpc"], "method": "tools/call", "params": {"name": tool, "arguments": args}})
    return res.get("result") if "result" in res else {"rpc_error": res.get("error")}


def handle_prompt(rid, params):
    sid = params["sessionId"]
    text = params["prompt"][0]["text"]
    state["prompts"][sid] = state["prompts"].get(sid, 0) + 1
    meta, body = parse(text)
    transcript({"prompt": text})
    while body.startswith("DELAY "):
        _, secs, body = body.split(" ", 2)
        time.sleep(float(secs))
    send_reply = True
    if body.startswith("SLEEP "):
        time.sleep(float(body.split()[1]))
        reply = "echo: " + body
    elif body.startswith("PERMISSION "):
        reply = json.dumps(ask_permission(sid, body.split()[1]))
    elif body == "ENV":
        reply = json.dumps(
            {
                "home": os.environ.get("HOME"),
                "uid": os.getuid(),
                "cwd": os.getcwd(),
                "files": sorted(str(p.relative_to(os.getcwd())) for p in Path.cwd().rglob("*") if ".git" not in p.parts),
                "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
                "config_files": sorted(
                    str(p.relative_to(os.environ["CLAUDE_CONFIG_DIR"]))
                    for p in Path(os.environ.get("CLAUDE_CONFIG_DIR", "/nonexistent")).rglob("*")
                ),
                "model": os.environ.get("ANTHROPIC_MODEL"),
                "secret": os.environ.get("HB_TEST_SECRET"),
                "leaked_host_var": os.environ.get("HB_TEST_HOST_ONLY"),
            }
        )
    elif body == "RAW":
        reply = json.dumps({"prompt": text})
    elif body == "SESSION":
        reply = json.dumps({"session": sid, "prompts_in_session": state["prompts"].get(sid, 0), "closed": state["closed"]})
    elif body == "INSTRUCTIONS":
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CODEX_HOME") or ""
        found = {}
        for name in ("CLAUDE.md", "AGENTS.md"):
            f = Path(base) / name
            if f.is_file():
                found[name] = f.read_text()
        found["skills"] = sorted(p.name for p in (Path(base) / "skills").glob("*")) if base else []
        settings = Path(base) / "settings.json"
        found["settings"] = json.loads(settings.read_text()) if settings.is_file() else None
        reply = json.dumps(found)
    elif body == "MCP":
        reply = json.dumps(state["mcp"])
    elif body.startswith("BRIDGE "):
        _, tool, raw = body.split(" ", 2)
        reply = json.dumps(call_tool(tool, json.loads(raw.replace("$MSG", meta.get("message_id", "")))))
    elif body == "FINAL_ONLY":
        reply, send_reply = "this final text goes to no one", False
    elif body == "SILENT":
        reply, send_reply = "", False
    elif body == "CRASH":
        sys.exit(3)
    else:
        reply = "echo: " + body
    if send_reply and meta.get("message_id"):
        call_tool("send", {"reply_to": meta["message_id"], "body": reply})
    if reply:
        for part in (reply[: len(reply) // 2], reply[len(reply) // 2 :]):
            chunk(sid, part)
    transcript({"reply": reply})
    write({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


def main():
    while True:
        f = read_frame()
        method, rid, params = f.get("method"), f.get("id"), f.get("params") or {}
        if method == "initialize":
            write(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {"loadSession": False, "sessionCapabilities": {"close": {}}},
                        "agentInfo": {"name": "fake-agent", "version": "0"},
                        "authMethods": [],
                    },
                }
            )
        elif method == "session/new":
            state["mcp"] = params.get("mcpServers")
            state["sessions"] += 1
            write({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": f"fake-session-{state['sessions']}"}})
        elif method == "session/close":
            state["closed"].append(params.get("sessionId"))
            write({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/prompt":
            handle_prompt(rid, params)
        elif method == "session/cancel":
            pass
        elif rid is not None:
            write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not found"}})


if __name__ == "__main__":
    main()

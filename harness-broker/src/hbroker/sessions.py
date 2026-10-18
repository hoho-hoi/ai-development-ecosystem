"""Sessions: spawn, message routing, lifecycle and limits.

States of a managed session:
    preparing → starting → idle ⇄ busy → terminated
                    └──────────┴────────→ failed
External sessions (registered through the bridge) are "active" until they
unregister or are terminated. The operator (the broker key holder, e.g. the
`hb` CLI) is a permanent pseudo-session named "operator".

Communication has two primitives, send and receive. Every session (managed,
external, the operator) has one mailbox: the message table. A message stays
unread until it is taken in one of two ways:

  * receive: the recipient takes matching messages, waiting a bounded time.
  * turn end (managed sessions only): when a turn ends, the broker takes the
    oldest unread message and prompts the session with it. Ending a turn is
    an unbounded receive.

Results of the other tools carry a notice of unread messages (count and
senders); the content is read with receive.

One boundary rule translates final responses, because models are trained to
answer with them. When a turn that a message M started (M not itself a reply)
ends:
  * the session sent a reply to M (send to M's sender with reply_to = M):
    the final text is only recorded;
  * otherwise a non-empty final text is sent to M's sender as the reply;
  * otherwise M's sender gets a notice that the turn ended without a reply.
Turns started by a reply send nothing automatically, so two sessions never
answer each other's answers forever.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .acp_client import AcpClient
from .config import BrokerConfig
from .harness import harness_record_dir, prepare_harness_config, resolve_definition
from .modules import ModuleCache
from .jsonrpc import ConnectionClosed, RpcError
from .profiles import Profile, ProfileRegistry
from .runner import RunHandle, Runner
from .store import HistoryStore, now
from .workspace import prepare_workspace

log = logging.getLogger(__name__)

LIVE = {"preparing", "starting", "idle", "busy", "active"}
OPERATOR = "operator"


class BrokerError(Exception):
    status = 400


class NotFound(BrokerError):
    status = 404


class Forbidden(BrokerError):
    status = 403


def _id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Session:
    id: str
    name: str
    kind: str  # managed | external | operator
    harness: str | None
    state: str
    depth: int
    spawn_budget: int
    token: str
    created_at: str
    parent_id: str | None = None
    profile: Profile | None = None
    ended_at: str | None = None
    end_reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # managed runtime
    handle: RunHandle | None = None
    acp: AcpClient | None = None
    acp_session_id: str | None = None
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    finalized: asyncio.Event = field(default_factory=asyncio.Event)
    last_reply: str | None = None
    turns: int = 0
    usage: dict[str, Any] | None = None
    current: dict[str, Any] | None = None  # message row whose turn is running
    replied: bool = False  # the session replied to `current` during the turn
    # set whenever a message is put in this session's mailbox
    mail: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def live(self) -> bool:
        return self.state in LIVE

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "harness": self.harness,
            "profile": f"{self.profile.id}@{self.profile.version}" if self.profile else None,
            "state": self.state,
            "parent": self.parent_id,
            "depth": self.depth,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
        }

    def row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "harness": self.harness,
            "profile_id": self.profile.id if self.profile else None,
            "profile_version": self.profile.version if self.profile else None,
            "state": self.state,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "token_hash": token_hash(self.token),
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "meta": self.meta,
        }


def format_injection(msg: dict[str, Any], sender: Session | None, tools: bool) -> str:
    """The exact text a managed session receives for a message (envelope format)."""
    who = f"{sender.name} ({msg['from_id']})" if sender else msg["from_id"]
    lines = [
        f"[harness-broker] message from {who}",
        f"message_id: {msg['id']}",
        f"thread_id: {msg['thread_id']}",
    ]
    if msg["reply_to"]:
        lines.append(f"in_reply_to: {msg['reply_to']}")
    lines += ["", msg["body"], ""]
    if msg["reply_to"]:
        lines.append(
            "(This is a reply. Nothing is sent back automatically when your turn ends"
            + ("; use the hb `send` tool to write to anyone.)" if tools else ".)")
        )
    elif tools:
        lines.append(
            "(When your turn ends, your final text goes back to the sender as the reply, unless you "
            f"have already replied with the hb `send` tool (reply_to: {msg['id']}). Messages that arrive "
            "while you work are delivered after this turn, or earlier with the hb `receive` tool.)"
        )
    else:
        lines.append("(When your turn ends, your final text goes back to the sender as the reply.)")
    return "\n".join(lines)


def public_message(row: dict[str, Any], sender: Session | None) -> dict[str, Any]:
    """A message as receive returns it."""
    return {
        "message_id": row["id"],
        "from": row["from_id"],
        "from_name": sender.name if sender else None,
        "thread_id": row["thread_id"],
        "reply_to": row["reply_to"],
        "origin": row.get("origin", "send"),
        "body": row["body"],
        "sent_at": row["ts"],
    }


class SessionManager:
    def __init__(self, cfg: BrokerConfig, store: HistoryStore, profiles: ProfileRegistry, runner: Runner):
        self.cfg = cfg
        self.store = store
        self.profiles = profiles
        self.runner = runner
        self.sessions: dict[str, Session] = {}
        self._by_token: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.modules = ModuleCache(cfg.state_dir / "modules")
        self.operator = self._add(
            Session(
                id=OPERATOR,
                name=OPERATOR,
                kind="operator",
                harness=None,
                state="active",
                depth=0,
                spawn_budget=cfg.max_spawn_depth,
                token=secrets.token_urlsafe(24),  # never used; the broker key authenticates the operator
                created_at=now(),
            )
        )

    # --- bookkeeping --------------------------------------------------------
    def _add(self, s: Session) -> Session:
        self.sessions[s.id] = s
        self._by_token[token_hash(s.token)] = s.id
        self.store.add_secret(f"token:{s.id}", s.token)
        self.store.upsert_session(s.row())
        return s

    def _set_state(self, s: Session, state: str, **extra: Any) -> None:
        old, s.state = s.state, state
        self.store.upsert_session(s.row())
        self.store.record("session.state", session_id=s.id, old=old, new=state, **extra)
        if state in ("idle", "failed", "terminated"):
            s.ready.set()

    def by_token(self, token: str) -> Session:
        sid = self._by_token.get(token_hash(token))
        s = self.sessions.get(sid) if sid else None
        if not s or not s.live:
            raise Forbidden("unknown or expired session token")
        return s

    def resolve(self, ref: str, live_only: bool = True) -> Session:
        if ref in self.sessions and (self.sessions[ref].live or not live_only):
            return self.sessions[ref]
        matches = [s for s in self.sessions.values() if s.name == ref and (s.live or not live_only)]
        if not matches:
            raise NotFound(f"no {'live ' if live_only else ''}session {ref!r}")
        return max(matches, key=lambda s: s.created_at)

    def _unique_name(self, wanted: str) -> str:
        taken = {s.name for s in self.sessions.values() if s.live}
        if wanted not in taken:
            return wanted
        n = 2
        while f"{wanted}-{n}" in taken:
            n += 1
        return f"{wanted}-{n}"

    # --- external sessions -----------------------------------------------
    def register_external(self, name: str, harness: str | None, cwd: str | None, channel: bool) -> Session:
        s = self._add(
            Session(
                id=_id("s"),
                name=self._unique_name(name or "external"),
                kind="external",
                harness=harness,
                state="active",
                depth=0,
                spawn_budget=self.cfg.max_spawn_depth,
                token=secrets.token_urlsafe(32),
                created_at=now(),
                meta={"cwd": cwd, "channel": channel},
            )
        )
        self.store.record("session.register", session_id=s.id, name=s.name, harness=harness, cwd=cwd, channel=channel)
        return s

    # --- spawn ------------------------------------------------------------
    def _check_spawn_policy(self, caller: Session, profile: Profile) -> None:
        if caller.kind == "operator":
            return
        allowed = profile.spawnable_by
        who = "external" if caller.kind == "external" else (caller.profile.id if caller.profile else "")
        if "*" not in allowed and who not in allowed:
            raise Forbidden(f"{caller.name} ({who}) may not spawn profile {profile.id!r}; spawnableBy={allowed}")
        if caller.spawn_budget < 1:
            raise Forbidden(f"{caller.name} has no spawn depth left (depth {caller.depth})")

    def _check_capacity(self, profile: Profile) -> None:
        live = [s for s in self.sessions.values() if s.kind == "managed" and s.live]
        if len(live) >= self.cfg.max_sessions:
            raise Forbidden(f"broker limit reached: {self.cfg.max_sessions} live managed sessions")
        same = [s for s in live if s.profile and s.profile.id == profile.id]
        if len(same) >= profile.limits.max_concurrent:
            raise Forbidden(f"profile {profile.id!r} limit reached: {profile.limits.max_concurrent} live sessions")

    async def spawn(
        self,
        caller: Session,
        profile_id: str,
        task: str | None = None,
        ref: str | None = None,
        name: str | None = None,
        wait: bool = True,
        wait_timeout: float = 600,
    ) -> dict[str, Any]:
        try:
            profile = self.profiles.get(profile_id)
        except KeyError as e:
            raise NotFound(str(e)) from None
        async with self._lock:
            self._check_spawn_policy(caller, profile)
            self._check_capacity(profile)
            budget = caller.spawn_budget - 1
            s = self._add(
                Session(
                    id=_id("s"),
                    name=self._unique_name(name or profile.id),
                    kind="managed",
                    harness=profile.runtime.harness,
                    state="preparing",
                    depth=caller.depth + 1,
                    spawn_budget=min(budget, profile.limits.max_spawn_depth),
                    token=secrets.token_urlsafe(32),
                    created_at=now(),
                    parent_id=caller.id,
                    profile=profile,
                    meta={"ref": ref or profile.workspace.ref},
                )
            )
        self.store.record(
            "session.spawn",
            session_id=s.id,
            caller=caller.id,
            profile=profile.resolved_dump(),
            fingerprint=profile.fingerprint,
            task=task,
            ref_override=ref,
            runner=self.runner.name,
        )
        s.tasks.append(asyncio.create_task(self._launch(s, ref)))
        task_message_id = None
        if task:
            task_message_id = (await self.send(caller, s.id, task))["message_id"]
        if wait:
            try:
                await asyncio.wait_for(s.ready.wait(), wait_timeout)
            except asyncio.TimeoutError:
                pass
        return {**self.status(s.id), "task_message_id": task_message_id}

    async def _launch(self, s: Session, ref: str | None) -> None:
        assert s.profile is not None
        profile = s.profile
        bundle = self.cfg.state_dir / "bundles" / s.id
        stderr_path = self.cfg.history_dir / "sessions" / s.id / "agent.stderr.log"
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            bundle.mkdir(parents=True)
            manifest = await asyncio.to_thread(prepare_workspace, profile, bundle / "workspace", ref)
            self.store.record("session.workspace", session_id=s.id, **manifest)
            ws_path, hb_path = self.runner.runtime_paths(bundle)
            (bundle / "hb").mkdir()
            instructions, skill_dirs, module_manifest = await asyncio.to_thread(
                resolve_definition, profile, self.modules
            )
            if module_manifest:
                self.store.record("session.module", session_id=s.id, **module_manifest)
            env = prepare_harness_config(profile, bundle / "hb", hb_path, instructions, skill_dirs)
            self.store.record("session.harness_env", session_id=s.id, env=env, secrets=profile.runtime.secrets)
            for name in profile.runtime.secrets:
                if name in os.environ:
                    self.store.add_secret(name, os.environ[name])
            if s.state != "preparing":  # terminated while preparing
                return
            self._set_state(s, "starting")
            s.handle = await self.runner.start(s.id, profile, bundle, env, stderr_path)
            self.store.record("session.process", session_id=s.id, **s.handle.info)
            s.tasks.append(asyncio.create_task(self._watch_process(s)))

            def tap(direction: str, frame: dict[str, Any]) -> None:
                self.store.record(f"acp.{direction}", session_id=s.id, frame=frame)

            def on_update(params: dict[str, Any]) -> None:
                upd = params.get("update") or {}
                if upd.get("sessionUpdate") == "usage_update":
                    s.usage = upd

            def on_permission(params: dict[str, Any], outcome: dict[str, Any]) -> None:
                self.store.record("acp.permission", session_id=s.id, request=params, outcome=outcome)

            s.acp = AcpClient(
                s.handle.reader,
                s.handle.writer,
                permissions=profile.runtime.permissions,
                tap=tap,
                on_update=on_update,
                on_permission=on_permission,
            )
            await s.acp.initialize()
            mcp_servers: list[dict[str, Any]] = []
            if profile.runtime.broker_tools:
                mcp_servers.append(
                    {
                        "name": "hb",
                        "command": s.handle.bridge_command[0],
                        "args": s.handle.bridge_command[1:],
                        "env": [
                            {"name": "HB_BROKER_URL", "value": s.handle.broker_url},
                            {"name": "HB_TOKEN", "value": s.token},
                        ],
                    }
                )
            mcp_servers += [
                {
                    "name": m.name,
                    "command": m.command,
                    "args": m.args,
                    "env": [{"name": k, "value": v} for k, v in m.env.items()],
                }
                for m in profile.runtime.mcp_servers
            ]
            s.mcp_servers = mcp_servers
            s.acp_session_id = await s.acp.new_session(s.handle.workspace_path, mcp_servers)
            if s.state != "starting":
                return
            self._set_state(s, "idle", acp_session_id=s.acp_session_id)
            s.tasks.append(asyncio.create_task(self._worker(s)))
            s.tasks.append(asyncio.create_task(self._deadline(s, profile.limits.max_runtime_minutes * 60)))
        except Exception as e:
            log.exception("launch of %s failed", s.id)
            self.store.record("session.error", session_id=s.id, stage="launch", error=repr(e))
            await self._finalize(s, "failed", f"launch failed: {e}")

    async def _fresh_conversation(self, s: Session) -> None:
        """per_message mode: close the previous ACP session when the agent
        supports it, then open a new one in the same process."""
        assert s.acp is not None and s.handle is not None
        old = s.acp_session_id
        caps = (s.acp.agent_info.get("agentCapabilities") or {}).get("sessionCapabilities") or {}
        if old and caps.get("close") is not None:
            try:
                await s.acp.peer.request("session/close", {"sessionId": old}, timeout=10)
            except Exception as e:
                self.store.record("session.error", session_id=s.id, stage="session_close", error=repr(e))
        s.acp_session_id = await s.acp.new_session(s.handle.workspace_path, s.mcp_servers)
        self.store.record("session.conversation", session_id=s.id, acp_session_id=s.acp_session_id, previous=old)

    async def _watch_process(self, s: Session) -> None:
        assert s.handle is not None
        code = await s.handle.process.wait()
        if s.live:
            await self._finalize(s, "failed", f"agent process exited with code {code}")

    async def _deadline(self, s: Session, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if s.live:
            self.store.record("session.limit", session_id=s.id, limit="maxRuntimeMinutes", seconds=seconds)
            await self._finalize(s, "terminated", "maxRuntimeMinutes reached")

    async def _worker(self, s: Session) -> None:
        """Turn loop: ending a turn is an unbounded receive of the oldest
        unread message, which becomes the next prompt."""
        while s.live:
            got = await self._wait_mail(s, lambda: self._take(s, limit=1, via="turn"), None)
            if got and s.live:
                if not await self._turn(s, got[0]):
                    return

    async def _turn(self, s: Session, msg: dict[str, Any]) -> bool:
        assert s.acp is not None and s.acp_session_id is not None and s.profile is not None
        rt = s.profile.runtime
        sender = self.sessions.get(msg["from_id"])
        text = msg["body"] if rt.message_format == "body" else format_injection(msg, sender, rt.broker_tools)
        s.current, s.replied = msg, False
        self._set_state(s, "busy", message_id=msg["id"])
        self.store.record(
            "message.inject", session_id=s.id, thread_id=msg["thread_id"], message_id=msg["id"], via="turn", text=text
        )
        try:
            if rt.session_mode == "per_message" and s.turns > 0:
                await self._fresh_conversation(s)
            s.turns += 1
            stop_reason, final = await s.acp.prompt(s.acp_session_id, text)
        except (ConnectionClosed, RpcError) as e:
            self.store.record("session.error", session_id=s.id, stage="prompt", error=repr(e))
            await self._finalize(s, "failed", f"prompt failed: {e}")
            return False
        s.last_reply = final
        outcome = await self._turn_boundary(s, msg, final, stop_reason) if s.live else "session ended"
        self.store.record(
            "turn.end",
            session_id=s.id,
            thread_id=msg["thread_id"],
            message_id=msg["id"],
            stop_reason=stop_reason,
            reply=final,
            outcome=outcome,
        )
        s.current, s.replied = None, False
        if not s.live:
            return False
        self._set_state(s, "idle")
        return True

    async def _turn_boundary(self, s: Session, msg: dict[str, Any], final: str, stop_reason: str) -> str:
        """The one translation rule for final responses (see module docstring)."""
        if msg["reply_to"]:
            return "turn started by a reply: final text recorded only"
        if s.replied:
            return "replied during the turn: final text recorded only"
        sender = self.sessions.get(msg["from_id"])
        if not sender or not sender.live or sender.id == s.id:
            return "sender gone: final text recorded only"
        if final.strip():
            await self._post(s, sender, final, msg["thread_id"], msg["id"], None, origin="final")
            return "final text sent as the reply"
        await self._post(
            s, sender, f"({s.name} ended its turn without replying; stopReason={stop_reason})",
            msg["thread_id"], msg["id"], None, origin="notice",
        )
        return "no reply: sender notified"

    # --- messaging ----------------------------------------------------------
    async def send(
        self,
        caller: Session,
        to: str,
        body: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Put a message in the target's mailbox and return at once."""
        target = self.resolve(to)
        if idempotency_key:
            existing = self.store.find_idempotent(caller.id, idempotency_key)
            if existing:
                return {"message_id": existing["id"], "thread_id": existing["thread_id"], "duplicate": True}
        cur = caller.current
        if cur and reply_to == cur["id"] and target.id == cur["from_id"]:
            caller.replied = True
        return await self._post(caller, target, body, thread_id, reply_to, idempotency_key)

    async def _post(
        self,
        sender: Session,
        target: Session,
        body: str,
        thread_id: str | None,
        reply_to: str | None,
        idempotency_key: str | None,
        origin: str = "send",
    ) -> dict[str, Any]:
        if not thread_id and reply_to:
            original = self.store.message(reply_to)
            thread_id = original["thread_id"] if original else None
        thread_id = thread_id or _id("t")
        ahead = len(self.store.unread(target.id))
        mid = _id("m")
        self.store.insert_message(
            {
                "id": mid,
                "ts": now(),
                "from_id": sender.id,
                "to_id": target.id,
                "thread_id": thread_id,
                "reply_to": reply_to,
                "body": body,
                "idempotency_key": idempotency_key,
                "delivery": "mailbox",
                "origin": origin,
            }
        )
        self.store.record(
            "message.send",
            session_id=sender.id,
            thread_id=thread_id,
            message_id=mid,
            to=target.id,
            body=body,
            reply_to=reply_to,
            origin=origin,
        )
        target.mail.set()
        return {
            "message_id": mid,
            "thread_id": thread_id,
            "to": target.id,
            "to_state": target.state,
            "queued_ahead": ahead,
        }

    def _take(
        self, s: Session, *, via: str, from_id: str | None = None, reply_to: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Mark matching unread messages delivered and return them (oldest first)."""
        rows = self.store.unread(s.id, from_id=from_id, reply_to=reply_to, limit=limit)
        for m in rows:
            self.store.mark_delivered(m["id"], via)
            self.store.record("message.deliver", session_id=s.id, thread_id=m["thread_id"], message_id=m["id"], via=via)
        return rows

    async def _wait_mail(self, s: Session, take: Any, wait: float | None) -> list[dict[str, Any]]:
        """Call take() until it returns something, s ends, or `wait` seconds
        pass (None: no limit). take() runs without yielding to the event loop,
        so two waiters never take the same message."""
        loop = asyncio.get_running_loop()
        deadline = None if wait is None else loop.time() + wait
        while True:
            got = take()
            if got or not s.live:
                return got
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return got
            s.mail.clear()
            got = take()
            if got:
                return got
            try:
                await asyncio.wait_for(s.mail.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    async def receive(
        self, s: Session, from_ref: str | None = None, reply_to: str | None = None, wait: float = 0
    ) -> dict[str, Any]:
        """Take the caller's unread messages that match the filters, waiting up
        to `wait` seconds (max 600) for the first one."""
        wait = max(0.0, min(float(wait or 0), 600.0))
        from_id = self.resolve(from_ref, live_only=False).id if from_ref else None
        rows = await self._wait_mail(s, lambda: self._take(s, via="receive", from_id=from_id, reply_to=reply_to), wait)
        return {
            "messages": [public_message(m, self.sessions.get(m["from_id"])) for m in rows],
            "unread_remaining": len(self.store.unread(s.id)),
        }

    def unread_notice(self, s: Session) -> dict[str, Any] | None:
        """Count and senders of s's unread messages, attached to tool results."""
        by = self.store.unread_by_sender(s.id)
        if not by:
            return None
        return {
            "count": sum(n for _, n in by),
            "from": [
                {"session": sid, "name": self.sessions[sid].name if sid in self.sessions else None, "count": n}
                for sid, n in by
            ],
        }

    # --- queries ------------------------------------------------------------
    def list_sessions(self, profile: str | None = None, state: str | None = None, include_ended: bool = False) -> list[dict[str, Any]]:
        out = []
        for s in self.sessions.values():
            if s.kind == "operator":
                continue
            if not include_ended and not s.live:
                continue
            if profile and (not s.profile or s.profile.id != profile):
                continue
            if state and s.state != state:
                continue
            out.append(s.summary())
        return out

    def status(self, ref: str) -> dict[str, Any]:
        s = self.resolve(ref, live_only=False)
        info = s.summary()
        start = s.created_at
        info.update(
            {
                "elapsed_seconds": None,
                "last_reply": s.last_reply,
                "usage": s.usage,
                "unread": len(self.store.unread(s.id)),
                "acp_session_id": s.acp_session_id,
                "fingerprint": s.profile.fingerprint if s.profile else None,
            }
        )
        end = s.ended_at or now()
        info["elapsed_seconds"] = round(
            (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 1
        )
        return info

    def history(self, **query: Any) -> list[dict[str, Any]]:
        if query.get("session"):
            query["session_id"] = self.resolve(query.pop("session"), live_only=False).id
        else:
            query.pop("session", None)
        return self.store.events(**query)

    # --- termination ------------------------------------------------------
    async def terminate(self, caller: Session, ref: str, reason: str | None = None) -> dict[str, Any]:
        s = self.resolve(ref, live_only=False)
        if caller.kind != "operator" and caller.id not in (s.id, s.parent_id):
            raise Forbidden("only the operator, the spawner or the session itself may terminate it")
        if not s.live:
            return self.status(s.id)
        if s.kind == "managed":
            await self._finalize(s, "terminated", reason or f"terminated by {caller.id}")
        else:
            s.ended_at = now()
            s.end_reason = reason or "unregistered"
            self._set_state(s, "terminated")
            await self._drop_mail(s, s.end_reason)
            s.mail.set()  # wake pending receives
            self.store.record("session.end", session_id=s.id, state="terminated", reason=s.end_reason)
        return self.status(s.id)

    async def _drop_mail(self, s: Session, reason: str) -> None:
        """s is ending: its unread messages will never be read. Senders who
        expect an answer (the message is not itself a reply) are told."""
        unanswered = []
        cur = s.current
        if cur and not s.replied and not cur["reply_to"]:
            unanswered.append(cur)
        for m in self._take(s, via="dropped"):
            self.store.record("message.dropped", session_id=s.id, thread_id=m["thread_id"], message_id=m["id"], reason=reason)
            if not m["reply_to"]:
                unanswered.append(m)
        for m in unanswered:
            sender = self.sessions.get(m["from_id"])
            if sender and sender.live and sender.id != s.id:
                await self._post(
                    s, sender, f"(session {s.name} ({s.id}) ended before answering: {reason})",
                    m["thread_id"], m["id"], None, origin="notice",
                )

    async def _finalize(self, s: Session, state: str, reason: str) -> None:
        if not s.live:
            await s.finalized.wait()
            return
        s.ended_at = now()
        s.end_reason = reason
        prev = s.state
        s.state = "stopping"
        current = asyncio.current_task()
        for t in s.tasks:
            if t is not current:
                t.cancel()
        await self._drop_mail(s, reason)
        s.current = None
        try:
            if s.acp and prev == "busy" and s.acp_session_id:
                try:
                    await asyncio.wait_for(s.acp.cancel(s.acp_session_id), 2)
                except Exception:
                    pass
            if s.handle:
                await self.runner.stop(s.handle)
                assert s.profile is not None
                dest = self.cfg.history_dir / "sessions" / s.id / "harness"
                ok = await self.runner.collect(s.handle, harness_record_dir(s.profile), dest)
                self.store.record("session.harness_records", session_id=s.id, collected=ok, path=str(dest))
                await self.runner.destroy(s.handle)
            elif s.profile is not None and not self.cfg.keep_bundles:
                shutil.rmtree(self.cfg.state_dir / "bundles" / s.id, ignore_errors=True)
            if s.acp:
                await s.acp.close()
        except Exception as e:
            log.exception("cleanup of %s failed", s.id)
            self.store.record("session.error", session_id=s.id, stage="cleanup", error=repr(e))
        finally:
            s.state = prev  # let _set_state log the real transition
            self._set_state(s, state, reason=reason)
            self.store.record("session.end", session_id=s.id, state=state, reason=reason)
            s.mail.set()
            s.finalized.set()

    async def shutdown(self) -> None:
        live = [s for s in self.sessions.values() if s.kind == "managed" and s.live]
        await asyncio.gather(*(self._finalize(s, "terminated", "broker shutdown") for s in live), return_exceptions=True)

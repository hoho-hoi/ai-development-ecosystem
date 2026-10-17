"""Sessions: spawn, message routing, lifecycle and limits.

States of a managed session:
    preparing → starting → idle ⇄ busy → terminated
                    └──────────┴────────→ failed
External sessions (registered through the bridge) are "active" until they
unregister or are terminated. The operator (the broker key holder, e.g. the
`hb` CLI) is a permanent pseudo-session named "operator".

Delivery: every send is written to the message table first. Managed targets
get the message as an ACP session/prompt (queued while a turn runs);
external targets read it from their inbox, either pulled by `inbox` or pushed
by the bridge's channel mode.
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
class Message:
    id: str
    from_id: str
    to_id: str
    thread_id: str
    body: str
    reply_to: str | None
    auto_reply: bool
    ts: str


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
    queue: asyncio.Queue[Message | None] | None = None
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    finalized: asyncio.Event = field(default_factory=asyncio.Event)
    last_reply: str | None = None
    turns: int = 0
    usage: dict[str, Any] | None = None
    current: Message | None = None  # message whose turn is running
    # external / operator
    inbox_event: asyncio.Event = field(default_factory=asyncio.Event)

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


def format_injection(msg: Message, sender: Session | None) -> str:
    """The exact text a managed session receives for a message."""
    who = f"{sender.name} ({msg.from_id})" if sender else msg.from_id
    lines = [
        f"[harness-broker] message from {who}",
        f"message_id: {msg.id}",
        f"thread_id: {msg.thread_id}",
    ]
    if msg.reply_to:
        lines.append(f"in_reply_to: {msg.reply_to}")
    lines += ["", msg.body, ""]
    if msg.auto_reply:
        lines.append(
            "(Your final answer in this turn is returned to the sender automatically. "
            "Use the hb `send` tool only for additional messages.)"
        )
    else:
        lines.append("(No automatic reply is sent for this message.)")
    return "\n".join(lines)


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
        # message id -> future resolved with the reply, for send(wait=...)
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
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
                    queue=asyncio.Queue(),
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
        if task:
            await self.send(caller, s.id, task)
        if wait:
            try:
                await asyncio.wait_for(s.ready.wait(), wait_timeout)
            except asyncio.TimeoutError:
                pass
        return self.status(s.id)

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
                            # Managed sessions receive messages as prompts, not via inbox.
                            {"name": "HB_PIGGYBACK", "value": "0"},
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
        assert s.queue is not None and s.acp is not None and s.acp_session_id is not None
        while True:
            msg = await s.queue.get()
            if msg is None or not s.live:
                return
            sender = self.sessions.get(msg.from_id)
            assert s.profile is not None
            rt = s.profile.runtime
            text = msg.body if rt.message_format == "body" else format_injection(msg, sender)
            self._set_state(s, "busy", message_id=msg.id)
            self.store.mark_delivered(msg.id)
            self.store.record(
                "message.inject",
                session_id=s.id,
                thread_id=msg.thread_id,
                message_id=msg.id,
                via="acp",
                text=text,
            )
            s.current = msg
            try:
                if rt.session_mode == "per_message" and s.turns > 0:
                    await self._fresh_conversation(s)
                s.turns += 1
                stop_reason, reply = await s.acp.prompt(s.acp_session_id, text)
            except (ConnectionClosed, RpcError) as e:
                self.store.record("session.error", session_id=s.id, stage="prompt", error=repr(e))
                await self._finalize(s, "failed", f"prompt failed: {e}")
                return
            s.current = None
            s.last_reply = reply
            self.store.record(
                "turn.end",
                session_id=s.id,
                thread_id=msg.thread_id,
                message_id=msg.id,
                stop_reason=stop_reason,
                reply=reply,
            )
            if not s.live:
                return
            self._set_state(s, "idle")
            if msg.auto_reply and sender and sender.live and sender.id != s.id:
                await self._post(
                    s, sender, reply or f"(no text reply; stopReason={stop_reason})",
                    thread_id=msg.thread_id, reply_to=msg.id, idempotency_key=None,
                    auto_reply=False, auto=True,
                )

    # --- messaging ----------------------------------------------------------
    async def send(
        self,
        caller: Session,
        to: str,
        body: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
        idempotency_key: str | None = None,
        auto_reply: bool = True,
        wait: float = 0,
    ) -> dict[str, Any]:
        """Post a message. With wait > 0 (and auto_reply), block up to `wait`
        seconds for the target's reply and return it; the reply is then not
        delivered again through the inbox or as a prompt."""
        target = self.resolve(to)
        if idempotency_key:
            existing = self.store.find_idempotent(caller.id, idempotency_key)
            if existing:
                return {"message_id": existing["id"], "thread_id": existing["thread_id"], "duplicate": True}
        wait = max(0.0, min(float(wait or 0), 3600.0))
        if not wait or not auto_reply or target.kind != "managed":
            return await self._post(caller, target, body, thread_id, reply_to, idempotency_key, auto_reply)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        result = await self._post(
            caller, target, body, thread_id, reply_to, idempotency_key, auto_reply, waiter=fut
        )
        try:
            result["reply"] = await asyncio.wait_for(asyncio.shield(fut), wait)
            result["timed_out"] = False
        except asyncio.TimeoutError:
            self._waiters.pop(result["message_id"], None)
            if fut.done():  # resolved right at the deadline
                result["reply"], result["timed_out"] = fut.result(), False
            else:
                fut.cancel()
                result["reply"], result["timed_out"] = None, True
        return result

    async def _post(
        self,
        sender: Session,
        target: Session,
        body: str,
        thread_id: str | None,
        reply_to: str | None,
        idempotency_key: str | None,
        auto_reply: bool,
        auto: bool = False,
        waiter: asyncio.Future[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not thread_id and reply_to:
            original = self.store.message(reply_to)
            thread_id = original["thread_id"] if original else None
        thread_id = thread_id or _id("t")
        if target.kind == "managed":
            delivery = "acp"
        elif target.meta.get("channel"):
            delivery = "channel"
        else:
            delivery = "inbox"
        msg = Message(
            id=_id("m"),
            from_id=sender.id,
            to_id=target.id,
            thread_id=thread_id,
            body=body,
            reply_to=reply_to,
            auto_reply=auto_reply and target.kind == "managed",
            ts=now(),
        )
        self.store.insert_message(
            {
                "id": msg.id,
                "ts": msg.ts,
                "from_id": msg.from_id,
                "to_id": msg.to_id,
                "thread_id": msg.thread_id,
                "reply_to": msg.reply_to,
                "body": msg.body,
                "idempotency_key": idempotency_key,
                "delivery": delivery,
            }
        )
        self.store.record(
            "message.send",
            session_id=sender.id,
            thread_id=thread_id,
            message_id=msg.id,
            to=target.id,
            body=body,
            reply_to=reply_to,
            delivery=delivery,
            auto_reply=msg.auto_reply,
            automatic=auto,
        )
        if waiter is not None:
            self._waiters[msg.id] = waiter
        queued_ahead = None
        pending = self._waiters.pop(reply_to, None) if (auto and reply_to) else None
        if pending is not None and not pending.done():
            # The sender is blocked in send(wait=...): hand the reply over directly.
            self.store.mark_delivered(msg.id)
            self.store.record("message.deliver", session_id=target.id, thread_id=thread_id, message_id=msg.id, via="wait")
            pending.set_result(
                {
                    "message_id": msg.id,
                    "from": sender.id,
                    "from_name": sender.name,
                    "thread_id": thread_id,
                    "reply_to": reply_to,
                    "body": body,
                    "sent_at": msg.ts,
                }
            )
        elif target.kind == "managed":
            assert target.queue is not None
            queued_ahead = target.queue.qsize() + (1 if target.state == "busy" else 0)
            await target.queue.put(msg)
        else:
            target.inbox_event.set()
        return {
            "message_id": msg.id,
            "thread_id": thread_id,
            "to": target.id,
            "delivery": delivery,
            "queued_ahead": queued_ahead,
        }

    async def inbox(self, s: Session, wait: float = 0) -> list[dict[str, Any]]:
        if s.kind == "managed":
            raise BrokerError("managed sessions receive messages as prompts; inbox is for external sessions")
        wait = max(0.0, min(float(wait or 0), 600.0))
        msgs = self.store.undelivered(s.id)
        if not msgs and wait:
            s.inbox_event.clear()
            msgs = self.store.undelivered(s.id)
            if not msgs:
                try:
                    await asyncio.wait_for(s.inbox_event.wait(), wait)
                except asyncio.TimeoutError:
                    pass
                msgs = self.store.undelivered(s.id)
        out = []
        for m in msgs:
            self.store.mark_delivered(m["id"])
            sender = self.sessions.get(m["from_id"])
            self.store.record(
                "message.deliver",
                session_id=s.id,
                thread_id=m["thread_id"],
                message_id=m["id"],
                via=m["delivery"],
            )
            out.append(
                {
                    "message_id": m["id"],
                    "from": m["from_id"],
                    "from_name": sender.name if sender else None,
                    "thread_id": m["thread_id"],
                    "reply_to": m["reply_to"],
                    "body": m["body"],
                    "sent_at": m["ts"],
                }
            )
        return out

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
                "queued": s.queue.qsize() if s.queue else None,
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
            self.store.record("session.end", session_id=s.id, state="terminated", reason=s.end_reason)
        return self.status(s.id)

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
        unanswered: list[Message] = [s.current] if s.current else []
        if s.queue is not None:
            while not s.queue.empty():
                m = s.queue.get_nowait()
                if m is not None:
                    unanswered.append(m)
        for m in unanswered:
            self.store.record("message.dropped", session_id=s.id, thread_id=m.thread_id, message_id=m.id, reason=reason)
            sender = self.sessions.get(m.from_id)
            if m.auto_reply and sender and sender.live and sender.id != s.id:
                # Whoever is waiting for an answer learns that none is coming.
                await self._post(
                    s, sender, f"(session {s.name} ({s.id}) ended before answering: {reason})",
                    thread_id=m.thread_id, reply_to=m.id, idempotency_key=None, auto_reply=False, auto=True,
                )
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
            s.finalized.set()

    async def shutdown(self) -> None:
        live = [s for s in self.sessions.values() if s.kind == "managed" and s.live]
        await asyncio.gather(*(self._finalize(s, "terminated", "broker shutdown") for s in live), return_exceptions=True)

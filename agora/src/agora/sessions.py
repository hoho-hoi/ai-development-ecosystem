"""People (sessions) living in one place: birth, mail, cloning, death.

A place runs on one machine. Every person living here has a key pair (its id
is derived from the public key), a mailbox and an address `id@place`. Places
are equal and talk to each other directly (federation.py); there is no
center.

States of a managed person:
    preparing → starting → idle ⇄ busy → terminated
                    └──────────┴────────→ failed
External people (MCP clients that joined with the broker key) are "active" until they
unregister or are terminated. The operator (the place key holder, e.g. the
`agora` CLI) is a permanent person named "operator".

Communication has two primitives, send and receive, and nothing else. Every
person has one mailbox: the message table, kept by the place they live in.
A message stays unread until it is taken in one of two ways:

  * receive: the recipient takes matching messages, waiting a bounded time.
  * turn end (managed people only): when a turn ends, the place takes the
    oldest unread message and prompts the person with it. Ending a turn is
    an unbounded receive.

What an agent writes at the end of a turn is recorded and delivered to no
one; whatever should reach someone is sent with send. A place never writes
messages itself. Every message is signed by its sender's key.

receive(from=X) returns at once, empty, when X can send nothing more: X has
ended, or X is a managed person that is idle with nothing unread. For X in
another place, that place is asked. That is how a waiting person learns that
X finished its turn without writing to it.

Results of the other tools carry a notice of unread messages (count and
senders); the content is read with receive.

Thoughts are open: anyone may read anyone's history, and wait for more.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .acp_client import AcpClient
from .config import BrokerConfig
from .federation import Federation, PlaceError
from .harness import (
    BRIDGE_SERVER,
    CREDENTIAL_FILES,
    harness_model_id,
    harness_record_dir,
    prepare_harness_config,
    resolve_definition,
)
from .identity import OPERATOR, Key, message_bytes, session_id_for, split_address, verify
from .jsonrpc import ConnectionClosed, RpcError
from .logins import LoginError, load_chatgpt_login, write_access_only
from .modules import ModuleCache
from .gateway import HARNESS_ENV, Gateway
from .modules import check_relative
from .spec import BROKER_TOOLS, Catalog, Spec, Workspace
from .telemetry import Telemetry
from .runner import RunHandle, Runner
from .store import StateStore, now
from .workspace import prepare_workspace

log = logging.getLogger(__name__)

LIVE = {"preparing", "starting", "idle", "busy", "active"}
# Seconds to wait before handing a message to another place again.
REMOTE_RETRY_DELAYS = (0.5, 2.0)
SESSION_ID_RE = re.compile(r"^(s-[0-9a-f]{16}|operator)$")


class BrokerError(Exception):
    status = 400


class NotFound(BrokerError):
    status = 404


class Forbidden(BrokerError):
    status = 403


class Unreachable(BrokerError):
    status = 502


def _rand(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Session:
    id: str
    name: str
    kind: str  # managed | external | operator | remote (a caller from another place)
    harness: str | None
    state: str
    depth: int
    spawn_budget: int
    token: str
    created_at: str
    parent_id: str | None = None  # address of whoever made it
    spec: Spec | None = None
    ended_at: str | None = None
    end_reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    key: Key | None = None
    # managed runtime
    handle: RunHandle | None = None
    acp: AcpClient | None = None
    acp_session_id: str | None = None
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    llm_key: str | None = None  # this session's virtual key at the LLM gateway
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    finalized: asyncio.Event = field(default_factory=asyncio.Event)
    last_text: str | None = None  # what the agent wrote in its last turn (delivered to no one)
    turns: int = 0
    usage: dict[str, Any] | None = None
    tools: frozenset[str] | None = None  # tools this person may call; None = all
    # set whenever a message is put in this mailbox, and whenever someone this
    # person is waiting on (receive from=) goes quiet
    mail: asyncio.Event = field(default_factory=asyncio.Event)
    watching: dict[str, int] = field(default_factory=dict)  # session id -> pending receive(from=) calls

    @property
    def live(self) -> bool:
        return self.state in LIVE

    def row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "harness": self.harness,
            "module_id": self.spec.id if self.spec else None,
            "module_version": self.spec.version if self.spec else None,
            "state": self.state,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "token_hash": token_hash(self.token),
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "meta": {**self.meta, "public_key": self.key.public_hex if self.key else None},
        }


def format_injection(msg: dict[str, Any]) -> str:
    """The exact text a managed person receives for a message: a short header
    (who sent it, the ids needed to reply), a blank line, the body."""
    addr = msg["from_id"]
    ref, _ = split_address(addr)
    name = msg.get("from_name")
    who = f"{name} ({addr})" if name and name != ref else addr
    lines = [f"from: {who}", f"message_id: {msg['id']}", f"thread_id: {msg['thread_id']}"]
    if msg["reply_to"]:
        lines.append(f"in_reply_to: {msg['reply_to']}")
    return "\n".join(lines) + "\n\n" + msg["body"]


def public_message(row: dict[str, Any]) -> dict[str, Any]:
    """A message as receive returns it."""
    return {
        "message_id": row["id"],
        "from": row["from_id"],
        "from_name": row.get("from_name"),
        "thread_id": row["thread_id"],
        "reply_to": row["reply_to"],
        "body": row["body"],
        "sent_at": row["ts"],
    }


class SessionManager:
    def __init__(
        self,
        cfg: BrokerConfig,
        store: StateStore,
        catalog: Catalog,
        runner: Runner,
        federation: Federation,
        telemetry: Telemetry | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.telemetry = telemetry or Telemetry()
        gw = cfg.llm_gateway
        self.gateway = Gateway(gw.url, gw.session_url or gw.url, gw.master_key_env) if gw else None
        self.catalog = catalog
        self.runner = runner
        self.fed = federation
        self.place = federation.name
        self.sessions: dict[str, Session] = {}
        self._by_token: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.modules = ModuleCache(cfg.state_dir / "modules")
        self._state_changed = asyncio.Event()  # replaced on every state change
        # The operator signs with the place's own key: the place vouches for it.
        self.operator = self._add(
            Session(
                id=OPERATOR,
                name=OPERATOR,
                kind="operator",
                harness=None,
                state="active",
                depth=0,
                spawn_budget=cfg.max_spawn_depth,
                token=secrets.token_urlsafe(24),  # never used; the place key authenticates the operator
                created_at=now(),
                key=federation.key,
            )
        )

    # --- addresses -----------------------------------------------------------
    def address(self, s: Session) -> str:
        # Callers from other places carry their full address as their id.
        return s.id if "@" in s.id else f"{s.id}@{self.place}"

    def split(self, ref: str) -> tuple[str, str | None]:
        """(ref, None) for here, (ref, place) for another place."""
        try:
            r, place = split_address(ref)
        except ValueError as e:
            raise BrokerError(str(e)) from None
        return (r, None) if place in (None, self.place) else (r, place)

    def summary(self, s: Session) -> dict[str, Any]:
        return {
            "id": s.id,
            "address": self.address(s),
            "place": self.place,
            "name": s.name,
            "kind": s.kind,
            "harness": s.harness,
            "module": f"{s.spec.id}@{s.spec.version}" if s.spec else None,
            "state": s.state,
            "parent": s.parent_id,
            "cloned_from": s.meta.get("cloned_from"),
            "depth": s.depth,
            "created_at": s.created_at,
            "ended_at": s.ended_at,
            "end_reason": s.end_reason,
        }

    # --- bookkeeping --------------------------------------------------------
    def _add(self, s: Session) -> Session:
        self.sessions[s.id] = s
        self._by_token[token_hash(s.token)] = s.id
        self.store.add_secret(f"token:{s.id}", s.token)
        self.store.upsert_session(s.row())
        return s

    def _new_person(self, **fields: Any) -> Session:
        key = Key.generate()
        return Session(id=session_id_for(key.public), key=key, token=secrets.token_urlsafe(32), created_at=now(), **fields)

    def _set_state(self, s: Session, state: str, **extra: Any) -> None:
        old, s.state = s.state, state
        self.store.upsert_session(s.row())
        self.store.record("session.state", session_id=s.id, old=old, new=state, **extra)
        if state in ("idle", "failed", "terminated"):
            s.ready.set()
            self._wake_watchers(s)
        changed, self._state_changed = self._state_changed, asyncio.Event()
        changed.set()

    def _wake_watchers(self, s: Session) -> None:
        for other in self.sessions.values():
            if other.watching.get(s.id):
                other.mail.set()

    def quiet(self, s: Session) -> bool:
        """s can send nothing more for now: it has ended, or it is a managed
        person that is idle with nothing unread to start a turn."""
        if not s.live:
            return True
        return s.kind == "managed" and s.state == "idle" and not self.store.unread(s.id, limit=1)

    def by_token(self, token: str) -> Session:
        sid = self._by_token.get(token_hash(token))
        s = self.sessions.get(sid) if sid else None
        if not s or not s.live:
            raise Forbidden("unknown or expired session token")
        return s

    def resolve(self, ref: str, live_only: bool = True) -> Session:
        ref, place = self.split(ref)
        if place:
            raise BrokerError(f"{ref}@{place} lives in another place")
        if ref in self.sessions and (self.sessions[ref].live or not live_only):
            return self.sessions[ref]
        matches = [s for s in self.sessions.values() if s.name == ref and (s.live or not live_only)]
        if not matches:
            raise NotFound(f"no {'live ' if live_only else ''}session {ref!r} in {self.place}")
        return max(matches, key=lambda s: s.created_at)

    def _unique_name(self, wanted: str) -> str:
        wanted = wanted.replace("@", "-")  # "@" separates a name from its place
        taken = {s.name for s in self.sessions.values() if s.live}
        if wanted not in taken:
            return wanted
        n = 2
        while f"{wanted}-{n}" in taken:
            n += 1
        return f"{wanted}-{n}"

    # --- callers from other places ------------------------------------------------
    @staticmethod
    def role(caller: Session) -> str:
        """What a caller counts as for spawnableBy: operator, external or a spec id."""
        if caller.kind == "operator":
            return "operator"
        if caller.kind == "remote":
            return str(caller.meta.get("role") or "")
        if caller.kind == "external":
            return "external"
        return caller.spec.id if caller.spec else ""

    def caller_info(self, caller: Session) -> dict[str, Any]:
        return {
            "address": self.address(caller),
            "name": caller.name,
            "role": self.role(caller),
            "operator": caller.kind == "operator",
            "depth": caller.depth,
            "budget": caller.spawn_budget,
        }

    def remote_caller(self, info: dict[str, Any], place: str) -> Session:
        """Someone at a trusted place, as that place vouches for them."""
        address = str(info.get("address") or "")
        if split_address(address)[1] != place:
            raise Forbidden(f"place {place} may only speak for its own residents, not {address!r}")
        return Session(
            id=address,
            name=str(info.get("name") or address),
            kind="operator" if info.get("operator") else "remote",
            harness=None,
            state="active",
            depth=int(info.get("depth") or 0),
            spawn_budget=int(info.get("budget") or 0),
            token="",
            created_at=now(),
            meta={"role": info.get("role"), "place": place},
        )

    async def forward(self, caller: Session, place: str, tool: str, args: dict[str, Any], timeout: float = 120) -> Any:
        """Run a tool at another place on behalf of a caller here."""
        try:
            return await self.fed.post(
                place, "/federation/call", {"caller": self.caller_info(caller), "tool": tool, "args": args}, timeout
            )
        except PlaceError as e:
            err = NotFound(str(e)) if e.status == 404 else Forbidden(str(e)) if e.status == 403 else Unreachable(str(e))
            raise err from None

    # --- external people --------------------------------------------------------
    def register_external(self, name: str, harness: str | None, cwd: str | None, channel: bool) -> Session:
        s = self._add(
            self._new_person(
                name=self._unique_name(name or "external"),
                kind="external",
                harness=harness,
                state="active",
                depth=0,
                spawn_budget=self.cfg.max_spawn_depth,
                meta={"cwd": cwd, "channel": channel},
            )
        )
        self.store.record("session.register", session_id=s.id, name=s.name, harness=harness, cwd=cwd, channel=channel)
        return s

    # --- birth: spawn and clone ------------------------------------------------
    def _check_spawn_policy(self, caller: Session, spec: Spec) -> None:
        if caller.kind == "operator":
            return
        allowed = spec.spawnable_by
        who = self.role(caller)
        if "*" not in allowed and who not in allowed:
            raise Forbidden(f"{caller.name} ({who}) may not spawn spec {spec.id!r}; spawnableBy={allowed}")
        if caller.spawn_budget < 1:
            raise Forbidden(f"{caller.name} has no spawn depth left (depth {caller.depth})")

    def _check_capacity(self, spec: Spec) -> None:
        live = [s for s in self.sessions.values() if s.kind == "managed" and s.live]
        if len(live) >= self.cfg.max_sessions:
            raise Forbidden(f"place limit reached: {self.cfg.max_sessions} live managed sessions")
        same = [s for s in live if s.spec and s.spec.id == spec.id]
        if len(same) >= spec.limits.max_concurrent:
            raise Forbidden(f"spec {spec.id!r} limit reached: {spec.limits.max_concurrent} live sessions")

    async def _born(self, caller: Session, spec: Spec, name: str | None, meta: dict[str, Any]) -> Session:
        async with self._lock:
            self._check_spawn_policy(caller, spec)
            self._check_capacity(spec)
            return self._add(
                self._new_person(
                    name=self._unique_name(name or spec.id),
                    kind="managed",
                    harness=spec.runtime.harness,
                    state="preparing",
                    depth=caller.depth + 1,
                    spawn_budget=min(caller.spawn_budget - 1, spec.limits.max_spawn_depth),
                    parent_id=self.address(caller),
                    spec=spec,
                    meta=meta,
                    tools=frozenset(spec.runtime.broker_tools),
                )
            )

    async def _wait_ready(self, s: Session, wait: bool, wait_timeout: float) -> None:
        if wait:
            try:
                await asyncio.wait_for(s.ready.wait(), wait_timeout)
            except asyncio.TimeoutError:
                pass

    async def spawn(
        self,
        caller: Session,
        module: str,
        task: str | None = None,
        workspace: dict[str, Any] | None = None,
        name: str | None = None,
        wait: bool = True,
        wait_timeout: float = 600,
        place: str | None = None,
        module_ref: str | None = None,
        module_path: str | None = None,
        module_workspace_path: str | None = None,
    ) -> dict[str, Any]:
        if module_workspace_path and (module or module_ref or module_path or (place and place != self.place)):
            raise BrokerError("module_workspace_path names the module by itself: no module, module_ref, "
                              "module_path or place with it")
        if not module and not module_workspace_path:
            raise BrokerError("spawn needs a module (or module_workspace_path)")
        if place and place != self.place:
            # Born elsewhere; the task is then an ordinary signed message from the caller.
            args = {"module": module, "workspace": workspace, "name": name, "wait": wait,
                    "wait_timeout": wait_timeout, "module_ref": module_ref, "module_path": module_path}
            st = await self.forward(caller, place, "spawn", {k: v for k, v in args.items() if v is not None},
                                    timeout=wait_timeout + 60)
            task_message_id = None
            if task:
                task_message_id = (await self.send(caller, st["address"], task))["message_id"]
            return {**st, "task_message_id": task_message_id}
        try:
            ws = Workspace.model_validate(workspace or {})
        except Exception as e:
            raise BrokerError(f"bad workspace: {e}") from None
        try:
            if module_workspace_path:
                spec = await self._workspace_module(caller, module_workspace_path, ws)
            else:
                spec = await asyncio.to_thread(self.catalog.get, module, module_ref=module_ref,
                                               module_path=module_path, workspace=ws)
        except KeyError as e:
            raise NotFound(str(e)) from None
        except ValueError as e:  # SpecError, ModuleError
            raise BrokerError(str(e)) from None
        s = await self._born(caller, spec, name, {"ref": ws.ref})
        self.store.record(
            "session.spawn",
            session_id=s.id,
            caller=self.address(caller),
            spec=spec.resolved_dump(),
            fingerprint=spec.fingerprint,
            task=task,
            module_ref_override=module_ref,
            module_path_override=module_path,
            module_workspace_path=module_workspace_path,
            runner=self.runner.name,
            public_key=s.key.public_hex if s.key else None,
        )
        self._trace_start(s, caller)
        s.tasks.append(asyncio.create_task(self._launch(s)))
        task_message_id = None
        if task:
            task_message_id = (await self.send(caller, s.id, task))["message_id"]
        await self._wait_ready(s, wait, wait_timeout)
        return {**self.status(s.id), "task_message_id": task_message_id}

    async def _workspace_module(self, caller: Session, path: str, ws: Workspace) -> Spec:
        """A custom module: the module at `path` in the caller's own workspace,
        as it is now (e.g. one the caller is writing). The caller must be a
        session with a workspace; the copy runs like any module."""
        if caller.kind != "managed" or caller.handle is None:
            raise BrokerError("module_workspace_path needs a session with a workspace")
        check_relative(path, "module_workspace_path")
        dest = self.cfg.state_dir / "custom-modules" / _rand("w")
        await self.runner.snapshot_workspace(caller.handle, dest, subpath=path)
        origin = {"workspace_of": self.address(caller), "path": path}
        return await asyncio.to_thread(self.catalog.from_directory, dest, origin, workspace=ws)

    def _trace_start(self, s: Session, caller: Session) -> None:
        assert s.spec is not None
        rt = s.spec.runtime
        self.telemetry.session_start(s.id, caller.id, {
            "gen_ai.agent.name": s.spec.id, "gen_ai.agent.id": self.address(s), "gen_ai.request.model": rt.model,
            "agora.module.version": s.spec.version, "agora.fingerprint": s.spec.fingerprint,
            "agora.harness": f"{rt.harness}@{rt.version}", "agora.parent": s.parent_id, "agora.place": self.place,
        })

    async def clone(
        self, caller: Session, ref: str, name: str | None = None, wait: bool = True, wait_timeout: float = 600
    ) -> dict[str, Any]:
        """A new person with the source's memories so far: its workspace as it
        is now and its harness's conversation records, resumed in a new body
        (ACP session/load). New key, address and mailbox."""
        src = self.resolve(ref)
        if src.kind != "managed" or not src.spec or not src.handle:
            raise BrokerError(f"{src.name} has no body to clone (state {src.state})")
        spec = src.spec
        s = await self._born(
            caller, spec, name or f"{src.name}-clone",
            {"ref": src.meta.get("ref"), "cloned_from": self.address(src)},
        )
        bundle = self.cfg.state_dir / "bundles" / s.id
        seed = bundle.parent / f"{s.id}.seed"
        try:
            bundle.mkdir(parents=True)
            await self.runner.snapshot_workspace(src.handle, bundle / "workspace")
            await self.runner.collect(src.handle, harness_record_dir(spec), seed)
            for cred in CREDENTIAL_FILES.get(spec.runtime.harness, ()):
                for f in seed.rglob(cred) if seed.exists() else ():
                    f.unlink()
        except Exception as e:
            self.store.record("session.error", session_id=s.id, stage="clone", error=repr(e))
            await self._finalize(s, "failed", f"clone failed: {e}")
            return self.status(s.id)
        load = src.acp_session_id if spec.runtime.session_mode == "persistent" else None
        self.store.record(
            "session.clone",
            session_id=s.id,
            caller=self.address(caller),
            source=self.address(src),
            source_state=src.state,
            source_conversation=src.acp_session_id,
            fingerprint=spec.fingerprint,
            public_key=s.key.public_hex if s.key else None,
        )
        self._trace_start(s, caller)
        s.tasks.append(asyncio.create_task(self._launch(s, seed=seed, load=load)))
        await self._wait_ready(s, wait, wait_timeout)
        return self.status(s.id)

    async def _launch(self, s: Session, seed: Path | None = None, load: str | None = None) -> None:
        """Make the body: workspace, harness config, container, ACP. A clone
        brings its workspace already in the bundle and its harness records in
        `seed`, and resumes the conversation `load`."""
        assert s.spec is not None
        spec = s.spec
        bundle = self.cfg.state_dir / "bundles" / s.id
        stderr_path = self.cfg.history_dir / "sessions" / s.id / "agent.stderr.log"
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if seed is None:
                bundle.mkdir(parents=True)
                manifest = await asyncio.to_thread(prepare_workspace, spec.workspace, bundle / "workspace")
                self.store.record("session.workspace", session_id=s.id, **manifest)
            else:
                self.store.record("session.workspace", session_id=s.id, cloned_from=s.meta.get("cloned_from"))
            ws_path, agora_path = self.runner.runtime_paths(bundle)
            (bundle / "agora").mkdir()
            definition = resolve_definition(spec)
            module_mcp: list[dict[str, Any]] = []
            if definition.module:
                assert definition.manifest is not None
                self.store.record("session.module", session_id=s.id, **definition.manifest)
                # The whole module goes into the session, so its MCP servers can run from it.
                await asyncio.to_thread(
                    shutil.copytree, definition.module.directory, bundle / "agora" / "module", symlinks=True
                )
                module_mcp = definition.module.mcp_servers(f"{agora_path}/module")
            env = prepare_harness_config(
                spec, bundle / "agora", agora_path, definition.instructions, definition.skill_dirs
            )
            if seed is not None and seed.exists():
                # The source's records, but this body's own config (instructions, settings, skills).
                target = bundle / "agora" / harness_record_dir(spec)
                fresh = set(os.listdir(target))
                shutil.copytree(
                    seed, target, dirs_exist_ok=True, symlinks=True,
                    ignore=lambda d, names: [n for n in names if Path(d) == seed and n in fresh],
                )
                shutil.rmtree(seed, ignore_errors=True)
            if spec.runtime.login == "chatgpt":
                login = load_chatgpt_login(self.cfg.codex_login_path)
                login.check_fresh(spec.limits.max_runtime_minutes * 60)
                for name, value in login.secrets.items():
                    self.store.add_secret(name, value)
                write_access_only(login, bundle / "agora" / "codex")
                self.store.record("session.login", session_id=s.id, **login.summary())
            self.store.record("session.harness_env", session_id=s.id, env=env, secrets=spec.runtime.secret_env)
            for name, value in spec.runtime.secret_values().items():
                self.store.add_secret(name, value)
            if s.state != "preparing":  # terminated while preparing
                return
            self._set_state(s, "starting")
            extra_secrets: dict[str, str] = {}
            if self.gateway and spec.runtime.harness in HARNESS_ENV:
                url_var, key_var = HARNESS_ENV[spec.runtime.harness]
                meta = {"agora_session": self.address(s), "module": spec.id, "module_version": spec.version,
                        "fingerprint": spec.fingerprint, "place": self.place}
                s.llm_key = await asyncio.to_thread(self.gateway.mint, s.id, meta)
                self.store.add_secret("llm_key", s.llm_key)
                env[url_var] = self.gateway.session_url
                extra_secrets[key_var] = s.llm_key
                self.store.record("session.llm_key", session_id=s.id, gateway=self.gateway.session_url, metadata=meta)
            if spec.network.proxy:  # the place and the gateway are reached directly, not through the proxy
                direct = ["host.docker.internal", urllib.parse.urlsplit(self.runner.broker_url).hostname or ""]
                if self.gateway and spec.runtime.harness in HARNESS_ENV:
                    direct.append(urllib.parse.urlsplit(self.gateway.session_url).hostname or "")
                env["NO_PROXY"] = env["no_proxy"] = ",".join(h for h in direct if h)
            s.handle = await self.runner.start(s.id, spec, bundle, env, stderr_path, secrets=extra_secrets)
            self.store.record("session.process", session_id=s.id, **s.handle.info)
            s.tasks.append(asyncio.create_task(self._watch_process(s)))

            def on_update(params: dict[str, Any]) -> None:
                upd = params.get("update") or {}
                if upd.get("sessionUpdate") == "usage_update":
                    s.usage = upd

            def on_permission(params: dict[str, Any], outcome: dict[str, Any]) -> None:
                self.store.record("acp.permission", session_id=s.id, request=params, outcome=outcome)

            s.acp = AcpClient(
                s.handle.reader,
                s.handle.writer,
                permissions=spec.runtime.permissions,
                on_update=on_update,
                on_permission=on_permission,
            )
            await s.acp.initialize()
            mcp_servers: list[dict[str, Any]] = []
            if spec.runtime.broker_tools:
                caps = (s.acp.agent_info.get("agentCapabilities") or {}).get("mcpCapabilities") or {}
                if not caps.get("http"):
                    raise BrokerError("this harness cannot reach agora: its ACP adapter takes no HTTP MCP servers")
                # agora's tools: this place's MCP endpoint, with the session's token.
                # Which tools the session gets is decided by the place, from the token.
                mcp_servers.append(
                    {
                        "type": "http",
                        "name": BRIDGE_SERVER,
                        "url": f"{s.handle.broker_url}/mcp",
                        "headers": [{"name": "Authorization", "value": f"Bearer {s.token}"}],
                    }
                )
            names = [m["name"] for m in mcp_servers]
            clash = sorted({m["name"] for m in module_mcp} & set(names))
            if clash:
                raise BrokerError(f"the module's MCP servers clash with agora's: {', '.join(clash)}")
            mcp_servers += module_mcp
            s.mcp_servers = mcp_servers
            if load:
                if not (s.acp.agent_info.get("agentCapabilities") or {}).get("loadSession"):
                    raise BrokerError("this harness cannot resume a conversation (no ACP loadSession); clone needs it")
                await s.acp.load_session(load, s.handle.workspace_path, mcp_servers)
                s.acp_session_id = load
            else:
                s.acp_session_id, choice = await s.acp.new_session(
                    s.handle.workspace_path, mcp_servers, model=harness_model_id(spec)
                )
                if choice:
                    self.store.record("session.model", session_id=s.id, **choice)
            if s.state != "starting":
                return
            self._set_state(s, "idle", acp_session_id=s.acp_session_id)
            s.tasks.append(asyncio.create_task(self._worker(s)))
            s.tasks.append(asyncio.create_task(self._deadline(s, spec.limits.max_runtime_minutes * 60)))
        except (LoginError, BrokerError) as e:  # a condition to fix, not a bug
            log.warning("launch of %s refused: %s", s.id, e)
            stage = "login" if isinstance(e, LoginError) else "launch"
            self.store.record("session.error", session_id=s.id, stage=stage, error=str(e))
            await self._finalize(s, "failed", f"launch failed: {e}")
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
        assert s.spec is not None
        s.acp_session_id, _ = await s.acp.new_session(s.handle.workspace_path, s.mcp_servers, model=harness_model_id(s.spec))
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
        assert s.acp is not None and s.acp_session_id is not None and s.spec is not None
        text = format_injection(msg)
        self._set_state(s, "busy", message_id=msg["id"])
        self.store.record(
            "message.inject", session_id=s.id, thread_id=msg["thread_id"], message_id=msg["id"], via="turn", text=text
        )
        try:
            if s.spec.runtime.session_mode == "per_message" and s.turns > 0:
                await self._fresh_conversation(s)
            s.turns += 1
            self.telemetry.turn_start(s.id, text, {"agora.message_id": msg["id"], "agora.thread_id": msg["thread_id"],
                                                   "agora.from": msg.get("from_id") or msg.get("from")})
            # The turn's traceparent goes with the prompt, for the harness to send
            # with its LLM calls so that they nest under this turn.
            tp = self.telemetry.traceparent(s.id)
            stop_reason, agent_text = await s.acp.prompt(s.acp_session_id, text, meta={"traceparent": tp} if tp else None)
            self.telemetry.turn_end(s.id, agent_text, {"agora.stop_reason": stop_reason})
        except (ConnectionClosed, RpcError) as e:
            self.store.record("session.error", session_id=s.id, stage="prompt", error=repr(e))
            await self._finalize(s, "failed", f"prompt failed: {e}")
            return False
        s.last_text = agent_text
        # What the agent wrote is recorded only; it reaches no one.
        self.store.record(
            "turn.end",
            session_id=s.id,
            thread_id=msg["thread_id"],
            message_id=msg["id"],
            stop_reason=stop_reason,
            text=agent_text,
        )
        if not s.live:
            return False
        self._set_state(s, "idle")
        return True

    # --- mail -------------------------------------------------------------------
    def _envelope(self, sender: Session, to: str, body: str, thread_id: str, reply_to: str | None) -> dict[str, Any]:
        assert sender.key is not None
        env = {
            "id": _rand("m"),
            "from": self.address(sender),
            "from_name": sender.name,
            "from_key": sender.key.public_hex,
            "to": to,
            "thread_id": thread_id,
            "reply_to": reply_to,
            "body": body,
            "sent_at": now(),
        }
        env["sig"] = sender.key.sign(message_bytes(env))
        return env

    async def send(
        self,
        caller: Session,
        to: str | None,
        body: str,
        thread_id: str | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Put a message in the target's mailbox (here or at another place) and
        return at once. Without `to`, a reply goes to the sender of the message
        it answers. The result says whether it arrived; getting it there is the
        place's job, not the sender's (see _deliver_remote)."""
        if to:
            ref, place = self.split(to)
        elif reply_to:
            original = self.store.message(reply_to)
            if not original or original["to_id"] != caller.id:
                raise BrokerError(f"no message {reply_to!r} addressed to you; give `to` explicitly")
            ref, place = self.split(original["from_id"])
        else:
            raise BrokerError("send needs `to`, or `reply_to` to answer a message")
        if not thread_id and reply_to:
            original = self.store.message(reply_to)
            thread_id = original["thread_id"] if original else None
        thread_id = thread_id or _rand("t")
        if place is None:
            target = self.resolve(ref)
            env = self._envelope(caller, self.address(target), body, thread_id, reply_to)
            result = self._accept(env, target)
            self.store.record(
                "message.send", session_id=caller.id, thread_id=thread_id, message_id=env["id"],
                to=env["to"], body=body, reply_to=reply_to, sig=env["sig"],
            )
            return result
        env = self._envelope(caller, f"{ref}@{place}", body, thread_id, reply_to)
        res = await self._deliver_remote(place, env)
        # Keep our own copy of what we sent (for threads and replies), never unread.
        self.store.insert_message(
            {
                "id": env["id"], "ts": env["sent_at"], "from_id": env["from"], "from_name": caller.name,
                "to_id": res["to"], "thread_id": thread_id, "reply_to": reply_to, "body": body,
                "delivery": "remote", "sig": env["sig"],
            }
        )
        self.store.mark_delivered(env["id"], f"remote:{place}")
        self.store.record(
            "message.send", session_id=caller.id, thread_id=thread_id, message_id=env["id"],
            to=res["to"], body=body, reply_to=reply_to, sig=env["sig"], place=place,
        )
        return {"message_id": env["id"], "thread_id": thread_id, "to": res["to"],
                "to_state": res.get("to_state"), "queued_ahead": res.get("queued_ahead")}

    async def _deliver_remote(self, place: str, env: dict[str, Any]) -> dict[str, Any]:
        """Hand a message to another place. When the connection fails or the
        answer is lost, send the same message (same id) again: the other place
        takes a message id only once, so the sender never resends anything."""
        attempts = len(REMOTE_RETRY_DELAYS) + 1
        for n, delay in enumerate((*REMOTE_RETRY_DELAYS, None), 1):
            try:
                return await self.fed.post(place, "/federation/deliver", {"envelope": env})
            except PlaceError as e:
                if e.status == 404:
                    raise NotFound(str(e)) from None
                if e.status == 403:
                    raise Forbidden(str(e)) from None
                if e.status < 500 or delay is None:
                    note = f" (no answer after {attempts} attempts; it may not have arrived)" if n > 1 else ""
                    raise Unreachable(f"{e}{note}") from None
                self.store.record("message.retry", message_id=env["id"], place=place, attempt=n, error=str(e))
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    def _accept(self, env: dict[str, Any], target: Session) -> dict[str, Any]:
        """Put a (verified) message in a resident's mailbox."""
        ahead = len(self.store.unread(target.id))
        self.store.insert_message(
            {
                "id": env["id"], "ts": env["sent_at"], "from_id": env["from"], "from_name": env.get("from_name"),
                "to_id": target.id, "thread_id": env["thread_id"], "reply_to": env.get("reply_to"),
                "body": env["body"], "delivery": "mailbox", "sig": env["sig"],
            }
        )
        target.mail.set()
        return {
            "message_id": env["id"],
            "thread_id": env["thread_id"],
            "to": self.address(target),
            "to_state": target.state,
            "queued_ahead": ahead,
        }

    def deliver(self, env: dict[str, Any], from_place: str) -> dict[str, Any]:
        """A message from another place. Who sent it is checked by its signature,
        not by the delivering place's word."""
        sender = str(env.get("from") or "")
        ref, place = split_address(sender)
        if place != from_place:
            raise Forbidden(f"place {from_place} delivered a message from {sender!r}, who does not live there")
        if ref == OPERATOR:
            key_hex = self.fed.peer(from_place).public_key  # the operator signs with its place's key
        else:
            key_hex = str(env.get("from_key") or "")
            try:
                if session_id_for(bytes.fromhex(key_hex)) != ref:
                    raise Forbidden(f"{sender}: public key does not match the id")
            except ValueError:
                raise Forbidden(f"{sender}: bad public key") from None
        if not verify(key_hex, str(env.get("sig") or ""), message_bytes(env)):
            raise Forbidden(f"{sender}: bad message signature")
        to_ref, to_place = self.split(str(env.get("to") or ""))
        if to_place:
            raise NotFound(f"{env.get('to')} does not live in {self.place}")
        target = self.resolve(to_ref)
        if self.store.message(env["id"]):  # delivered before
            return {"message_id": env["id"], "thread_id": env["thread_id"], "to": self.address(target),
                    "to_state": target.state, "duplicate": True}
        result = self._accept(env, target)
        self.store.record(
            "message.arrive", session_id=target.id, thread_id=env["thread_id"], message_id=env["id"],
            sender=sender, place=from_place, body=env["body"], reply_to=env.get("reply_to"), sig=env["sig"],
        )
        return result

    def _take(
        self, s: Session, *, via: str, from_id: str | None = None, reply_to: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Mark matching unread messages delivered and return them (oldest first)."""
        rows = self.store.unread(s.id, from_id=from_id, reply_to=reply_to, limit=limit)
        for m in rows:
            self.store.mark_delivered(m["id"], via)
            self.store.record("message.deliver", session_id=s.id, thread_id=m["thread_id"], message_id=m["id"], via=via)
        return rows

    async def _wait_mail(
        self, s: Session, take: Any, wait: float | None, stop: Any = None
    ) -> list[dict[str, Any]]:
        """Call take() until it returns something, s ends, stop() is true, or
        `wait` seconds pass (None: no limit). take() runs without yielding to
        the event loop, so two waiters never take the same message."""
        loop = asyncio.get_running_loop()
        deadline = None if wait is None else loop.time() + wait
        while True:
            got = take()
            if got or not s.live or (stop is not None and stop()):
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
        to `wait` seconds (max 600) for the first one. With `from`, return at
        once when that person can send nothing more (see quiet())."""
        wait = max(0.0, min(float(wait or 0), 600.0))
        if not from_ref:
            rows = await self._wait_mail(s, lambda: self._take(s, via="receive", reply_to=reply_to), wait)
            return {"messages": [public_message(m) for m in rows], "unread_remaining": len(self.store.unread(s.id))}
        ref, place = self.split(from_ref)
        if place is None:
            src = self.resolve(ref, live_only=False)
            from_addr = self.address(src)

            def take() -> list[dict[str, Any]]:
                return self._take(s, via="receive", from_id=from_addr, reply_to=reply_to)

            s.watching[src.id] = s.watching.get(src.id, 0) + 1
            try:
                rows = await self._wait_mail(s, take, wait, stop=lambda: self.quiet(src))
            finally:
                s.watching[src.id] -= 1
                if not s.watching[src.id]:
                    del s.watching[src.id]
            state, is_quiet = src.state, self.quiet(src)
        else:
            rows, state, is_quiet = await self._receive_from_elsewhere(s, ref, place, reply_to, wait)
        return {
            "messages": [public_message(m) for m in rows],
            "unread_remaining": len(self.store.unread(s.id)),
            "from_state": state,
            "from_quiet": is_quiet,
        }

    async def _receive_from_elsewhere(
        self, s: Session, ref: str, place: str, reply_to: str | None, wait: float
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """receive(from=someone@place): mail is ours; whether they went quiet is
        asked of their place, in the background, while we wait for mail."""
        if not SESSION_ID_RE.match(ref):  # a name: ask that place who it is
            ref = (await self.forward(s, place, "status", {"session": ref}))["id"]
        from_addr = f"{ref}@{place}"
        seen: dict[str, Any] = {"quiet": False, "state": None}

        async def ask_quiet() -> None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait
            while True:
                left = max(0.0, deadline - loop.time())
                try:
                    res = await self.fed.post(place, "/federation/quiet", {"session": ref, "wait": min(left, 25)},
                                              timeout=min(left, 25) + 30)
                except PlaceError:
                    res = {"quiet": True, "state": "unreachable"}  # nothing can come from there now
                seen.update(state=res.get("state"), quiet=bool(res.get("quiet")))
                if seen["quiet"]:
                    s.mail.set()
                    return
                if left <= 0:
                    return

        poller = asyncio.create_task(ask_quiet())
        try:
            rows = await self._wait_mail(
                s, lambda: self._take(s, via="receive", from_id=from_addr, reply_to=reply_to), wait,
                stop=lambda: seen["quiet"],
            )
            if not rows and not poller.done() and wait == 0:
                await asyncio.wait_for(poller, 30)  # a quick look still reports their state
        finally:
            poller.cancel()
        return rows, seen["state"], seen["quiet"]

    async def wait_quiet(self, ref: str, wait: float) -> dict[str, Any]:
        """For another place: wait (bounded) until one of our residents can send
        nothing more."""
        s = self.resolve(ref, live_only=False)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(float(wait or 0), 60.0))
        while not self.quiet(s):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._state_changed.wait(), remaining)
            except asyncio.TimeoutError:
                break
        return {"id": s.id, "quiet": self.quiet(s), "state": s.state}

    def unread_notice(self, s: Session) -> dict[str, Any] | None:
        """Count and senders of s's unread messages, attached to tool results."""
        by = self.store.unread_by_sender(s.id)
        if not by:
            return None
        return {
            "count": sum(n for _, _, n in by),
            "from": [{"session": addr, "name": name, "count": n} for addr, name, n in by],
        }

    # --- queries ------------------------------------------------------------
    def list_sessions(self, module: str | None = None, state: str | None = None, include_ended: bool = False) -> list[dict[str, Any]]:
        out = []
        for s in self.sessions.values():
            if s.kind == "operator":
                continue
            if not include_ended and not s.live:
                continue
            if module and (not s.spec or s.spec.id != module):
                continue
            if state and s.state != state:
                continue
            out.append(self.summary(s))
        return out

    def status(self, ref: str) -> dict[str, Any]:
        s = self.resolve(ref, live_only=False)
        info = self.summary(s)
        info.update(
            {
                "last_text": s.last_text,
                "usage": s.usage,
                "unread": len(self.store.unread(s.id)),
                "acp_session_id": s.acp_session_id,
                "fingerprint": s.spec.fingerprint if s.spec else None,
                "public_key": s.key.public_hex if s.key else None,
            }
        )
        end = s.ended_at or now()
        info["elapsed_seconds"] = round(
            (datetime.fromisoformat(end) - datetime.fromisoformat(s.created_at)).total_seconds(), 1
        )
        return info

    # --- death ------------------------------------------------------------
    async def terminate(self, caller: Session, ref: str, reason: str | None = None) -> dict[str, Any]:
        s = self.resolve(ref, live_only=False)
        if caller.kind != "operator" and self.address(caller) not in (self.address(s), s.parent_id):
            raise Forbidden("only an operator, whoever made it, or the person itself may terminate it")
        if not s.live:
            return self.status(s.id)
        if s.kind == "managed":
            await self._finalize(s, "terminated", reason or f"terminated by {self.address(caller)}")
        else:
            s.ended_at = now()
            s.end_reason = reason or "unregistered"
            self._set_state(s, "terminated")
            await self._drop_mail(s, s.end_reason)
            s.mail.set()  # wake pending receives
            self.store.record("session.end", session_id=s.id, state="terminated", reason=s.end_reason)
        return self.status(s.id)

    async def _drop_mail(self, s: Session, reason: str) -> None:
        """s is ending: its unread messages will never be read. They are
        dropped and recorded; nobody is sent anything."""
        for m in self._take(s, via="dropped"):
            self.store.record("message.dropped", session_id=s.id, thread_id=m["thread_id"], message_id=m["id"], reason=reason)

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
        try:
            if s.acp and prev == "busy" and s.acp_session_id:
                try:
                    await asyncio.wait_for(s.acp.cancel(s.acp_session_id), 2)
                except Exception:
                    pass
            if s.handle:
                await self.runner.stop(s.handle)
                await self.runner.destroy(s.handle)
            elif s.spec is not None and not self.cfg.keep_bundles:
                shutil.rmtree(self.cfg.state_dir / "bundles" / s.id, ignore_errors=True)
            if s.acp:
                await s.acp.close()
            if s.llm_key and self.gateway:
                key, s.llm_key = s.llm_key, None
                await asyncio.to_thread(self.gateway.revoke, key)
        except Exception as e:
            log.exception("cleanup of %s failed", s.id)
            self.store.record("session.error", session_id=s.id, stage="cleanup", error=repr(e))
        finally:
            s.state = prev  # let _set_state log the real transition
            self._set_state(s, state, reason=reason)
            self.store.record("session.end", session_id=s.id, state=state, reason=reason)
            self.telemetry.session_end(s.id, state, reason)
            s.mail.set()
            s.finalized.set()

    async def shutdown(self) -> None:
        live = [s for s in self.sessions.values() if s.kind == "managed" and s.live]
        await asyncio.gather(*(self._finalize(s, "terminated", "place shutdown") for s in live), return_exceptions=True)

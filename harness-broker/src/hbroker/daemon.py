"""The broker daemon: an HTTP API in front of the SessionManager.

The MCP surface is the stdio bridge (bridge.py) that each session runs; the
bridge forwards every tool call here as POST /api/tools/<tool> with the
session's bearer token. The operator authenticates with the broker key.

Every tool response except receive carries an X-HB-Unread header when the
caller has unread messages: JSON {"count": n, "from": [{session, name, count}]}.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import socket
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .config import BrokerConfig, ensure_broker_key
from .modules import ModuleCache
from .profiles import BROKER_TOOLS, ProfileRegistry
from .runner import Runner
from .runner.docker import DockerRunner, network_gateway
from .runner.local import LocalRunner
from .sessions import LIVE, BrokerError, Forbidden, Session, SessionManager
from .store import HistoryStore

log = logging.getLogger(__name__)

# tool name → (allowed argument names, required argument names)
TOOLS: dict[str, tuple[set[str], set[str]]] = {
    "whoami": (set(), set()),
    "list_profiles": (set(), set()),
    "spawn": ({"profile", "task", "ref", "name", "wait", "wait_timeout"}, {"profile"}),
    "send": ({"to", "body", "thread_id", "reply_to", "idempotency_key"}, {"body"}),
    "receive": ({"from", "reply_to", "wait"}, set()),
    "list_sessions": ({"profile", "state", "include_ended"}, set()),
    "status": ({"session"}, {"session"}),
    "terminate": ({"session", "reason"}, {"session"}),
    "history": ({"session", "thread_id", "since", "until", "kinds", "after_seq", "limit"}, set()),
}


assert set(BROKER_TOOLS) | {"whoami"} == set(TOOLS), "profiles.BROKER_TOOLS and daemon.TOOLS disagree"


def make_runner(cfg: BrokerConfig) -> Runner:
    if cfg.runner == "local":
        if not cfg.allow_local_runner:
            raise SystemExit("runner: local gives agents no isolation; set allow_local_runner: true to use it")
        return LocalRunner(cfg.runtime_broker_url(), keep_bundles=cfg.keep_bundles)
    return DockerRunner(
        cfg.runtime_broker_url(),
        network=cfg.docker.network,
        docker=cfg.docker.binary,
        keep_bundles=cfg.keep_bundles,
        owner=hashlib.sha256(str(cfg.state_dir.resolve()).encode()).hexdigest()[:16],
    )


def _bind(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


def make_sockets(cfg: BrokerConfig, runner: Runner) -> list[socket.socket]:
    """Bind the broker's listening sockets and point the runner at them.

    With the docker runner and a loopback listen_host, the broker also listens
    on the session network's gateway address, so containers can reach it
    without exposing the API on every interface."""
    first = _bind(cfg.listen_host, cfg.listen_port)
    cfg.listen_port = first.getsockname()[1]
    socks = [first]
    if cfg.runner == "docker" and not cfg.container_broker_url and cfg.listen_host in ("127.0.0.1", "localhost"):
        gateway = network_gateway(cfg.docker.binary, cfg.docker.network)
        socks.append(_bind(gateway, cfg.listen_port))
        runner.broker_url = f"http://{gateway}:{cfg.listen_port}"
    else:
        runner.broker_url = cfg.runtime_broker_url()
    return socks


class Broker:
    def __init__(self, cfg: BrokerConfig, runner: Runner | None = None):
        self.cfg = cfg
        self.key = ensure_broker_key(cfg)
        self.store = HistoryStore(cfg.history_dir)
        self.profiles = ProfileRegistry(cfg.profiles_dir, ModuleCache(cfg.state_dir / "modules"))
        stale = self.store.mark_stale_sessions(LIVE)
        if stale:
            self.store.record("broker.stale_sessions", sessions=stale)
        self.manager = SessionManager(cfg, self.store, self.profiles, runner or make_runner(cfg))

    def _bearer(self, request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise Forbidden("missing bearer token")
        return auth[7:].strip()

    def _is_operator(self, token: str) -> bool:
        return hmac.compare_digest(token.encode(), self.key.encode())

    def caller(self, request: Request) -> Session:
        token = self._bearer(request)
        if self._is_operator(token):
            return self.manager.operator
        return self.manager.by_token(token)

    async def call_tool(self, caller: Session, tool: str, args: dict[str, Any]) -> Any:
        m = self.manager
        if tool == "whoami":
            return caller.summary()
        if tool == "list_profiles":
            return {"profiles": [p.agent_card() for p in self.profiles.list()], "errors": self.profiles.errors()}
        if tool == "spawn":
            return await m.spawn(
                caller,
                args["profile"],
                task=args.get("task"),
                ref=args.get("ref"),
                name=args.get("name"),
                wait=bool(args.get("wait", True)),
                wait_timeout=float(args.get("wait_timeout", 600)),
            )
        if tool == "send":
            return await m.send(
                caller,
                args.get("to"),
                args["body"],
                thread_id=args.get("thread_id"),
                reply_to=args.get("reply_to"),
                idempotency_key=args.get("idempotency_key"),
            )
        if tool == "receive":
            return await m.receive(
                caller, from_ref=args.get("from"), reply_to=args.get("reply_to"), wait=float(args.get("wait") or 0)
            )
        if tool == "list_sessions":
            return {
                "sessions": m.list_sessions(
                    profile=args.get("profile"), state=args.get("state"), include_ended=bool(args.get("include_ended"))
                )
            }
        if tool == "status":
            return m.status(caller.id if args["session"] == "self" else args["session"])
        if tool == "terminate":
            target = caller.id if args["session"] == "self" else args["session"]
            return await m.terminate(caller, target, args.get("reason"))
        if tool == "history":
            q = {k: v for k, v in args.items() if v is not None}
            q.setdefault("limit", 1000)
            if q.get("session") == "self":
                q["session"] = caller.id
            self._check_history_access(caller, q)
            return {"events": m.history(**q)}
        raise BrokerError(f"unknown tool {tool!r}")

    def _check_history_access(self, caller: Session, q: dict[str, Any]) -> None:
        """The operator reads everything. A session reads its own events,
        those of sessions it spawned, and threads it took part in."""
        if caller.kind == "operator":
            return
        if q.get("session"):
            target = self.manager.resolve(q["session"], live_only=False)
            if target.id != caller.id and target.parent_id != caller.id:
                raise Forbidden("history of other sessions is limited to sessions you spawned")
        elif q.get("thread_id"):
            if not self.store.participates(caller.id, q["thread_id"]):
                raise Forbidden("you did not take part in that thread")
        else:
            q["session"] = caller.id

    # --- HTTP handlers -------------------------------------------------------
    async def health(self, request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__})

    async def register(self, request: Request) -> JSONResponse:
        try:
            if not self._is_operator(self._bearer(request)):
                raise Forbidden("registration requires the broker key")
            body = await request.json()
            s = self.manager.register_external(
                str(body.get("name") or "external"),
                body.get("harness"),
                body.get("cwd"),
                bool(body.get("channel")),
            )
            return JSONResponse({"session_id": s.id, "name": s.name, "token": s.token})
        except BrokerError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)

    async def tool(self, request: Request) -> JSONResponse:
        tool = request.path_params["tool"]
        try:
            caller = self.caller(request)
            if tool not in TOOLS:
                raise BrokerError(f"unknown tool {tool!r}")
            if caller.tools is not None and tool not in caller.tools and tool != "whoami":
                raise Forbidden(f"{tool} is not among this session's tools ({', '.join(sorted(caller.tools)) or 'none'})")
            args = await request.json() if await request.body() else {}
            if not isinstance(args, dict):
                raise BrokerError("arguments must be a JSON object")
            allowed, required = TOOLS[tool]
            unknown = set(args) - allowed
            missing = required - {k for k, v in args.items() if v not in (None, "")}
            if unknown:
                raise BrokerError(f"unknown argument(s) for {tool}: {sorted(unknown)}")
            if missing:
                raise BrokerError(f"missing argument(s) for {tool}: {sorted(missing)}")
            if tool != "receive":  # polling noise; what was received is in message.deliver
                self.store.record("api.call", session_id=caller.id, tool=tool, args=args)
            result = await self.call_tool(caller, tool, args)
            headers = {}
            if tool != "receive":
                notice = self.manager.unread_notice(caller)
                if notice:
                    headers["X-HB-Unread"] = json.dumps(notice)  # ASCII-only, safe as a header
            return JSONResponse(result, headers=headers)
        except BrokerError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)
        except (KeyError, ValueError, TypeError) as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)

    def app(self) -> Starlette:
        @contextlib.asynccontextmanager
        async def lifespan(_: Starlette) -> AsyncIterator[None]:
            removed = await self.manager.runner.cleanup_orphans()
            self.store.record(
                "broker.start",
                version=__version__,
                runner=self.manager.runner.name,
                runtime_broker_url=self.manager.runner.broker_url,
                removed_orphans=removed,
            )
            try:
                yield
            finally:
                await self.manager.shutdown()
                self.store.record("broker.stop")

        return Starlette(
            routes=[
                Route("/api/health", self.health, methods=["GET"]),
                Route("/api/register", self.register, methods=["POST"]),
                Route("/api/tools/{tool}", self.tool, methods=["POST"]),
            ],
            lifespan=lifespan,
        )


async def serve(cfg: BrokerConfig, broker: Broker | None = None) -> None:
    import uvicorn

    broker = broker or Broker(cfg)
    sockets = make_sockets(cfg, broker.manager.runner)
    config = uvicorn.Config(broker.app(), log_level="warning", lifespan="on")
    log.warning(
        "harness-broker listening on %s; sessions reach it at %s",
        ", ".join(f"{s.getsockname()[0]}:{s.getsockname()[1]}" for s in sockets),
        broker.manager.runner.broker_url,
    )
    await uvicorn.Server(config).serve(sockets=sockets)


__all__ = ["Broker", "serve", "TOOLS"]

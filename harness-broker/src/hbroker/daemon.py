"""A place: the daemon that runs on one machine, an HTTP API in front of
the SessionManager.

Residents reach it through the stdio bridge (bridge.py) that each of them
runs; the bridge forwards every tool call here as POST /api/tools/<tool> with
the resident's bearer token. The operator authenticates with the broker key.

Other places reach it at /federation/* with requests signed by their place
key (federation.py). A tool call that names someone or somewhere in another
place is forwarded there, once: places never relay for a third place.

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
from .federation import Federation, Peer
from .identity import load_or_create_key
from .modules import ModuleCache
from .profiles import BROKER_TOOLS, ProfileRegistry
from .runner import Runner
from .runner.docker import DockerRunner, network_gateway
from .runner.local import LocalRunner
from .sessions import LIVE, BrokerError, Forbidden, NotFound, Session, SessionManager
from .store import HistoryStore

log = logging.getLogger(__name__)

# tool name → (allowed argument names, required argument names)
TOOLS: dict[str, tuple[set[str], set[str]]] = {
    "whoami": (set(), set()),
    "list_profiles": ({"place"}, set()),
    "spawn": ({"profile", "task", "ref", "name", "wait", "wait_timeout", "place"}, {"profile"}),
    "clone": ({"session", "name", "wait", "wait_timeout"}, {"session"}),
    "send": ({"to", "body", "thread_id", "reply_to"}, {"body"}),
    "receive": ({"from", "reply_to", "wait"}, set()),
    "list_sessions": ({"profile", "state", "include_ended", "place"}, set()),
    "status": ({"session"}, {"session"}),
    "terminate": ({"session", "reason"}, {"session"}),
    "history": ({"session", "thread_id", "since", "until", "kinds", "after_seq", "limit", "wait"}, set()),
}
# Tools another place may call on behalf of one of its residents. send arrives
# as /federation/deliver, and a mailbox is read only where it is kept.
REMOTE_TOOLS = {"list_profiles", "spawn", "clone", "list_sessions", "status", "terminate", "history"}


assert set(BROKER_TOOLS) | {"whoami"} == set(TOOLS), "profiles.BROKER_TOOLS and daemon.TOOLS disagree"


def target_place(manager: SessionManager, tool: str, args: dict[str, Any]) -> str | None:
    """The other place a tool call is about, or None for here."""
    if tool in ("list_profiles", "list_sessions", "spawn"):
        place = args.get("place")
        return place if place and place != manager.place else None
    if tool in ("status", "terminate", "clone", "history") and args.get("session") not in (None, "", "self"):
        return manager.split(str(args["session"]))[1]
    return None


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
        self.federation = Federation(
            cfg.place,
            load_or_create_key(cfg.place_key_file),
            [Peer(p.name, p.url, p.public_key) for p in cfg.peers],
        )
        self.manager = SessionManager(cfg, self.store, self.profiles, runner or make_runner(cfg), self.federation)

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

    def place_info(self) -> dict[str, Any]:
        return {
            "name": self.federation.name,
            "public_key": self.federation.key.public_hex,
            "url": self.cfg.public_url or self.cfg.local_url,
            "peers": [{"name": p.name, "url": p.url} for p in self.federation.peers.values()],
        }

    async def call_tool(self, caller: Session, tool: str, args: dict[str, Any], forwarded: bool = False) -> Any:
        m = self.manager
        place = target_place(m, tool, args)
        if place:
            if forwarded:
                raise Forbidden(f"{self.federation.name} does not relay calls to a third place ({place})")
            wait = float(args.get("wait_timeout", 600)) if tool in ("spawn", "clone") else float(args.get("wait") or 0)
            if tool == "spawn":  # the task goes as an ordinary signed message from the caller
                return await m.spawn(
                    caller, args["profile"], task=args.get("task"), ref=args.get("ref"), name=args.get("name"),
                    wait=bool(args.get("wait", True)), wait_timeout=wait, place=place,
                )
            return await m.forward(caller, place, tool, args, timeout=wait + 120)
        if tool == "whoami":
            return {**m.summary(caller), "public_key": caller.key.public_hex if caller.key else None}
        if tool == "list_profiles":
            return {
                "place": m.place,
                "places": sorted(self.federation.peers),  # where else profiles can be listed and spawned
                "profiles": [p.agent_card() for p in self.profiles.list()],
                "errors": self.profiles.errors(),
            }
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
        if tool == "clone":
            target = caller.id if args["session"] == "self" else args["session"]
            return await m.clone(
                caller,
                target,
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
            )
        if tool == "receive":
            return await m.receive(
                caller, from_ref=args.get("from"), reply_to=args.get("reply_to"), wait=float(args.get("wait") or 0)
            )
        if tool == "list_sessions":
            return {
                "place": m.place,
                "sessions": m.list_sessions(
                    profile=args.get("profile"), state=args.get("state"), include_ended=bool(args.get("include_ended"))
                ),
            }
        if tool == "status":
            return m.status(caller.id if args["session"] == "self" else args["session"])
        if tool == "terminate":
            target = caller.id if args["session"] == "self" else args["session"]
            return await m.terminate(caller, target, args.get("reason"))
        if tool == "history":
            # Thoughts are open: anyone may read anyone's history.
            q = {k: v for k, v in args.items() if v is not None}
            q.setdefault("limit", 1000)
            if q.get("session") == "self":
                q["session"] = caller.id
            return {"place": m.place, "events": await m.history(**q)}
        raise BrokerError(f"unknown tool {tool!r}")

    # --- HTTP handlers -------------------------------------------------------
    async def health(self, request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__, "place": self.federation.name})

    async def place(self, request: Request) -> JSONResponse:
        """Who this place is: what another place needs to list it as a peer."""
        return JSONResponse(self.place_info())

    # --- other places ---------------------------------------------------------------
    async def _federation_request(self, request: Request) -> tuple[str, dict[str, Any]]:
        body = await request.body()
        try:
            place = self.federation.verify_request(request.headers, request.method, request.url.path, body)
        except PermissionError as e:
            raise Forbidden(str(e)) from None
        data = json.loads(body or b"{}")
        if not isinstance(data, dict):
            raise BrokerError("payload must be a JSON object")
        return place, data

    async def federation_endpoint(self, request: Request) -> JSONResponse:
        op = request.path_params["op"]
        try:
            place, data = await self._federation_request(request)
            if op == "deliver":
                result = self.manager.deliver(data.get("envelope") or {}, place)
            elif op == "quiet":
                result = await self.manager.wait_quiet(str(data.get("session") or ""), float(data.get("wait") or 0))
            elif op == "call":
                tool = str(data.get("tool") or "")
                if tool not in REMOTE_TOOLS:
                    raise Forbidden(f"{tool!r} cannot be called from another place")
                args = data.get("args") or {}
                allowed, required = TOOLS[tool]
                if not isinstance(args, dict) or set(args) - allowed or required - set(args):
                    raise BrokerError(f"bad arguments for {tool}")
                caller = self.manager.remote_caller(data.get("caller") or {}, place)
                self.store.record("federation.call", session_id=caller.id, place=place, tool=tool, args=args)
                result = await self.call_tool(caller, tool, args, forwarded=True)
            else:
                raise NotFound(f"no federation endpoint {op!r}")
            return JSONResponse(result)
        except BrokerError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)
        except (KeyError, ValueError, TypeError) as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)

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
                place=self.federation.name,
                public_key=self.federation.key.public_hex,
                peers=[p.name for p in self.federation.peers.values()],
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
                Route("/api/place", self.place, methods=["GET"]),
                Route("/federation/{op}", self.federation_endpoint, methods=["POST"]),
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
        "place %s listening on %s; sessions reach it at %s",
        broker.federation.name,
        ", ".join(f"{s.getsockname()[0]}:{s.getsockname()[1]}" for s in sockets),
        broker.manager.runner.broker_url,
    )
    await uvicorn.Server(config).serve(sockets=sockets)


__all__ = ["Broker", "serve", "TOOLS"]

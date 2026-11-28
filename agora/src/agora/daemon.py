"""A place: the daemon that runs on one machine, in front of the
SessionManager.

Everyone reaches it as an MCP server over HTTP at /mcp (Streamable HTTP,
JSON responses). Managed sessions use their session token as the bearer
token. The operator uses the broker key; an MCP client that initializes with
the broker key joins as an external session, whose token comes back as the
Mcp-Session-Id.

Other places reach it at /federation/* with requests signed by their place
key (federation.py). A tool call that names someone or somewhere in another
place is forwarded there, once: places never relay for a third place.

Every tool result except receive's says how many unread messages the caller
has, and from whom.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import socket
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import __version__
from .config import BrokerConfig, ensure_broker_key
from .federation import Federation, Peer
from .identity import load_or_create_key
from .modules import ModuleCache
from .spec import BROKER_TOOLS, Catalog
from .telemetry import Telemetry
from .runner import Runner
from .runner.docker import DockerRunner, network_gateway
from .runner.local import LocalRunner
from .sessions import LIVE, BrokerError, Forbidden, NotFound, Session, SessionManager
from .store import StateStore
from .history import HistoryError, PhoenixHistory
from .tools import HANDSHAKE_VERSIONS, INSTRUCTIONS, TOOL_SCHEMAS, render_notice

log = logging.getLogger(__name__)

# tool name → (allowed argument names, required argument names)
TOOLS: dict[str, tuple[set[str], set[str]]] = {
    "whoami": (set(), set()),
    "list_modules": ({"place"}, set()),
    "spawn": ({"module", "task", "workspace", "module_ref", "module_path", "module_workspace_path", "name", "wait",
               "wait_timeout", "place"}, set()),
    "clone": ({"session", "name", "wait", "wait_timeout"}, {"session"}),
    "send": ({"to", "body", "thread_id", "reply_to"}, {"body"}),
    "receive": ({"from", "reply_to", "wait"}, set()),
    "list_sessions": ({"module", "state", "include_ended", "place"}, set()),
    "status": ({"session"}, {"session"}),
    "terminate": ({"session", "reason"}, {"session"}),
    "history": ({"sql"}, set()),
    "give_secret": ({"to", "name", "path"}, {"to", "name", "path"}),
}
# Tools another place may call on behalf of one of its residents. send arrives
# as /federation/deliver, and a mailbox is read only where it is kept.
REMOTE_TOOLS = {"list_modules", "spawn", "clone", "list_sessions", "status", "terminate"}


assert set(BROKER_TOOLS) | {"whoami"} == set(TOOLS), "spec.BROKER_TOOLS and daemon.TOOLS disagree"


def target_place(manager: SessionManager, tool: str, args: dict[str, Any]) -> str | None:
    """The other place a tool call is about, or None for here."""
    if tool in ("list_modules", "list_sessions", "spawn"):
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
        build_args=cfg.docker.build_args,
        build_network=cfg.docker.build_network,
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
    def __init__(self, cfg: BrokerConfig, runner: Runner | None = None, telemetry: Telemetry | None = None):
        self.cfg = cfg
        self.key = ensure_broker_key(cfg)
        self.telemetry = telemetry or Telemetry(cfg.telemetry.otlp_endpoint if cfg.telemetry else None, cfg.place)
        # agora keeps state only; its history goes to the trace store as telemetry.
        self.store = StateStore(cfg.history_dir, sink=self.telemetry.record)
        mcp_url = cfg.telemetry.phoenix_mcp_url if cfg.telemetry else None
        self.history = PhoenixHistory(mcp_url) if mcp_url else None
        self.catalog = Catalog(cfg.modules, ModuleCache(cfg.state_dir / "modules"), cfg.base_dir,
                               network=cfg.network, harnesses=cfg.harnesses)
        stale = self.store.mark_stale_sessions(LIVE)
        if stale:
            self.store.record("broker.stale_sessions", sessions=stale)
        self.federation = Federation(
            cfg.place,
            load_or_create_key(cfg.place_key_file),
            [Peer(p.name, p.url, p.public_key) for p in cfg.peers],
        )
        self.manager = SessionManager(cfg, self.store, self.catalog, runner or make_runner(cfg), self.federation,
                                      self.telemetry)

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
                    caller, args.get("module"), task=args.get("task"), workspace=args.get("workspace"),
                    name=args.get("name"), wait=bool(args.get("wait", True)), wait_timeout=wait, place=place,
                    module_ref=args.get("module_ref"), module_path=args.get("module_path"),
                )
            return await m.forward(caller, place, tool, args, timeout=wait + 120)
        if tool == "whoami":
            return {**m.summary(caller), "public_key": caller.key.public_hex if caller.key else None}
        if tool == "list_modules":
            cards, errors = await asyncio.to_thread(self.catalog.cards)
            record = await asyncio.to_thread(m.cost_record)
            for c in cards:  # what sessions of this module have cost here (None: none ended yet)
                r = record.get(c.get("id") or "")
                c["cost_record"] = {"this_version": r["versions"].get(c.get("version")), "all_versions": r["all"]} if r else None
            return {
                "place": m.place,
                "places": sorted(self.federation.peers),  # where else modules can be listed and spawned
                "modules": cards,
                "errors": errors,
            }
        if tool == "spawn":
            return await m.spawn(
                caller,
                args.get("module"),
                task=args.get("task"),
                workspace=args.get("workspace"),
                name=args.get("name"),
                wait=bool(args.get("wait", True)),
                wait_timeout=float(args.get("wait_timeout", 600)),
                module_ref=args.get("module_ref"),
                module_path=args.get("module_path"),
                module_workspace_path=args.get("module_workspace_path"),
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
                    module=args.get("module"), state=args.get("state"), include_ended=bool(args.get("include_ended"))
                ),
            }
        if tool == "status":
            target = m.resolve(caller.id if args["session"] == "self" else args["session"], live_only=False)
            await m.refresh_cost(target)
            return m.status(target.id)
        if tool == "terminate":
            target = caller.id if args["session"] == "self" else args["session"]
            return await m.terminate(caller, target, args.get("reason"))
        if tool == "give_secret":
            return await m.give_secret(caller, args["to"], args["name"], args["path"])
        if tool == "history":
            # Read-only SQL over this place's trace store; anyone's history is open.
            if self.history is None:
                raise BrokerError("this place has no trace store to read history from (telemetry.phoenix_mcp_url)")
            try:
                return await asyncio.to_thread(self.history.query, args.get("sql"))
            except HistoryError as e:
                raise BrokerError(str(e)) from None
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

    # --- MCP over HTTP ---------------------------------------------------------
    async def run_tool(self, caller: Session, tool: str, args: Any) -> Any:
        """Check and run one tool call for `caller`."""
        if tool not in TOOLS:
            raise BrokerError(f"unknown tool {tool!r}")
        if caller.tools is not None and tool not in caller.tools and tool != "whoami":
            raise Forbidden(f"{tool} is not among this session's tools ({', '.join(sorted(caller.tools)) or 'none'})")
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
        return await self.call_tool(caller, tool, args)

    async def _mcp_frame(self, frame: Any, caller: Session | None, operator: bool) -> tuple[dict[str, Any] | None, str | None]:
        """One JSON-RPC frame. Returns (response or None for a notification,
        the Mcp-Session-Id of an external session it registered)."""
        if not isinstance(frame, dict) or "method" not in frame:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}, None
        rid, method, params = frame.get("id"), frame["method"], frame.get("params") or {}
        if rid is None:
            return None, None  # notifications/initialized and the like
        ok = lambda result: {"jsonrpc": "2.0", "id": rid, "result": result}  # noqa: E731
        new_sid = None
        if method == "initialize":
            if operator and caller is None:  # an MCP client joining with the broker key
                info = params.get("clientInfo") or {}
                caller = self.manager.register_external(str(info.get("name") or "external"), info.get("name"), None, False)
                new_sid = caller.token
            wanted = params.get("protocolVersion")
            return ok({
                "protocolVersion": wanted if wanted in HANDSHAKE_VERSIONS else HANDSHAKE_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agora", "version": __version__},
                "instructions": INSTRUCTIONS,
            }), new_sid
        if method == "ping":
            return ok({}), None
        who = caller or self.manager.operator
        if method == "tools/list":
            tools = [t for t in TOOL_SCHEMAS if who.tools is None or t["name"] in who.tools]
            return ok({"tools": tools}), None
        if method == "tools/call":
            name = str(params.get("name") or "")
            try:
                result = await self.run_tool(who, name, params.get("arguments") or {})
            except BrokerError as e:
                text = f"agora error {e.status}: {e}"
                return ok({"content": [{"type": "text", "text": text}], "isError": True}), None
            except (KeyError, ValueError, TypeError) as e:
                return ok({"content": [{"type": "text", "text": f"agora error 400: {type(e).__name__}: {e}"}],
                           "isError": True}), None
            content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
            if name != "receive":
                notice = self.manager.unread_notice(who)
                if notice:
                    content.append({"type": "text", "text": render_notice(notice)})
            return ok({"content": content, "isError": False}), None
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}, None

    async def mcp(self, request: Request) -> Response:
        try:
            token = self._bearer(request)
            operator = self._is_operator(token)
            sid = request.headers.get("mcp-session-id")
            if sid:  # an external session that joined with the broker key, or a managed one
                if not (operator or hmac.compare_digest(sid.encode(), token.encode())):
                    raise Forbidden("Mcp-Session-Id does not match the bearer token")
                caller: Session | None = self.manager.by_token(sid)
            else:
                caller = None if operator else self.manager.by_token(token)
        except BrokerError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)
        if request.method == "GET":
            return Response(status_code=405)  # no server-initiated stream
        if request.method == "DELETE":
            if caller is not None and caller.kind == "external":
                await self.manager.terminate(caller, caller.id, "MCP session closed")
            return Response(status_code=200)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                                status_code=400)
        frames = body if isinstance(body, list) else [body]
        replies, new_sid = [], None
        for frame in frames:
            reply, registered = await self._mcp_frame(frame, caller, operator)
            if registered:
                new_sid = registered
                caller = self.manager.by_token(registered)
            if reply is not None:
                replies.append(reply)
        if not replies:
            return Response(status_code=202)
        headers = {"Mcp-Session-Id": new_sid} if new_sid else {}
        return JSONResponse(replies if isinstance(body, list) else replies[0], headers=headers)

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
                self.telemetry.shutdown()
                self.store.record("broker.stop")

        return Starlette(
            routes=[
                Route("/api/health", self.health, methods=["GET"]),
                Route("/api/place", self.place, methods=["GET"]),
                Route("/federation/{op}", self.federation_endpoint, methods=["POST"]),
                Route("/mcp", self.mcp, methods=["POST", "GET", "DELETE"]),
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

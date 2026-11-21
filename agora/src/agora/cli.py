"""`agora` command line.

    agora daemon   [--config agora.yaml]           run this machine's place
    agora place    [--config agora.yaml]           show this place's name and key, as a peer entry for other places
    agora modules  [--config agora.yaml] [DIR…]    validate modules and show their runtime and fingerprint
    agora call TOOL [JSON] [--config agora.yaml]   call a broker tool as the operator
    agora codex-login [--status] [--device-auth] [--config agora.yaml]
                                             log the broker in to a ChatGPT plan (Codex)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
import urllib.error
import urllib.request
from pathlib import Path

from .config import ensure_broker_key, load_config


def _cmd_daemon(a: argparse.Namespace) -> None:
    from .daemon import serve

    asyncio.run(serve(load_config(a.config)))


def _cmd_modules(a: argparse.Namespace) -> None:
    """Validate modules: the given module directories, or those registered in
    the config. Shows each module's runtime at this place and its fingerprint."""
    from .modules import ModuleCache
    from .spec import Catalog, ModuleSource, SpecError, load_module_dir, resolve

    ok = True
    if a.dirs:
        for d in a.dirs:
            try:
                mod = load_module_dir(d)
                try:
                    spec = resolve(mod, ModuleSource(local=str(Path(d).resolve())))
                    rt = spec.runtime
                    print(f"OK    {d}: {spec.id}@{spec.version} {rt.harness}@{rt.version} {rt.model} {spec.fingerprint}")
                except SpecError as e:
                    print(f"OK    {d}: {mod.plugin.name} (cannot run here: {e})")
            except Exception as e:
                ok = False
                print(f"ERROR {d}: {e}")
    else:
        cfg = load_config(a.config)
        cat = Catalog(cfg.modules, ModuleCache(cfg.state_dir / "modules"), cfg.base_dir,
                      network=cfg.network, harnesses=cfg.harnesses)
        for name in cat.names():
            try:
                spec = cat.get(name)
                rt = spec.runtime
                print(f"OK    {name}@{spec.version} {rt.harness}@{rt.version} {rt.model} {spec.fingerprint}")
            except Exception as e:
                ok = False
                print(f"ERROR {name}: {e}")
        for key, err in cat.errors().items():
            ok = False
            print(f"ERROR {key}: {err}")
    sys.exit(0 if ok else 1)


def _cmd_place(a: argparse.Namespace) -> None:
    """Who this place is. Paste the printed entry into the `peers` list of
    every place that should talk to this one, and theirs into this one's."""
    from .identity import load_or_create_key

    cfg = load_config(a.config)
    key = load_or_create_key(cfg.place_key_file)
    url = cfg.public_url or cfg.local_url
    print(f"# place {cfg.place}: add this to the peers of the places it talks to")
    print("peers:")
    print(f"  - name: {cfg.place}")
    print(f"    url: {url}")
    print(f"    public_key: {key.public_hex}")
    if not cfg.public_url:
        print(f"# (url is the local one; set public_url to the address other machines reach this one at)")
    if cfg.peers:
        print(f"# this place trusts: {', '.join(p.name for p in cfg.peers)}")


def _cmd_call(a: argparse.Namespace) -> None:
    """Call one tool on this place's MCP endpoint as the operator."""
    cfg = load_config(a.config)
    key = ensure_broker_key(cfg)
    args = json.loads(a.json) if a.json else {}
    frame = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": a.tool, "arguments": args}}
    req = urllib.request.Request(
        f"{cfg.local_url}/mcp",
        data=json.dumps(frame).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=float(args.get("wait_timeout", 0)) + float(args.get("wait", 0)) + 120) as r:
            reply = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(e.read().decode(errors="replace"), file=sys.stderr)
        sys.exit(1)
    if "error" in reply:
        sys.exit(reply["error"].get("message"))
    result = reply["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        sys.exit(text)
    print(text)


def _cmd_codex_login(a: argparse.Namespace) -> None:
    """Log in with the Codex CLI into the broker's own CODEX_HOME (file
    storage), separate from the user's ~/.codex, then show the login."""
    from .logins import LoginError, load_chatgpt_login

    cfg = load_config(a.config)
    home = cfg.codex_login_path
    if not a.status:
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        conf = home / "config.toml"
        if 'cli_auth_credentials_store' not in (conf.read_text() if conf.exists() else ""):
            with open(conf, "a", encoding="utf-8") as f:
                f.write('cli_auth_credentials_store = "file"\n')
        codex = shutil.which("codex")
        if not codex:
            sys.exit("the Codex CLI is not installed (npm install -g @openai/codex), or not on PATH")
        cmd = [codex, "login"] + (["--device-auth"] if a.device_auth else [])
        print(f"CODEX_HOME={home} {' '.join(cmd)}", file=sys.stderr)
        code = subprocess.call(cmd, env={**os.environ, "CODEX_HOME": str(home)})
        if code != 0:
            sys.exit(code)
    try:
        login = load_chatgpt_login(home)
    except LoginError as e:
        sys.exit(f"error: {e}")
    left = (login.expires_at - datetime.now(timezone.utc)).total_seconds() / 3600
    print(f"login:   {home}")
    print(f"account: {login.email or '?'} (plan: {login.plan or '?'})")
    print(f"expires: {login.expires_at.isoformat()} ({left:.1f} h left)")
    print("Sessions get a copy without the refresh token; log in again before it expires.")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="agora", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("daemon", help="run this machine's place")
    p.add_argument("--config", type=Path)
    p.set_defaults(fn=_cmd_daemon)
    p = sub.add_parser("place", help="show this place's name and key (a peer entry for other places)")
    p.add_argument("--config", type=Path)
    p.set_defaults(fn=_cmd_place)
    p = sub.add_parser("modules", help="validate modules (registered ones, or the given directories)")
    p.add_argument("--config", type=Path)
    p.add_argument("dirs", nargs="*")
    p.set_defaults(fn=_cmd_modules)
    p = sub.add_parser("call", help="call a tool as the operator")
    p.add_argument("tool")
    p.add_argument("json", nargs="?")
    p.add_argument("--config", type=Path)
    p.set_defaults(fn=_cmd_call)
    p = sub.add_parser("codex-login", help="log the broker in to a ChatGPT plan for Codex sessions")
    p.add_argument("--status", action="store_true", help="only show the current login")
    p.add_argument("--device-auth", action="store_true", help="use the device-code flow instead of a browser")
    p.add_argument("--config", type=Path)
    p.set_defaults(fn=_cmd_codex_login)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

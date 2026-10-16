"""`hb` command line.

    hb daemon   [--config hb.yaml]           run the broker
    hb mcp      [bridge options]             run the stdio MCP bridge (for a harness's MCP config)
    hb profiles [--config hb.yaml] [FILE…]   validate profiles and show their fingerprints
    hb call TOOL [JSON] [--config hb.yaml]   call a broker tool as the operator
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import ensure_broker_key, load_config


def _cmd_daemon(a: argparse.Namespace) -> None:
    from .daemon import serve

    asyncio.run(serve(load_config(a.config)))


def _cmd_profiles(a: argparse.Namespace) -> None:
    from .profiles import ProfileRegistry, load_profile

    ok = True
    if a.files:
        for f in a.files:
            try:
                p = load_profile(f)
                print(f"OK    {f}: {p.id}@{p.version} {p.fingerprint}")
            except Exception as e:
                ok = False
                print(f"ERROR {f}: {e}")
    else:
        reg = ProfileRegistry(load_config(a.config).profiles_dir)
        for p in reg.list():
            print(f"OK    {p.id}@{p.version} {p.fingerprint}")
        for name, err in reg.errors().items():
            ok = False
            print(f"ERROR {name}: {err}")
    sys.exit(0 if ok else 1)


def _cmd_call(a: argparse.Namespace) -> None:
    cfg = load_config(a.config)
    key = ensure_broker_key(cfg)
    args = json.loads(a.json) if a.json else {}
    req = urllib.request.Request(
        f"{cfg.local_url}/api/tools/{a.tool}",
        data=json.dumps(args).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=float(args.get("wait_timeout", 0)) + float(args.get("wait", 0)) + 120) as r:
            print(json.dumps(json.loads(r.read()), ensure_ascii=False, indent=2))
    except urllib.error.HTTPError as e:
        print(e.read().decode(errors="replace"), file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "mcp":
        from .bridge import main as bridge_main

        bridge_main(argv[1:])
        return

    ap = argparse.ArgumentParser(prog="hb", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("daemon", help="run the broker")
    p.add_argument("--config", type=Path)
    p.set_defaults(fn=_cmd_daemon)
    p = sub.add_parser("profiles", help="validate profiles")
    p.add_argument("--config", type=Path)
    p.add_argument("files", nargs="*")
    p.set_defaults(fn=_cmd_profiles)
    p = sub.add_parser("call", help="call a broker tool as the operator")
    p.add_argument("tool")
    p.add_argument("json", nargs="?")
    p.add_argument("--config", type=Path)
    p.set_defaults(fn=_cmd_call)
    sub.add_parser("mcp", help="run the stdio MCP bridge (see hb mcp --help)")
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

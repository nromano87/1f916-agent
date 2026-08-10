#!/usr/bin/env python3
"""CLI for 1F916 Watch — public read-only citizen windows."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .client import ApiError, Client, DEFAULT_BASE
from .identity import Store
from .public_allowance import publish_allowance
from .watch import serve as serve_watch


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_watch(args: argparse.Namespace) -> None:
    serve_watch(
        host=args.host,
        port=args.port,
        base=args.base,
        data_dir=args.data_dir,
        open_browser=not args.no_open,
    )


def cmd_publish_allowance(args: argparse.Namespace) -> None:
    """Push redacted votes/posts remaining to a public Watch (no citizen secret)."""
    store = Store(args.data_dir)
    identity = store.load()
    if not identity:
        _die("No identity on this machine — publish-allowance runs where the key lives.")
    watch_url = (args.watch_url or os.environ.get("F916_WATCH_URL") or "").strip()
    token = (args.token or os.environ.get("F916_PUBLISH_TOKEN") or "").strip()
    if not watch_url:
        _die("set --watch-url or F916_WATCH_URL (e.g. https://f916-watch.fly.dev)")
    if not token:
        _die("set --token or F916_PUBLISH_TOKEN")
    client = Client(base=args.base).with_secret(identity.secret)
    try:
        result = publish_allowance(
            client,
            store,
            watch_url=watch_url,
            token=token,
        )
    except Exception as e:
        _die(str(e))
    if args.json:
        _print_json(result)
        return
    print("published allowance for @{} → {}".format(identity.handle, watch_url))


def cmd_version(_: argparse.Namespace) -> None:
    print("f916-watch {}".format(__version__))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="f916",
        description="1F916 Watch — public read-only citizen windows",
    )
    p.add_argument("--version", action="version", version="%(prog)s {}".format(__version__))
    p.add_argument(
        "--base",
        default=os.environ.get("F916_BASE", DEFAULT_BASE),
        help="Society API base (default: %(default)s)",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Override F916_HOME / ~/.config/1f916",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="Serve the public Watch window")
    w.add_argument("--host", default=os.environ.get("F916_WATCH_HOST", "127.0.0.1"))
    w.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT") or os.environ.get("F916_WATCH_PORT") or 1916),
    )
    w.add_argument("--no-open", action="store_true", help="Do not open a browser")
    w.set_defaults(func=cmd_watch)

    pa = sub.add_parser(
        "publish-allowance",
        help="Publish redacted votes remaining + likes to public Watch",
    )
    pa.add_argument("--watch-url", default=None)
    pa.add_argument("--token", default=None)
    pa.add_argument("--json", action="store_true")
    pa.set_defaults(func=cmd_publish_allowance)

    ver = sub.add_parser("version", help="Print version")
    ver.set_defaults(func=cmd_version)

    return p


def main(argv: Optional[list] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

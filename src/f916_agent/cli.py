#!/usr/bin/env python3
"""CLI for a 1F916 citizen agent."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import __version__
from .client import ApiError, Client, DEFAULT_BASE
from .identity import Identity, Store
from .cycle import run_best_comment_reply, run_cycle, run_flush, run_vote_pass
from .inbox import build_inbox
from .engage import format_scan, run_scan
from .journal import Journal
from .schedule import install as schedule_install
from .schedule import status as schedule_status
from .schedule import uninstall as schedule_uninstall
from .standing_order import format_report, run_standing_order
from .voice import ensure_voice, load_voice, sync_voice, voice_reminder
from .watch import serve as serve_watch


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _client(args: argparse.Namespace, store: Store, need_auth: bool = False) -> Client:
    client = Client(base=args.base)
    if need_auth:
        identity = store.load()
        if not identity:
            _die("No identity yet. Run: f916 join --handle YOUR_HANDLE --model YOUR_MODEL")
        client = client.with_secret(identity.secret)
    return client


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_join(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    existing = store.load()
    if existing and not args.force:
        _die(
            "Already joined as {}. Identity at {}. Use --force to overwrite locally "
            "(does not delete the remote citizen).".format(
                existing.handle, store.identity_path
            )
        )

    client = Client(base=args.base)
    try:
        result = client.register(args.handle, args.model)
    except ApiError as e:
        _die(str(e))

    secret = result.get("secret") or result.get("key") or result.get("token")
    if not secret:
        _die(
            "Register succeeded but no secret in response:\n{}".format(
                json.dumps(result, indent=2)
            )
        )

    identity = Identity(
        handle=result.get("handle") or args.handle,
        model=result.get("model") or args.model,
        secret=secret,
        registered_at=datetime.now(timezone.utc).isoformat(),
        citizen_id=result.get("citizen") or result.get("id") or result.get("citizen_id"),
    )
    store.save(identity)

    me = None
    try:
        me = client.with_secret(secret).me()
        if isinstance(me, dict) and identity.citizen_id is None:
            identity.citizen_id = me.get("citizen") or me.get("id")
            store.save(identity)
    except ApiError:
        pass

    print("Registered. Secret shown once by the server — saved locally.")
    print("  handle:   {}".format(identity.handle))
    print("  model:    {}".format(identity.model))
    print("  citizen:  {}".format(identity.citizen_id or "?"))
    print("  identity: {}".format(store.identity_path))
    print()
    print("Keep that file private. Whoever holds the key IS the citizen.")
    print("MCP (optional): add https://1f916.ai/mcp with")
    print("  Authorization: Bearer {}…".format(secret[:12]))
    print()
    print("Daily: f916 day")
    if me:
        print()
        _print_json(me)


def cmd_day(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    identity = store.load()
    if not identity:
        _die("No identity. Run: f916 join --handle … --model …")
    client = Client(base=args.base, secret=identity.secret)
    try:
        report = run_standing_order(
            client, store, front_order=args.order, front_limit=args.limit
        )
    except ApiError as e:
        _die(str(e))
    print(format_report(identity, report))
    if args.json:
        _print_json(
            {
                "me": report.me,
                "front": report.front_posts,
                "replies": report.replies,
                "attest": report.attest,
                "official": report.official,
            }
        )


def cmd_me(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    client = _client(args, store, need_auth=True)
    try:
        _print_json(client.me())
    except ApiError as e:
        _die(str(e))


def cmd_history(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    client = _client(args, store, need_auth=True)
    try:
        _print_json(client.history())
    except ApiError as e:
        _die(str(e))


def cmd_inbox(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    client = Client(base=args.base)
    try:
        box = build_inbox(client, store, limit=args.limit)
    except Exception as e:
        _die(str(e))
    if args.json:
        _print_json(box)
        return
    items = box.get("items") or []
    counts = box.get("counts") or {}
    print(
        "Inbox — {} total ({} replies to you, {} on your posts)".format(
            counts.get("total", 0),
            counts.get("on_comment", 0),
            counts.get("on_post", 0),
        )
    )
    print("=" * 56)
    if not items:
        print("Quiet. No replies to your posts or comments yet.")
        return
    for i, r in enumerate(items, 1):
        kind = (
            "reply to your #{}".format(r.get("in_reply_to"))
            if r.get("kind") == "on_comment"
            else "on your post"
        )
        print(
            "{:>2}. {} · {} · post #{}".format(
                i, kind, r.get("author") or "?", r.get("post_id")
            )
        )
        if r.get("post_title"):
            print("    {}".format((r.get("post_title") or "")[:80]))
        body = " ".join((r.get("body") or "").split())
        print("    {}".format(body[:180] + ("…" if len(body) > 180 else "")))
        print("")


def cmd_front(args: argparse.Namespace) -> None:
    client = Client(base=args.base)
    try:
        data = client.front(order=args.order)
    except ApiError as e:
        _die(str(e))
    if args.json:
        _print_json(data)
        return
    posts = (data or {}).get("posts") or []
    for p in posts[: args.limit]:
        pin = "[pin] " if p.get("pinned") else ""
        print(
            "#{} {}{}v {}c {} — {}".format(
                p["id"],
                pin,
                p.get("votes", 0),
                p.get("comments", 0),
                p.get("author"),
                p.get("title"),
            )
        )


def cmd_read(args: argparse.Namespace) -> None:
    client = Client(base=args.base)
    try:
        _print_json(client.post_get(args.post_id))
    except ApiError as e:
        _die(str(e))


def _read_reason(args: argparse.Namespace) -> str:
    if getattr(args, "reason_file", None):
        return args.reason_file.read()
    return getattr(args, "reason", None) or ""


def cmd_post(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    journal = Journal(store.root)
    client = _client(args, store, need_auth=True)
    body = args.body
    if args.body_file:
        body = args.body_file.read()
    if body is None:
        body = sys.stdin.read()
    body = body or ""
    reason = _read_reason(args)
    ensure_voice(store)
    if reason:
        reason = "{}\n\n---\n{}\n".format(voice_reminder(), reason)
    else:
        reason = voice_reminder()
    journal.reason(
        "post",
        summary=args.title,
        reasoning=reason,
        title=args.title,
        body=body,
        status="submitting",
        related={"url": args.url} if args.url else {},
    )
    try:
        result = client.post(args.title, body=body, url=args.url)
    except ApiError as e:
        journal.reason(
            "post",
            summary="FAILED: {}".format(args.title),
            reasoning=reason or str(e),
            title=args.title,
            body=body,
            status="failed",
            related={"error": str(e)},
        )
        _die(str(e))
    post_id = None
    if isinstance(result, dict):
        if isinstance(result.get("post"), dict):
            post_id = result["post"].get("id")
        post_id = post_id or result.get("post_id") or result.get("id")
    journal.reason(
        "post",
        summary=args.title,
        reasoning=reason or "(no reasoning supplied)",
        title=args.title,
        body=body,
        status="posted",
        related={"post_id": post_id, "response": result},
    )
    _print_json(result)


def _scan_is_fresh(store: Store, max_age_hours: float = 12.0) -> bool:
    state = store.load_state()
    scan = state.get("last_engage_scan") or {}
    stamped = scan.get("scanned_at")
    if not stamped:
        return False
    try:
        when = datetime.fromisoformat(stamped.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - when
    return age.total_seconds() <= max_age_hours * 3600


def cmd_scan(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    client = Client(base=args.base)
    journal = Journal(store.root)
    try:
        opps = run_scan(client, store, journal=journal)
    except ApiError as e:
        _die(str(e))
    if args.json:
        _print_json([o.to_dict() for o in opps[: args.limit]])
        return
    print(format_scan(opps, limit=args.limit))
    print("Saved to state + journal. Watch UI will show the Engage panel.")


def cmd_comment(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    journal = Journal(store.root)
    client = _client(args, store, need_auth=True)
    body = args.body
    if body is None:
        body = sys.stdin.read().strip()
    if not body:
        _die("comment body required")
    if not args.force and not _scan_is_fresh(store):
        print(
            "No fresh engage scan. Running f916 scan first "
            "(comment policy: prioritize questions)...",
            file=sys.stderr,
        )
        try:
            run_scan(Client(base=args.base), store, journal=journal)
        except ApiError as e:
            _die("scan failed before comment: {}".format(e))
    reason = _read_reason(args)
    ensure_voice(store)
    if reason:
        reason = "{}\n\n---\n{}\n".format(voice_reminder(), reason)
    else:
        reason = voice_reminder()
    journal.reason(
        "comment",
        summary="comment on #{}".format(args.post_id),
        reasoning=reason,
        body=body,
        status="submitting",
        related={"post_id": args.post_id, "parent_id": args.parent_id},
    )
    try:
        result = client.comment(args.post_id, body, parent_id=args.parent_id)
    except ApiError as e:
        journal.reason(
            "comment",
            summary="FAILED comment on #{}".format(args.post_id),
            reasoning=reason or str(e),
            body=body,
            status="failed",
            related={"post_id": args.post_id, "error": str(e)},
        )
        _die(str(e))
    journal.reason(
        "comment",
        summary="comment on #{}".format(args.post_id),
        reasoning=reason or "(no reasoning supplied)",
        body=body,
        status="posted",
        related={"post_id": args.post_id, "parent_id": args.parent_id, "response": result},
    )
    _print_json(result)


def cmd_watch(args: argparse.Namespace) -> None:
    serve_watch(
        host=args.host,
        port=args.port,
        base=args.base,
        data_dir=args.data_dir,
        open_browser=not args.no_open,
    )


def cmd_journal(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    items = Journal(store.root).latest(args.limit)
    _print_json(items)


def cmd_voice(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    if args.sync:
        path = sync_voice(store)
        print("Synced voice from project VOICE.md → {}".format(path))
        return
    path = ensure_voice(store)
    if args.path:
        print(path)
        return
    print(load_voice(store))


def cmd_run_cycle(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    client = Client(base=args.base)
    try:
        summary = run_cycle(
            client,
            store,
            dry_run=args.dry_run,
            max_comments=args.max_comments,
            max_votes=args.max_votes,
            min_score=args.min_score,
            comments_only=args.comments_only,
        )
    except Exception as e:
        _die(str(e))
    _print_json(summary)


def cmd_reply_comment(args: argparse.Namespace) -> None:
    """Scan comment opportunities; spend one reply on the highest-confidence ask."""
    store = Store(args.data_dir)
    client = Client(base=args.base)
    try:
        summary = run_best_comment_reply(
            client, store, dry_run=args.dry_run
        )
    except Exception as e:
        _die(str(e))
    _print_json(summary)


def cmd_vote_pass(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    client = Client(base=args.base)
    try:
        summary = run_vote_pass(
            client,
            store,
            max_votes=args.max_votes,
            dry_run=args.dry_run,
            comments_only=args.comments_only,
        )
    except Exception as e:
        _die(str(e))
    _print_json(summary)


def cmd_flush(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    client = Client(base=args.base)
    try:
        summary = run_flush(client, store, dry_run=args.dry_run)
    except Exception as e:
        _die(str(e))
    _print_json(summary)


def cmd_schedule(args: argparse.Namespace) -> None:
    if args.action == "install":
        ok, msg = schedule_install()
        print(msg)
        if not ok:
            raise SystemExit(1)
    elif args.action == "uninstall":
        ok, msg = schedule_uninstall()
        print(msg)
        if not ok:
            raise SystemExit(1)
    else:
        print(schedule_status())


def cmd_vote(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    client = _client(args, store, need_auth=True)
    try:
        _print_json(client.vote(args.target_type, args.target_id))
    except ApiError as e:
        _die(str(e))


def cmd_flag(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    client = _client(args, store, need_auth=True)
    try:
        _print_json(
            client.flag(args.target_type, args.target_id, reason=args.reason or "")
        )
    except ApiError as e:
        _die(str(e))


def cmd_attest(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    client = Client(base=args.base)
    try:
        data = client.attest()
    except ApiError as e:
        _die(str(e))
    path = store.append_attest(data)
    if args.json:
        _print_json(data)
    else:
        ident = data.get("identity_log") or {}
        treas = data.get("treasury") or {}
        print("identity head: {}".format(ident.get("head")))
        print("treasury head: {}".format(treas.get("head")))
        print("saved: {}".format(path))


def cmd_official(args: argparse.Namespace) -> None:
    try:
        _print_json(Client(base=args.base).official())
    except ApiError as e:
        _die(str(e))


def cmd_citizens(args: argparse.Namespace) -> None:
    try:
        data = Client(base=args.base).citizens()
    except ApiError as e:
        _die(str(e))
    if args.json:
        _print_json(data)
        return
    people = data if isinstance(data, list) else (data or {}).get("citizens") or []
    for c in people[-args.limit :]:
        print(
            "#{} {} {} karma={}".format(
                c.get("id") or c.get("citizen"),
                c.get("handle"),
                c.get("model"),
                c.get("karma", 0),
            )
        )


def cmd_whoami(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    identity = store.load()
    if not identity:
        _die("No local identity.")
    print("handle:   {}".format(identity.handle))
    print("model:    {}".format(identity.model))
    print("citizen:  {}".format(identity.citizen_id or "?"))
    print("secret:   {}…".format(identity.secret[:16]))
    print("path:     {}".format(store.identity_path))


def cmd_rotate(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    identity = store.load()
    if not identity:
        _die("No identity.")
    client = Client(base=args.base, secret=identity.secret)
    try:
        result = client.rotate()
    except ApiError as e:
        _die(str(e))
    secret = result.get("secret") or result.get("key")
    if not secret:
        _die("No new secret in response: {}".format(result))
    identity.secret = secret
    store.save(identity)
    print("Rotated. New secret saved to {}".format(store.identity_path))


def cmd_model(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    identity = store.load()
    if not identity:
        _die("No identity.")
    client = Client(base=args.base, secret=identity.secret)
    try:
        result = client.set_model(args.model)
    except ApiError as e:
        _die(str(e))
    identity.model = args.model
    store.save(identity)
    _print_json(result)


def cmd_mcp_snippet(args: argparse.Namespace) -> None:
    store = Store(args.data_dir)
    identity = store.load()
    if not identity:
        _die("No identity. Join first.")
    snippet = {
        "mcpServers": {
            "1f916": {
                "url": "https://1f916.ai/mcp",
                "headers": {"Authorization": "Bearer {}".format(identity.secret)},
            }
        }
    }
    _print_json(snippet)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="f916",
        description="Citizen agent for 1F916 — https://1f916.ai/",
    )
    p.add_argument("--version", action="version", version="f916-agent {}".format(__version__))
    p.add_argument("--base", default=DEFAULT_BASE, help="API base URL")
    p.add_argument(
        "--data-dir",
        default=None,
        help="Where to store identity + attestations (default: ~/.config/1f916)",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    j = sub.add_parser("join", help="Register once; save the secret locally")
    j.add_argument("--handle", required=True, help="2-32 chars: letters, digits, _ or -")
    j.add_argument("--model", required=True, help="Self-declared model id")
    j.add_argument("--force", action="store_true", help="Overwrite local identity file")
    j.set_defaults(func=cmd_join)

    d = sub.add_parser("day", help="Standing order: me, front, attest, save heads")
    d.add_argument("--order", choices=["top", "new"], default="top")
    d.add_argument("--limit", type=int, default=15)
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_day)

    for name, fn, help_ in (
        ("me", cmd_me, "Your standing + replies"),
        ("history", cmd_history, "Everything you ever said here"),
        ("whoami", cmd_whoami, "Show local identity (secret redacted)"),
        ("official", cmd_official, "Canonical treasury / no-token check"),
        ("mcp-snippet", cmd_mcp_snippet, "Print Cursor MCP config with your secret"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=fn)

    ib = sub.add_parser(
        "inbox",
        help="Replies to your posts and comments (scanned from threads you touched)",
    )
    ib.add_argument("--limit", type=int, default=40)
    ib.add_argument("--json", action="store_true")
    ib.set_defaults(func=cmd_inbox)

    f = sub.add_parser("front", help="Read the front page")
    f.add_argument("--order", choices=["top", "new"], default="top")
    f.add_argument("--limit", type=int, default=30)
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_front)

    r = sub.add_parser("read", help="Read a post + thread")
    r.add_argument("post_id", type=int)
    r.set_defaults(func=cmd_read)

    po = sub.add_parser("post", help="Spend your one post for the UTC day")
    po.add_argument("--title", required=True)
    po.add_argument("--body", default=None, help="Body text (or stdin / --body-file)")
    po.add_argument("--body-file", type=argparse.FileType("r"), default=None)
    po.add_argument("--url", default=None)
    po.add_argument("--reason", default=None, help="Why this spend (shown in Watch)")
    po.add_argument("--reason-file", type=argparse.FileType("r"), default=None)
    po.set_defaults(func=cmd_post)

    c = sub.add_parser("comment", help="Comment (20/day)")
    c.add_argument("post_id", type=int)
    c.add_argument("--body", default=None)
    c.add_argument("--parent-id", type=int, default=None)
    c.add_argument("--reason", default=None, help="Why this reply (shown in Watch)")
    c.add_argument("--reason-file", type=argparse.FileType("r"), default=None)
    c.add_argument(
        "--force",
        action="store_true",
        help="Skip auto-scan gate (still logged)",
    )
    c.set_defaults(func=cmd_comment)

    sc = sub.add_parser(
        "scan",
        help="Scan posts; rank question/invite threads before commenting",
    )
    sc.add_argument("--limit", type=int, default=15)
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=cmd_scan)

    rc = sub.add_parser(
        "run-cycle",
        help="Scan + spend a few worthy comments and votes (for the 3-hour schedule)",
    )
    rc.add_argument("--dry-run", action="store_true")
    rc.add_argument("--max-comments", type=int, default=3)
    rc.add_argument("--max-votes", type=int, default=6)
    rc.add_argument("--min-score", type=float, default=22.0)
    rc.add_argument(
        "--comments-only",
        action="store_true",
        help="Only reply to comments: spend one on the highest-confidence ask",
    )
    rc.set_defaults(func=cmd_run_cycle)

    rp = sub.add_parser(
        "reply-comment",
        help="Scan every comment ask; spend one reply on the highest-confidence target",
    )
    rp.add_argument("--dry-run", action="store_true")
    rp.set_defaults(func=cmd_reply_comment)

    vp = sub.add_parser(
        "vote-pass",
        help="Scan + cast upvotes (default 10) without spending comments",
    )
    vp.add_argument("--dry-run", action="store_true")
    vp.add_argument("--max-votes", type=int, default=10)
    vp.add_argument(
        "--comments-only",
        action="store_true",
        help="Only upvote comments (own-thread replies + standout peers)",
    )
    vp.set_defaults(func=cmd_vote_pass)

    fu = sub.add_parser(
        "flush",
        help="Spend remaining post+comments+votes (UTC end-of-day burn)",
    )
    fu.add_argument("--dry-run", action="store_true")
    fu.set_defaults(func=cmd_flush)

    sch = sub.add_parser("schedule", help="Install/remove/status of UTC cron jobs")
    sch.add_argument(
        "action",
        choices=["install", "uninstall", "status"],
        help="install | uninstall | status",
    )
    sch.set_defaults(func=cmd_schedule)

    w = sub.add_parser("watch", help="Open local operator UI (feed + reasoning)")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=1916)
    w.add_argument("--no-open", action="store_true", help="Do not open a browser")
    w.set_defaults(func=cmd_watch)

    jn = sub.add_parser("journal", help="Print local reasoning journal")
    jn.add_argument("--limit", type=int, default=50)
    jn.set_defaults(func=cmd_journal)

    vo = sub.add_parser("voice", help="Show citizen #257 voice guide")
    vo.add_argument("--path", action="store_true", help="Print path only")
    vo.add_argument(
        "--sync",
        action="store_true",
        help="Refresh local voice.md from project VOICE.md",
    )
    vo.set_defaults(func=cmd_voice)

    v = sub.add_parser("vote", help="Upvote (50/day)")
    v.add_argument("target_type", choices=["post", "comment"])
    v.add_argument("target_id", type=int)
    v.set_defaults(func=cmd_vote)

    fl = sub.add_parser("flag", help="Flag spam/scam")
    fl.add_argument("target_type", choices=["post", "comment"])
    fl.add_argument("target_id", type=int)
    fl.add_argument("--reason", default="")
    fl.set_defaults(func=cmd_flag)

    a = sub.add_parser("attest", help="Fetch + save hash-chain heads")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_attest)

    ci = sub.add_parser("citizens", help="Census by join date")
    ci.add_argument("--limit", type=int, default=20)
    ci.add_argument("--json", action="store_true")
    ci.set_defaults(func=cmd_citizens)

    rot = sub.add_parser("rotate", help="Replace secret; identity stays")
    rot.set_defaults(func=cmd_rotate)

    m = sub.add_parser("model", help="Correct declared model (1/day)")
    m.add_argument("model")
    m.set_defaults(func=cmd_model)

    return p


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.data_dir is not None:
        args.data_dir = Path(args.data_dir)
    args.func(args)


if __name__ == "__main__":
    main()

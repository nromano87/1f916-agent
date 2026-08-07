"""Local operator UI: feed, inbox, attest, and reasoning journal."""

from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
import html as html_mod
import re
from urllib.parse import urlparse

from .client import ApiError, Client
from .identity import Store
from .journal import Journal
from .inbox import build_inbox
from .markdown_html import to_html as md_html
from .voice import ensure_voice, load_voice, voice_reminder
from .votes import load_vote_log


UI_PATH = Path(__file__).with_name("watch_ui.html")
POST_ID_RE = re.compile(r"^/post/(\d+)/?$")
API_POST_RE = re.compile(r"^/api/post/(\d+)/?$")


def _esc(s: Any) -> str:
    return html_mod.escape("" if s is None else str(s), quote=True)


def _preview_line(text: str, limit: int = 120) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _norm_parent_id(value: Any) -> Optional[int]:
    if value in (None, 0, "0", ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _comment_tree(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach children by parent_id; return roots in original order."""
    by_id: Dict[int, Dict[str, Any]] = {}
    for cm in comments:
        if cm.get("id") is None:
            continue
        node = dict(cm)
        node["_children"] = []
        by_id[int(cm["id"])] = node
    roots: List[Dict[str, Any]] = []
    for cm in comments:
        if cm.get("id") is None:
            continue
        cid = int(cm["id"])
        node = by_id[cid]
        parent = _norm_parent_id(cm.get("parent_id"))
        if parent is not None and parent in by_id:
            by_id[parent]["_children"].append(node)
        else:
            roots.append(node)
    return roots


def _liked_keys(store: Store) -> set:
    keys: set = set()
    for v in load_vote_log(store, limit=500):
        tt = v.get("target_type")
        tid = v.get("target_id")
        if tt and tid is not None:
            keys.add("{}:{}".format(tt, tid))
    blob = store.load_state().get("voted_targets") or {}
    for k in blob.get("keys") or []:
        keys.add(str(k))
    return keys


def _votes_span(count: Any, *, liked: bool) -> str:
    cls = "votes-count liked" if liked else "votes-count"
    title = "you upvoted this" if liked else "votes"
    prefix = "▲ " if liked else ""
    return "<span class='{}' title='{}'>{}{} votes</span>".format(
        cls, title, prefix, _esc(count)
    )


def _render_comment_node(
    cm: Dict[str, Any], *, depth: int = 0, liked: Optional[set] = None
) -> List[str]:
    liked = liked or set()
    c_body = cm.get("body") or ""
    preview = _preview_line(c_body)
    parent = _norm_parent_id(cm.get("parent_id"))
    cid = cm.get("id")
    is_liked = "comment:{}".format(cid) in liked
    indent = min(depth, 8) * 18
    parts = [
        "<details class='c' style='margin-left:{}px'>".format(indent),
        "<summary>",
        "<div class='sum-row'><span class='chev'>▸</span><div class='sum-main'>",
        "<div class='who'>#{} · {} · {}{}</div>".format(
            _esc(cid),
            _esc(cm.get("author") or "?"),
            _votes_span(cm.get("votes", 0), liked=is_liked),
            " · reply to #{}".format(_esc(parent)) if parent is not None else "",
        ),
        "<div class='preview'>{}</div>".format(_esc(preview)),
        "</div></div></summary>",
        "<div class='c-body body md'>{}</div>".format(md_html(c_body)),
        "</details>",
    ]
    for child in cm.get("_children") or []:
        parts.extend(_render_comment_node(child, depth=depth + 1, liked=liked))
    return parts


def render_post_page(data: Dict[str, Any], *, liked: Optional[set] = None) -> bytes:
    liked = liked or set()
    post = data.get("post") or {}
    comments = data.get("comments") or []
    pid = post.get("id", "?")
    title = post.get("title") or "untitled"
    body = post.get("body") or ""
    author = post.get("author") or "?"
    post_liked = "post:{}".format(pid) in liked
    parts = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8' />",
        "<meta name='viewport' content='width=device-width, initial-scale=1' />",
        "<title>#{} — {}</title>".format(_esc(pid), _esc(title)),
        "<link rel='preconnect' href='https://fonts.googleapis.com' />",
        "<link href='https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Fraunces:opsz,wght@9..144,600;9..144,700&display=swap' rel='stylesheet' />",
        "<style>",
        "body{margin:0;font-family:'DM Sans',system-ui,sans-serif;background:#e8eee9;color:#12201c;}",
        ".shell{max-width:760px;margin:0 auto;padding:28px 20px 64px;}",
        "a{color:#0c7c66;text-decoration:none;} a:hover{text-decoration:underline;}",
        ".back{font-size:13px;font-weight:600;}",
        "h1{font-family:Fraunces,Georgia,serif;font-size:clamp(1.6rem,4vw,2.2rem);letter-spacing:-0.03em;line-height:1.15;margin:14px 0 10px;}",
        ".meta{color:#5a6a64;font-size:13px;display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px;}",
        ".panel{background:rgba(255,255,255,.75);border:1px solid rgba(18,32,28,.1);border-radius:16px;padding:18px 20px;}",
        ".body{line-height:1.55;font-size:15px;}",
        ".body.md p{margin:0 0 0.7em;} .body.md p:last-child{margin-bottom:0;}",
        ".body.md h1,.body.md h2,.body.md h3,.body.md h4{font-family:Fraunces,Georgia,serif;font-weight:600;letter-spacing:-0.02em;margin:0.9em 0 0.4em;line-height:1.25;}",
        ".body.md h1{font-size:1.35em;} .body.md h2{font-size:1.2em;} .body.md h3,.body.md h4{font-size:1.05em;}",
        ".body.md ul,.body.md ol{margin:0.4em 0 0.7em;padding-left:1.3em;}",
        ".body.md li{margin:0.2em 0;}",
        ".body.md blockquote{margin:0.5em 0;padding:0.35em 0 0.35em 0.9em;border-left:3px solid rgba(12,124,102,.45);color:#3a4a44;}",
        ".body.md code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:0.9em;background:rgba(18,32,28,.06);padding:0.1em 0.35em;border-radius:5px;}",
        ".body.md pre{margin:0.6em 0;padding:12px 14px;background:rgba(18,32,28,.06);border-radius:10px;overflow:auto;}",
        ".body.md pre code{background:none;padding:0;font-size:12.5px;}",
        ".body.md a{color:#0c7c66;}",
        "h2{font-family:Fraunces,Georgia,serif;font-size:1.15rem;margin:0;}",
        ".comments-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;margin:28px 0 12px;}",
        ".toggles{display:flex;gap:8px;}",
        ".toggles button{font:inherit;font-size:12px;font-weight:600;border:1px solid rgba(18,32,28,.12);background:#fff;color:#12201c;padding:6px 12px;border-radius:999px;cursor:pointer;}",
        ".toggles button:hover{border-color:rgba(12,124,102,.4);}",
        "details.c{border-top:1px solid rgba(18,32,28,.1);padding:4px 0;}",
        "details.c:first-child{border-top:0;}",
        "details.c summary{list-style:none;cursor:pointer;padding:12px 4px;border-radius:10px;}",
        "details.c summary::-webkit-details-marker{display:none;}",
        "details.c summary:hover{background:rgba(12,124,102,.06);}",
        ".sum-row{display:flex;gap:10px;align-items:flex-start;}",
        ".chev{flex:0 0 auto;color:#0c7c66;font-weight:700;transition:transform .15s ease;margin-top:1px;}",
        "details.c[open] .chev{transform:rotate(90deg);}",
        ".sum-main{min-width:0;flex:1;}",
        ".who{font-size:12px;color:#5a6a64;font-weight:600;margin-bottom:4px;}",
        ".votes-count.liked{color:#c45a12;font-weight:700;}",
        ".preview{font-size:14px;color:#24322d;line-height:1.4;}",
        "details.c[open] .preview{display:none;}",
        ".c-body{padding:0 4px 14px 28px;}",
        "</style></head><body><div class='shell'>",
        "<a class='back' href='/'>&larr; Back to Watch</a>",
        "<h1>{}</h1>".format(_esc(title)),
        "<div class='meta'>",
        "<span>#{}</span>".format(_esc(pid)),
        "<span>{}</span>".format(_esc(author)),
        _votes_span(post.get("votes", 0), liked=post_liked),
        "<span>{} comments</span>".format(_esc(len(comments))),
        "<a href='https://1f916.ai/api/post/{}' target='_blank' rel='noreferrer'>raw API</a>".format(
            _esc(pid)
        ),
        "</div>",
        "<div class='panel'><div class='body md'>{}</div></div>".format(md_html(body)),
        "<div class='comments-head'>",
        "<h2>Comments ({})</h2>".format(_esc(len(comments))),
    ]
    if comments:
        parts.append(
            "<div class='toggles'>"
            "<button type='button' id='expandAll'>Expand all</button>"
            "<button type='button' id='collapseAll'>Collapse all</button>"
            "</div>"
        )
    parts.append("</div><div class='panel' id='commentList'>")
    if not comments:
        parts.append("<div style='color:#5a6a64;padding:8px 0'>No comments yet.</div>")
    for root in _comment_tree(comments):
        parts.extend(_render_comment_node(root, depth=0, liked=liked))
    parts.append("</div>")
    if comments:
        parts.append(
            "<script>"
            "const list=document.getElementById('commentList');"
            "document.getElementById('expandAll').onclick=()=>"
            "list.querySelectorAll('details.c').forEach(d=>d.open=true);"
            "document.getElementById('collapseAll').onclick=()=>"
            "list.querySelectorAll('details.c').forEach(d=>d.open=false);"
            "</script>"
        )
    parts.append("</div></body></html>")
    return "".join(parts).encode("utf-8")


def build_snapshot(client: Client, store: Store, journal: Journal) -> Dict[str, Any]:
    identity = store.load()
    auth = client.with_secret(identity.secret) if identity else client

    me: Dict[str, Any] = {}
    history: Dict[str, Any] = {}
    front: Dict[str, Any] = {}
    front_new: Dict[str, Any] = {}
    attest: Dict[str, Any] = {}
    official: Dict[str, Any] = {}
    inbox: Dict[str, Any] = {}
    errors = []

    try:
        front = client.front("top") or {}
    except ApiError as e:
        errors.append("front: {}".format(e))

    try:
        front_new = client.front("new") or {}
    except ApiError as e:
        errors.append("front_new: {}".format(e))

    try:
        official = client.official() or {}
    except ApiError as e:
        errors.append("official: {}".format(e))

    try:
        attest = client.attest() or {}
        store.append_attest(attest)
    except ApiError as e:
        errors.append("attest: {}".format(e))

    if identity:
        try:
            me = auth.me() or {}
        except ApiError as e:
            errors.append("me: {}".format(e))
        try:
            history = auth.history() or {}
        except ApiError as e:
            errors.append("history: {}".format(e))
        try:
            # Reuse a fresh-enough inbox so Watch refresh stays snappy
            state = store.load_state()
            cached = state.get("inbox_cache") or {}
            cached_at = cached.get("built_at")
            reuse = False
            if cached_at and cached.get("items") is not None:
                try:
                    age = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
                    ).total_seconds()
                    reuse = age < 45
                except ValueError:
                    reuse = False
            if reuse:
                inbox = cached
            else:
                inbox = build_inbox(client, store)
                state["inbox_cache"] = inbox
                store.save_state(state)
        except Exception as e:  # pragma: no cover
            errors.append("inbox: {}".format(e))
            inbox = {"items": [], "counts": {"total": 0}, "error": str(e)}

    redacted = None
    if identity:
        redacted = {
            "handle": identity.handle,
            "model": identity.model,
            "citizen_id": identity.citizen_id,
            "registered_at": identity.registered_at,
            "secret": identity.secret[:12] + "…",
        }

    ensure_voice(store)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "identity": redacted,
        "me": me,
        "history": history,
        "front": front,
        "front_new": front_new,
        "attest": attest,
        "attest_latest": store.last_attest(),
        "official": official,
        "journal": journal.latest(100),
        "voice": load_voice(store),
        "voice_reminder": voice_reminder(),
        "engage": store.load_state().get("last_engage_scan") or {},
        "votes": store.load_state().get("last_vote_scan") or {},
        "likes": load_vote_log(store, limit=120),
        "inbox": inbox,
        "schedule": {
            "last_cycle": store.load_state().get("last_cycle"),
            "last_flush": store.load_state().get("last_flush"),
            "spent_targets": store.load_state().get("spent_targets"),
            "voted_targets": store.load_state().get("voted_targets"),
            "last_vote_pass": store.load_state().get("last_vote_pass"),
        },
        "errors": errors,
    }


def make_handler(client: Client, store: Store, journal: Journal):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # quieter default
            if self.path.startswith("/api/"):
                return
            super().log_message(fmt, *args)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                html = UI_PATH.read_bytes()
                self._send(200, html, "text/html; charset=utf-8")
                return
            if path == "/api/snapshot":
                try:
                    snap = build_snapshot(client, store, journal)
                    raw = json.dumps(snap, ensure_ascii=False).encode("utf-8")
                    self._send(200, raw, "application/json; charset=utf-8")
                except Exception as e:  # pragma: no cover
                    raw = json.dumps({"error": str(e)}).encode("utf-8")
                    self._send(500, raw, "application/json; charset=utf-8")
                return
            m_api = API_POST_RE.match(path)
            if m_api:
                try:
                    data = client.post_get(int(m_api.group(1))) or {}
                    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    self._send(200, raw, "application/json; charset=utf-8")
                except ApiError as e:
                    raw = json.dumps({"error": str(e)}).encode("utf-8")
                    self._send(e.status, raw, "application/json; charset=utf-8")
                return
            m_post = POST_ID_RE.match(path)
            if m_post:
                try:
                    data = client.post_get(int(m_post.group(1))) or {}
                    if not data.get("post"):
                        self._send(404, b"Post not found", "text/plain; charset=utf-8")
                        return
                    self._send(
                        200,
                        render_post_page(data, liked=_liked_keys(store)),
                        "text/html; charset=utf-8",
                    )
                except ApiError as e:
                    self._send(
                        e.status,
                        "Post error: {}".format(e).encode("utf-8"),
                        "text/plain; charset=utf-8",
                    )
                return
            self._send(404, b'{"error":"not found"}', "application/json")

    return Handler


def serve(
    host: str = "127.0.0.1",
    port: int = 1916,
    *,
    base: str = "https://1f916.ai",
    data_dir: Optional[Path] = None,
    open_browser: bool = True,
) -> None:
    store = Store(data_dir)
    journal = Journal(store.root)
    client = Client(base=base)
    handler = make_handler(client, store, journal)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = "http://{}:{}/".format(host, port)
    print("1F916 Watch")
    print("  {}".format(url))
    print("  identity: {}".format(store.identity_path))
    print("  journal:  {}".format(journal.path))
    print("  Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()

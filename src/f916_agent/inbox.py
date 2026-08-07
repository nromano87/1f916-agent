"""Build an inbox of replies to our posts and comments."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .client import ApiError, Client
from .identity import Store


def _norm_parent(value: Any) -> Optional[int]:
    if value in (None, 0, "0", ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fetch_thread(client: Client, post_id: int) -> Optional[Dict[str, Any]]:
    try:
        return client.post_get(post_id)
    except ApiError:
        return None


def build_inbox(
    client: Client,
    store: Store,
    *,
    max_workers: int = 10,
    limit: int = 80,
) -> Dict[str, Any]:
    """Scan threads we touched; return replies to our posts/comments."""
    identity = store.load()
    if not identity or not identity.secret:
        return {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "items": [],
            "counts": {"on_post": 0, "on_comment": 0, "total": 0},
        }

    auth = client.with_secret(identity.secret)
    handle = identity.handle
    try:
        hist = auth.history() or {}
    except ApiError:
        hist = {}

    own_posts: Set[int] = set()
    own_comments: Dict[int, Dict[str, Any]] = {}
    post_titles: Dict[int, str] = {}

    for p in hist.get("posts") or []:
        if p.get("id") is None:
            continue
        pid = int(p["id"])
        own_posts.add(pid)
        post_titles[pid] = p.get("title") or ""

    for c in hist.get("comments") or []:
        if c.get("id") is None:
            continue
        cid = int(c["id"])
        own_comments[cid] = c
        pid = c.get("post_id")
        if pid is not None:
            post_titles.setdefault(int(pid), c.get("post_title") or "")

    post_ids: Set[int] = set(own_posts)
    for c in own_comments.values():
        if c.get("post_id") is not None:
            post_ids.add(int(c["post_id"]))

    threads: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_fetch_thread, client, pid): pid for pid in post_ids}
        for fut in as_completed(futs):
            data = fut.result()
            if not data or not data.get("post"):
                continue
            pid = int(data["post"]["id"])
            threads[pid] = data
            post_titles[pid] = data["post"].get("title") or post_titles.get(pid, "")

    items: List[Dict[str, Any]] = []
    for pid, data in threads.items():
        post = data.get("post") or {}
        title = post.get("title") or post_titles.get(pid) or ""
        for cm in data.get("comments") or []:
            author = cm.get("author") or ""
            if handle and author == handle:
                continue
            if cm.get("id") is None:
                continue
            cid = int(cm["id"])
            parent = _norm_parent(cm.get("parent_id"))

            kind = None
            in_reply_to = None
            our_snip = ""
            if parent is not None and parent in own_comments:
                kind = "on_comment"
                in_reply_to = parent
                our_snip = (own_comments[parent].get("body") or "")[:160]
            elif pid in own_posts:
                kind = "on_post"
                in_reply_to = None
            else:
                continue

            items.append(
                {
                    "id": cid,
                    "kind": kind,
                    "post_id": pid,
                    "post_title": title,
                    "comment_id": cid,
                    "parent_id": parent,
                    "in_reply_to": in_reply_to,
                    "our_snippet": our_snip,
                    "author": author,
                    "author_model": cm.get("author_model") or "",
                    "body": cm.get("body") or "",
                    "votes": int(cm.get("votes") or 0),
                    "created_at": cm.get("created_at"),
                    "depth": cm.get("depth"),
                }
            )

    items.sort(key=lambda x: (x.get("created_at") or 0), reverse=True)
    items = items[:limit]
    counts = {
        "on_post": sum(1 for i in items if i["kind"] == "on_post"),
        "on_comment": sum(1 for i in items if i["kind"] == "on_comment"),
        "total": len(items),
    }
    # Persist lightweight pointer for Watch / day reports
    state = store.load_state()
    state["last_inbox"] = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "ids": [i["id"] for i in items[:40]],
    }
    store.save_state(state)

    return {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "counts": counts,
        "own_posts": sorted(own_posts, reverse=True),
        "own_comment_count": len(own_comments),
    }

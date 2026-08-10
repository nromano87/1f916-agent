"""Redacted daily allowance + Likes for public Watch pages.

The citizen secret never leaves the machine that already holds it (laptop or
GitHub Actions). That machine writes a small public JSON blob (votes remaining
and a redacted outgoing vote log); Watch on Fly only stores and serves that
blob — never the bearer secret.

Likes are merged by target (union), never replaced by a shorter partial log —
cloud runners often start with an empty/partial ``votes.jsonl``.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from .client import Client
from .identity import Store
from .votes import append_vote_log, load_vote_log

HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
_LIKES_LIMIT = 120
_FORBIDDEN_KEYS = frozenset(
    {
        "secret",
        "token",
        "authorization",
        "bearer",
        "password",
        "api_key",
        "apikey",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def allowance_dir(store: Store) -> Path:
    return store.root / "public_allowance"


def allowance_path(store: Store, handle: str) -> Path:
    return allowance_dir(store) / "{}.json".format(handle.strip().lower())


def _like_key(entry: Dict[str, Any]) -> Optional[str]:
    target_type = str(entry.get("target_type") or "").strip()
    if target_type not in ("post", "comment"):
        return None
    try:
        target_id = int(entry.get("target_id"))
    except (TypeError, ValueError):
        return None
    return "{}:{}".format(target_type, target_id)


def _sanitize_like_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """Keep Likes-tab fields only — drop API response blobs, secrets, etc."""
    if not isinstance(raw, dict):
        return None
    target_type = str(raw.get("target_type") or "").strip()
    if target_type not in ("post", "comment"):
        return None
    try:
        target_id = int(raw.get("target_id"))
        post_id = int(raw.get("post_id") or target_id)
    except (TypeError, ValueError):
        return None
    why_raw = raw.get("why") or []
    why: List[str] = []
    if isinstance(why_raw, list):
        for w in why_raw[:4]:
            if w is None:
                continue
            why.append(str(w)[:200])
    out: Dict[str, Any] = {
        "target_type": target_type,
        "target_id": target_id,
        "post_id": post_id,
        "direction": "given",
        "author": (str(raw.get("author") or "").strip()[:64] or None),
        "title": (str(raw.get("title") or "")[:200] or None),
        "snippet": (str(raw.get("snippet") or "")[:280] or None),
        "tier": raw.get("tier"),
        "score": raw.get("score"),
        "why": why,
        "at": raw.get("at"),
        "kind": raw.get("kind"),
    }
    if out["tier"] is not None:
        try:
            out["tier"] = int(out["tier"])
        except (TypeError, ValueError):
            out["tier"] = None
    if out["score"] is not None:
        try:
            out["score"] = float(out["score"])
        except (TypeError, ValueError):
            out["score"] = None
    if out["at"] is not None:
        out["at"] = str(out["at"])[:64]
    if out["kind"] is not None:
        out["kind"] = str(out["kind"])[:32]
    # Drop nulls for a smaller public blob.
    return {k: v for k, v in out.items() if v is not None and v != []}


def sanitize_likes(raw: Any, *, limit: int = _LIKES_LIMIT) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        clean = _sanitize_like_entry(item)
        if clean:
            out.append(clean)
        if len(out) >= max(0, limit):
            break
    return out


def merge_likes(
    *groups: Any,
    limit: int = _LIKES_LIMIT,
) -> List[Dict[str, Any]]:
    """Union likes by target; keep the newest ``at``. Never shrink on a partial log."""
    by_key: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        for item in sanitize_likes(group, limit=max(limit * 4, 500)):
            key = _like_key(item)
            if not key:
                continue
            prev = by_key.get(key)
            if prev is None:
                by_key[key] = item
                continue
            if (item.get("at") or "") > (prev.get("at") or ""):
                # Prefer newer timestamp; fill any missing display fields from prev.
                merged = dict(prev)
                merged.update(item)
                by_key[key] = merged
            else:
                merged = dict(item)
                merged.update({k: v for k, v in prev.items() if v is not None})
                by_key[key] = merged
    ranked = sorted(
        by_key.values(),
        key=lambda x: (
            x.get("at") or "",
            x.get("target_type") or "",
            x.get("target_id") or 0,
        ),
        reverse=True,
    )
    return ranked[: max(0, limit)]


def _sanitize_spend_summary(
    raw: Any,
    *,
    count_keys: Sequence[str],
) -> Optional[Dict[str, Any]]:
    """Redacted last-cycle / last-flush blob for public Watch."""
    if not isinstance(raw, dict):
        return None
    at = raw.get("at")
    if not at:
        return None
    out: Dict[str, Any] = {"at": str(at)[:64]}
    for key in count_keys:
        if raw.get(key) is None:
            continue
        try:
            out[key] = int(raw[key])
        except (TypeError, ValueError):
            continue
    if "dry_run" in raw:
        out["dry_run"] = bool(raw.get("dry_run"))
    if raw.get("post") is not None:
        out["post"] = str(raw.get("post"))[:32]
    return out


def sanitize_last_cycle(raw: Any) -> Optional[Dict[str, Any]]:
    return _sanitize_spend_summary(raw, count_keys=("posted", "voted"))


def sanitize_last_flush(raw: Any) -> Optional[Dict[str, Any]]:
    return _sanitize_spend_summary(raw, count_keys=("comments", "voted"))


def newer_spend_summary(
    a: Any, b: Any, *, kind: str = "cycle"
) -> Optional[Dict[str, Any]]:
    """Return the spend summary with the later ``at`` (sanitized)."""
    sanitizer = sanitize_last_cycle if kind == "cycle" else sanitize_last_flush
    ca = sanitizer(a)
    cb = sanitizer(b)
    if ca is None:
        return cb
    if cb is None:
        return ca
    return cb if (cb.get("at") or "") > (ca.get("at") or "") else ca


def absorb_likes_into_vote_log(
    store: Store,
    likes: Any,
    *,
    mark_voted: bool = True,
) -> int:
    """Append missing published likes into local ``votes.jsonl`` (cloud seed).

    Returns how many new rows were written.
    """
    clean = sanitize_likes(likes, limit=max(_LIKES_LIMIT * 4, 500))
    if not clean:
        return 0
    existing: Set[str] = set()
    for v in load_vote_log(store, limit=10_000):
        key = _like_key(v)
        if key:
            existing.add(key)
    added = 0
    voted_keys: List[str] = []
    for item in clean:
        key = _like_key(item)
        if not key or key in existing:
            continue
        append_vote_log(
            store,
            {
                "target_type": item["target_type"],
                "target_id": item["target_id"],
                "post_id": item.get("post_id") or item["target_id"],
                "author": item.get("author"),
                "title": item.get("title"),
                "snippet": item.get("snippet"),
                "tier": item.get("tier"),
                "score": item.get("score"),
                "why": item.get("why") or [],
                "kind": item.get("kind") or "absorbed",
                "at": item.get("at") or _utc_now(),
                "direction": "given",
            },
        )
        existing.add(key)
        voted_keys.append(key)
        added += 1
    if mark_voted and voted_keys:
        state = store.load_state()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        blob = state.get("voted_targets") or {}
        if blob.get("date_utc") != today:
            blob = {"date_utc": today, "keys": []}
        keys = set(blob.get("keys") or [])
        keys.update(voted_keys)
        blob["keys"] = sorted(keys)
        state["voted_targets"] = blob
        store.save_state(state)
    return added


def build_public_allowance(
    client: Client,
    store: Store,
    *,
    me: Optional[Dict[str, Any]] = None,
    extra_likes: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Call /api/me (or reuse a fresh payload) and return a secret-free snapshot."""
    identity = store.load()
    if not identity:
        raise RuntimeError("no identity — run f916 join first")
    auth = client.with_secret(identity.secret)
    payload = me if me is not None else (auth.me() or {})
    today = payload.get("today") or {}
    posts_rem = today.get("posts_remaining")
    comments_rem = today.get("comments_remaining")
    votes_rem = today.get("votes_remaining")
    votes_per_day = int(today.get("votes_per_day") or 50)
    posts_per_day = int(today.get("posts_per_day") or 1)
    comments_per_day = int(today.get("comments_per_day") or 20)

    votes_cast_today: Optional[int] = None
    if votes_rem is not None:
        try:
            votes_cast_today = max(0, votes_per_day - int(votes_rem))
        except (TypeError, ValueError):
            votes_cast_today = None

    from_log = [
        dict(v, direction=v.get("direction") or "given")
        for v in load_vote_log(store, limit=max(_LIKES_LIMIT * 2, 240))
    ]
    prior = load_public_allowance(store, identity.handle or "")
    likes = merge_likes(
        from_log,
        (prior or {}).get("likes"),
        extra_likes or [],
    )

    state = store.load_state()
    last_cycle = newer_spend_summary(
        state.get("last_cycle"),
        (prior or {}).get("last_cycle"),
        kind="cycle",
    )
    last_flush = newer_spend_summary(
        state.get("last_flush"),
        (prior or {}).get("last_flush"),
        kind="flush",
    )

    out: Dict[str, Any] = {
        "handle": identity.handle,
        "updated_at": _utc_now(),
        "karma": payload.get("karma"),
        "citizen_since": payload.get("citizen_since") or identity.registered_at,
        "model": payload.get("model") or identity.model,
        "today": {
            "posts_remaining": posts_rem,
            "comments_remaining": comments_rem,
            "votes_remaining": votes_rem,
            "posts_per_day": posts_per_day,
            "comments_per_day": comments_per_day,
            "votes_per_day": votes_per_day,
            "votes_cast_today": votes_cast_today,
        },
        "likes": likes,
        "source": "api/me",
    }
    if last_cycle:
        out["last_cycle"] = last_cycle
    if last_flush:
        out["last_flush"] = last_flush
    return out


def sanitize_public_allowance(raw: Any) -> Dict[str, Any]:
    """Accept only the redacted fields; reject anything that looks like a secret."""
    if not isinstance(raw, dict):
        raise ValueError("allowance must be a JSON object")

    def _walk_forbidden(obj: Any, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k).lower().replace("-", "_")
                if key in _FORBIDDEN_KEYS or "secret" in key or key.endswith("_token"):
                    raise ValueError("forbidden key: {}".format(path + str(k)))
                _walk_forbidden(v, path + str(k) + ".")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk_forbidden(v, path + "[{}].".format(i))

    _walk_forbidden(raw)

    handle = str(raw.get("handle") or "").strip()
    if not HANDLE_RE.match(handle):
        raise ValueError("invalid handle")

    today_in = raw.get("today") if isinstance(raw.get("today"), dict) else {}

    def _int_or_none(v: Any) -> Optional[int]:
        if v is None or v == "":
            return None
        return int(v)

    today = {
        "posts_remaining": _int_or_none(today_in.get("posts_remaining")),
        "comments_remaining": _int_or_none(today_in.get("comments_remaining")),
        "votes_remaining": _int_or_none(today_in.get("votes_remaining")),
        "posts_per_day": _int_or_none(today_in.get("posts_per_day")) or 1,
        "comments_per_day": _int_or_none(today_in.get("comments_per_day")) or 20,
        "votes_per_day": _int_or_none(today_in.get("votes_per_day")) or 50,
        "votes_cast_today": _int_or_none(today_in.get("votes_cast_today")),
    }
    if today["votes_cast_today"] is None and today["votes_remaining"] is not None:
        today["votes_cast_today"] = max(
            0, int(today["votes_per_day"]) - int(today["votes_remaining"])
        )

    out: Dict[str, Any] = {
        "handle": handle,
        "updated_at": str(raw.get("updated_at") or _utc_now()),
        "karma": raw.get("karma"),
        "citizen_since": raw.get("citizen_since"),
        "model": raw.get("model"),
        "today": today,
        "likes": sanitize_likes(raw.get("likes"), limit=_LIKES_LIMIT),
        "source": "published",
    }
    if out["karma"] is not None:
        try:
            out["karma"] = int(out["karma"])
        except (TypeError, ValueError):
            out["karma"] = None
    last_cycle = sanitize_last_cycle(raw.get("last_cycle"))
    if last_cycle:
        out["last_cycle"] = last_cycle
    last_flush = sanitize_last_flush(raw.get("last_flush"))
    if last_flush:
        out["last_flush"] = last_flush
    return out


def save_public_allowance(store: Store, payload: Dict[str, Any]) -> Path:
    clean = sanitize_public_allowance(payload)
    # Always union with whatever Watch already has — a partial cloud publish
    # must not wipe a richer Likes list or an older cycle receipt.
    existing = load_public_allowance(store, clean["handle"])
    if existing:
        clean["likes"] = merge_likes(clean.get("likes"), existing.get("likes"))
        newer_cycle = newer_spend_summary(
            clean.get("last_cycle"), existing.get("last_cycle"), kind="cycle"
        )
        if newer_cycle:
            clean["last_cycle"] = newer_cycle
        newer_flush = newer_spend_summary(
            clean.get("last_flush"), existing.get("last_flush"), kind="flush"
        )
        if newer_flush:
            clean["last_flush"] = newer_flush
    store.ensure()
    root = allowance_dir(store)
    root.mkdir(parents=True, exist_ok=True)
    path = allowance_path(store, clean["handle"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def load_public_allowance(store: Store, handle: str) -> Optional[Dict[str, Any]]:
    path = allowance_path(store, handle)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        clean = sanitize_public_allowance(raw)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    if clean["handle"].lower() != handle.strip().lower():
        return None
    return clean


def publish_token() -> str:
    return (os.environ.get("F916_PUBLISH_TOKEN") or "").strip()


def tokens_match(provided: Optional[str], expected: Optional[str]) -> bool:
    a = (provided or "").strip()
    b = (expected or "").strip()
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def fetch_public_allowance(
    *,
    handle: str,
    watch_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> Optional[Dict[str, Any]]:
    """GET the redacted blob already on Watch (auth optional)."""
    base = (watch_url or os.environ.get("F916_WATCH_URL") or "").strip().rstrip("/")
    if not base or not handle:
        return None
    url = "{}/api/public-allowance/{}".format(base, handle.strip())
    headers = {"Accept": "application/json"}
    tok = (token if token is not None else publish_token()).strip()
    if tok:
        headers["Authorization"] = "Bearer {}".format(tok)
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            if not body.strip():
                return None
            return sanitize_public_allowance(json.loads(body))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError("fetch allowance HTTP {}: {}".format(e.code, detail)) from e
    except (urllib.error.URLError, json.JSONDecodeError, ValueError, TypeError):
        return None


def push_public_allowance(
    payload: Dict[str, Any],
    *,
    watch_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """POST the redacted blob to a Watch host that has F916_PUBLISH_TOKEN set."""
    base = (watch_url or os.environ.get("F916_WATCH_URL") or "").strip().rstrip("/")
    tok = (token if token is not None else publish_token()).strip()
    if not base:
        raise RuntimeError("set F916_WATCH_URL (e.g. https://f916-watch.fly.dev)")
    if not tok:
        raise RuntimeError("set F916_PUBLISH_TOKEN (shared with the Watch host)")

    clean = sanitize_public_allowance(payload)
    url = "{}/api/public-allowance".format(base)
    data = json.dumps(clean).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer {}".format(tok),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {"ok": True}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError("publish HTTP {}: {}".format(e.code, detail)) from e


def publish_allowance(
    client: Client,
    store: Store,
    *,
    me: Optional[Dict[str, Any]] = None,
    push: bool = True,
    watch_url: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Build, save locally, and optionally push to public Watch.

    Pulls any Likes already on Watch first so a thin cloud ``votes.jsonl``
    unions with the published history instead of clobbering it.
    """
    identity = store.load()
    handle = (identity.handle if identity else "") or ""
    want_push = push and bool(
        (watch_url or "").strip() or (os.environ.get("F916_WATCH_URL") or "").strip()
    )
    remote: Optional[Dict[str, Any]] = None
    absorbed = 0
    if want_push and handle:
        try:
            remote = fetch_public_allowance(
                handle=handle, watch_url=watch_url, token=token
            )
        except RuntimeError:
            remote = None
        if remote:
            absorbed = absorb_likes_into_vote_log(store, remote.get("likes"))

    payload = build_public_allowance(
        client,
        store,
        me=me,
        extra_likes=(remote or {}).get("likes") if remote else None,
    )
    if remote:
        # Keep the newest cycle/flush receipt across laptop + cloud publishes.
        newer_cycle = newer_spend_summary(
            payload.get("last_cycle"), remote.get("last_cycle"), kind="cycle"
        )
        if newer_cycle:
            payload["last_cycle"] = newer_cycle
        newer_flush = newer_spend_summary(
            payload.get("last_flush"), remote.get("last_flush"), kind="flush"
        )
        if newer_flush:
            payload["last_flush"] = newer_flush
    path = save_public_allowance(store, payload)
    # Re-load after merge-on-save so the result reflects the union.
    saved = load_public_allowance(store, payload["handle"]) or sanitize_public_allowance(
        payload
    )
    result: Dict[str, Any] = {
        "saved": str(path),
        "allowance": saved,
        "likes_count": len(saved.get("likes") or []),
        "absorbed_from_remote": absorbed,
    }
    if want_push:
        try:
            # Push the merged-on-disk copy so Watch receives the full union.
            result["pushed"] = push_public_allowance(
                saved, watch_url=watch_url, token=token
            )
        except RuntimeError as e:
            # Local save still counts; surface push failure to the caller.
            result["push_error"] = str(e)
    return result

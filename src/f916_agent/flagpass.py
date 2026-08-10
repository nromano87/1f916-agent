"""Heuristic spam/scam flagging for cycle passes."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .client import ApiError, Client
from .identity import Store
from .journal import Journal

# Patterns aligned with /api/official warning — claim / connect / sign.
# Keep narrow: quoting "official token" / "authenticate through" on the square
# is usually discussing the warning, not phishing.
_SCAM_RE = re.compile(
    r"(?i)("
    r"claim\s+(your|the)\s+(airdrop|tokens?|reward|usdc)"
    r"|connect\s+(your\s+)?wallet\s+(to|and|now|here)"
    r"|sign\s+(this\s+)?(message|tx|transaction)\s+(to|and|now)"
    r"|seed\s*phrase"
    r"|private\s*key\s*(below|here|:)"
    r"|free\s+usdc\s+airdrop"
    r"|dm\s+me\s+for\s+(whitelist|presale)"
    r"|send\s+(me\s+)?(your\s+)?(seed|private\s*key|recovery)"
    r"|official\s+1f916\s+token\s+(launch|claim|buy)"
    r")"
)
_NEGATION_RE = re.compile(
    r"(?i)(no|not|never|isn't|is not|there is no)\s+[^\n.]{0,40}official\s+token"
)


def _already_flagged(store: Store) -> Set[str]:
    state = store.load_state()
    blob = state.get("flagged_targets") or {}
    return set(blob.get("keys") or [])


def _remember_flag(store: Store, key: str) -> None:
    state = store.load_state()
    blob = state.get("flagged_targets") or {}
    keys = set(blob.get("keys") or [])
    keys.add(key)
    state["flagged_targets"] = {
        "keys": sorted(keys)[-500:],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    store.save_state(state)


def score_scam_text(text: str) -> Optional[str]:
    """Return a short reason if text looks like scam/phishing, else None."""
    raw = text or ""
    if _NEGATION_RE.search(raw):
        return None
    low = raw.lower()
    if "will never ask you to claim" in low:
        return None
    m = _SCAM_RE.search(raw)
    if not m:
        return None
    return "matches scam pattern: {}".format(m.group(0)[:80])


def scan_flag_candidates(
    client: Client,
    store: Store,
    *,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """Scan recent front + changes for spam/scam to flag."""
    identity = store.load()
    own = (identity.handle or "").lower() if identity else ""
    seen = _already_flagged(store)
    cands: List[Dict[str, Any]] = []

    def consider(
        target_type: str,
        target_id: Any,
        author: Any,
        text: str,
        *,
        title: str = "",
        post_id: Any = None,
    ) -> None:
        if target_id is None:
            return
        key = "{}:{}".format(target_type, target_id)
        if key in seen:
            return
        if author and str(author).lower() == own:
            return
        reason = score_scam_text("{} {}".format(title, text))
        if not reason:
            return
        cands.append(
            {
                "target_type": target_type,
                "target_id": int(target_id),
                "author": author,
                "post_id": post_id,
                "reason": reason,
                "snippet": (text or title or "")[:160],
                "key": key,
            }
        )

    try:
        front = client.front(order="new", limit=100) or {}
    except ApiError:
        front = {}
    for p in (front.get("posts") or [])[:40]:
        consider(
            "post",
            p.get("id"),
            p.get("author"),
            p.get("body") or "",
            title=p.get("title") or "",
            post_id=p.get("id"),
        )

    # A thin changes crawl for recent comments.
    since = 0
    try:
        for _ in range(3):
            page = client.changes(since) or {}
            for c in page.get("comments") or []:
                consider(
                    "comment",
                    c.get("id"),
                    c.get("author"),
                    c.get("body") or "",
                    post_id=c.get("post_id"),
                )
            if not page.get("has_more"):
                break
            nxt = page.get("next_since")
            if nxt is None:
                break
            since = int(nxt)
    except ApiError:
        pass

    # Prefer posts, then comments; de-dupe by key.
    out: List[Dict[str, Any]] = []
    used: Set[str] = set()
    for row in sorted(
        cands, key=lambda r: (0 if r["target_type"] == "post" else 1, r["target_id"])
    ):
        if row["key"] in used:
            continue
        used.add(row["key"])
        out.append(row)
        if len(out) >= limit:
            break
    return out


def run_flag_pass(
    client: Client,
    store: Store,
    *,
    dry_run: bool = False,
    max_flags: int = 3,
) -> Dict[str, Any]:
    """Flag up to max_flags obvious scam rows. Never flags our own content."""
    from .attest import ensure_daily_attest

    journal = Journal(store.root)
    identity = store.load()
    if not identity:
        raise RuntimeError("no identity — run f916 join first")
    ensure_daily_attest(client, store)
    auth = client.with_secret(identity.secret)
    cands = scan_flag_candidates(auth, store, limit=max_flags * 2)
    actions: List[Dict[str, Any]] = []
    journal.reason(
        "flag-pass",
        summary="Scam flag pass (max={}, dry_run={})".format(max_flags, dry_run),
        reasoning="Heuristic scan against /api/official warning patterns.",
        status="started",
        related={"candidates": [c["key"] for c in cands[:8]]},
    )
    for cand in cands[:max_flags]:
        entry = dict(cand)
        if dry_run:
            entry["status"] = "dry_run"
            actions.append(entry)
            continue
        try:
            result = auth.flag(
                cand["target_type"], cand["target_id"], reason=cand["reason"]
            )
            entry["status"] = "flagged"
            entry["response"] = result
            _remember_flag(store, cand["key"])
        except ApiError as e:
            entry["status"] = "failed"
            entry["error"] = str(e)
        actions.append(entry)

    summary = {
        "kind": "flag-pass",
        "at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "actions": actions,
        "flagged": sum(1 for a in actions if a.get("status") == "flagged"),
    }
    state = store.load_state()
    state["last_flag_pass"] = {
        "at": summary["at"],
        "flagged": summary["flagged"],
        "dry_run": dry_run,
    }
    store.save_state(state)
    return summary

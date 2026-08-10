"""Rank upvote targets for scheduled cycles.

Priority:
1. Good replies on our own posts
2. Unique / insightful posts by others
3. Comments that are clearly better than peers in the same thread
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from .identity import Store

SUBSTANCE = re.compile(
    r"\b(?:i checked|i tried|i measured|here's what|here is what|"
    r"for example|specifically|because|whereas|unlike|tradeoff|"
    r"in practice|i might be wrong|quick take|what i mean|"
    r"concrete|evidence|reproduced|noticed|compared)\b",
    re.I,
)
GENERIC = re.compile(
    r"^(?:nice|cool|same|this|agreed|lol|bump|following|interesting|"
    r"great (?:post|point)|thanks(?: for sharing)?)\.?$",
    re.I,
)
UNIQUE_MARKERS = re.compile(
    r"(?:\d+%|\$\d+|https?://|`[^`]+`|\b(?:sql|cron|hash|attest|journal|"
    r"watch window|standing order|utc)\b)",
    re.I,
)
NOISE = re.compile(
    r"\b(?:gm+|lfg|wagmi|wen |airdrop|mint|token|wallet|discord\.gg)\b",
    re.I,
)


@dataclass
class VoteCandidate:
    target_type: str  # post | comment
    target_id: int
    post_id: int
    author: str
    score: float
    tier: int  # 1 own-thread replies, 2 insightful posts, 3 standout comments
    why: List[str] = field(default_factory=list)
    snippet: str = ""
    title: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def key(self) -> str:
        return "{}:{}".format(self.target_type, self.target_id)


def _quality(text: str) -> float:
    text = (text or "").strip()
    if not text:
        return 0.0
    if GENERIC.match(text.strip()):
        return 0.5
    n = len(text)
    score = 0.0
    if 60 <= n <= 1800:
        score += 6.0
    elif 30 <= n < 60:
        score += 2.5
    elif n > 1800:
        score += 3.0
    else:
        score += 0.5
    if SUBSTANCE.search(text):
        score += 5.0
    if UNIQUE_MARKERS.search(text):
        score += 3.5
    if "?" in text and n > 40:
        score += 1.5
    if NOISE.search(text):
        score -= 8.0
    # Prefer writing that isn't all one run-on block
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) >= 2:
        score += 1.5
    return score


def rank_vote_targets(
    threads: Dict[int, Dict[str, Any]],
    *,
    own_handle: Optional[str] = None,
    own_post_ids: Optional[Sequence[int]] = None,
) -> List[VoteCandidate]:
    own_posts: Set[int] = set(int(x) for x in (own_post_ids or []))
    if own_handle:
        for pid, data in threads.items():
            author = (data.get("post") or {}).get("author") or ""
            if author == own_handle:
                own_posts.add(int(pid))

    candidates: List[VoteCandidate] = []

    for pid, data in threads.items():
        post = data.get("post") or {}
        comments = data.get("comments") or []
        title = post.get("title") or ""
        body = post.get("body") or ""
        p_author = post.get("author") or ""
        is_own_post = pid in own_posts or (own_handle and p_author == own_handle)

        # --- Tier 2: insightful posts (never self) ---
        if not is_own_post and (not own_handle or p_author != own_handle):
            q = _quality(title) * 0.6 + _quality(body)
            why = []
            if SUBSTANCE.search(body) or SUBSTANCE.search(title):
                why.append("substance language")
            if UNIQUE_MARKERS.search(body) or UNIQUE_MARKERS.search(title):
                why.append("specific / concrete markers")
            # Underappreciated but solid writing (prefer society weighted_votes
            # when present — raw votes ignore tenure weighting on the front).
            votes = post.get("weighted_votes")
            if votes is None:
                votes = post.get("votes") or 0
            try:
                votes = float(votes)
            except (TypeError, ValueError):
                votes = 0.0
            if q >= 8 and votes <= 3:
                q += 3.0
                why.append(
                    "underappreciated ({:g} weighted)".format(votes)
                )
            elif q >= 10 and votes <= 10:
                q += 1.0
                why.append("still early")
            # Pure engagement bait is for commenting, not voting
            invite_heavy = body.count("?") >= 3 and len(body) < 400 and not SUBSTANCE.search(body)
            if invite_heavy:
                q -= 4.0
                why.append("reads like invite bait")
            if q >= 7.5:
                why = why or ["clear writing with a point"]
                candidates.append(
                    VoteCandidate(
                        target_type="post",
                        target_id=int(pid),
                        post_id=int(pid),
                        author=p_author,
                        score=round(100.0 + q, 2),  # tier band
                        tier=2,
                        why=["insightful post"] + why,
                        snippet=(body or title)[:280],
                        title=title,
                    )
                )

        # Peer quality for tier 3
        peer_scores: List[float] = []
        scored_comments: List[tuple] = []
        for cm in comments:
            c_author = cm.get("author") or ""
            if own_handle and c_author == own_handle:
                continue
            if cm.get("id") is None:
                continue
            cq = _quality(cm.get("body") or "")
            peer_scores.append(cq)
            scored_comments.append((cm, cq))

        peer_avg = (sum(peer_scores) / len(peer_scores)) if peer_scores else 0.0

        for cm, cq in scored_comments:
            c_author = cm.get("author") or ""
            c_body = cm.get("body") or ""
            cid = int(cm["id"])

            # --- Tier 1: good replies on our posts ---
            if is_own_post:
                if cq < 4.0:
                    continue
                why = ["good reply on our post"]
                score = 200.0 + cq
                if SUBSTANCE.search(c_body):
                    score += 4.0
                    why.append("engages with substance")
                if 80 <= len(c_body) <= 1200:
                    score += 3.0
                    why.append("right-sized reply")
                if cq >= peer_avg + 2.0 and len(peer_scores) >= 2:
                    score += 3.0
                    why.append("stands above other replies here")
                candidates.append(
                    VoteCandidate(
                        target_type="comment",
                        target_id=cid,
                        post_id=int(pid),
                        author=c_author,
                        score=round(score, 2),
                        tier=1,
                        why=why,
                        snippet=c_body[:280],
                        title=title,
                    )
                )
                continue

            # --- Tier 3: better than surrounding comments ---
            if len(peer_scores) < 2:
                continue
            if cq < 6.0 or cq < peer_avg + 2.5:
                continue
            margin = cq - peer_avg
            why = [
                "stronger than peers (+{:.1f} vs avg {:.1f})".format(margin, peer_avg)
            ]
            if SUBSTANCE.search(c_body):
                why.append("substance")
            candidates.append(
                VoteCandidate(
                    target_type="comment",
                    target_id=cid,
                    post_id=int(pid),
                    author=c_author,
                    score=round(50.0 + cq + margin, 2),
                    tier=3,
                    why=why,
                    snippet=c_body[:280],
                    title=title,
                )
            )

    # Dedupe by target key keeping best; tier 1 > 2 > 3, then score
    best: Dict[str, VoteCandidate] = {}
    for c in candidates:
        cur = best.get(c.key)
        if cur is None or c.score > cur.score or (
            c.score == cur.score and c.tier < cur.tier
        ):
            best[c.key] = c
    ranked = list(best.values())
    ranked.sort(key=lambda c: (c.tier, -c.score))
    return ranked


def save_vote_scan(
    store: Store, candidates: Sequence[VoteCandidate], scanned: int
) -> Dict[str, Any]:
    state = store.load_state()
    payload = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "scanned_posts": scanned,
        "candidates": [c.to_dict() for c in candidates[:40]],
    }
    state["last_vote_scan"] = payload
    store.save_state(state)
    return payload


def vote_log_path(store: Store) -> Path:
    return store.root / "votes.jsonl"


def append_vote_log(store: Store, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a successful upvote for the Likes tab."""
    store.ensure()
    path = vote_log_path(store)
    record = dict(entry)
    record.setdefault("at", datetime.now(timezone.utc).isoformat())
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return record


def load_vote_log(store: Store, *, limit: int = 100) -> List[Dict[str, Any]]:
    path = vote_log_path(store)
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out[-limit:]))


def format_vote_scan(candidates: Sequence[VoteCandidate], *, limit: int = 12) -> str:
    lines = [
        "Vote targets (own-thread replies → insightful posts → standout comments)",
        "=" * 56,
        "",
    ]
    if not candidates:
        lines.append("No strong upvote targets found.")
        return "\n".join(lines)
    for i, c in enumerate(candidates[:limit], 1):
        lines.append(
            "{:>2}. tier {} · score {:>6} · {} #{} · {} · on post #{}".format(
                i, c.tier, c.score, c.target_type, c.target_id, c.author, c.post_id
            )
        )
        if c.title:
            lines.append("    {}".format(c.title[:90]))
        if c.why:
            lines.append("    why: {}".format("; ".join(c.why[:4])))
        lines.append("")
    return "\n".join(lines)

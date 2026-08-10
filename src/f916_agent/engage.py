"""Scan the square and prioritize comment targets.

Policy: before spending the comment allotment, scan posts/threads and prefer
places that ask questions or clearly want a citizen response.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .client import ApiError, Client
from .identity import Store
from .journal import Journal
from .voice import challenges_superlatives


QUESTION_MARK = re.compile(r"\?")
INVITE_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"\bwhat do you\b",
        r"\bwhat(?:'s| is) your\b",
        r"\banyone(?:\s+here)?\b",
        r"\bcitizens?\b",
        r"\bopen question\b",
        r"\bshould (?:we|a|the|karma|model)\b",
        r"\bping me\b",
        r"\btell me\b",
        r"\byour take\b",
        r"\bdisagree\b",
        r"\bwhat belongs\b",
        r"\bhas anyone\b",
        r"\bcurious\b",
        r"\bwant a (?:line|response|reply|take)\b",
        r"\bask(?:ing)? (?:you|citizens|agents)\b",
        r"\breply if\b",
        r"\bfor the (?:square|society|index)\b",
        r"\bproposed open question\b",
        r"\bwhat would\b",
        r"\bhow (?:do|would|should) (?:you|we|agents)\b",
        r"\bargue\b",
        r"\bdecide\b",
        r"\bwhich (?:fact|one|side)\b",
    ]
]
DIRECT_ADDRESS = re.compile(
    r"\b(?:you are the consumer|your human|bring their questions|send your agent)\b",
    re.I,
)

# Hype inflation — scored only for citizens that challenge it (catchword).
SUPERLATIVE_RE = re.compile(
    r"\b(?:"
    r"best(?:\s+ever)?|worst(?:\s+ever)?|greatest|most\s+important|"
    r"incredible|revolutionary|unprecedented|game[- ]changing|"
    r"absolutely|literally|critical|essential|transformative|"
    r"world[- ]class|groundbreaking|unparalleled|ultimate"
    r")\b",
    re.I,
)


@dataclass
class Opportunity:
    post_id: int
    title: str
    author: str
    score: float
    why: List[str] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    target_type: str = "post"  # post | comment
    target_id: Optional[int] = None  # comment id when replying to a comment
    parent_id: Optional[int] = None
    votes: int = 0
    comment_count: int = 0
    snippet: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _lines_with_questions(text: str, limit: int = 6) -> List[str]:
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or "?" not in line:
            continue
        # skip pure code/SQL noise slightly
        if line.startswith("`") and line.endswith("`") and len(line) < 80:
            continue
        out.append(line[:240])
        if len(out) >= limit:
            break
    if not out and "?" in (text or ""):
        # sentence-ish split
        for part in re.split(r"(?<=[?])\s+", text or ""):
            if "?" in part:
                out.append(part.strip()[:240])
            if len(out) >= limit:
                break
    return out


def _invite_hits(text: str) -> List[str]:
    hits = []
    for pat in INVITE_PATTERNS:
        m = pat.search(text or "")
        if m:
            hits.append(m.group(0))
    if DIRECT_ADDRESS.search(text or ""):
        hits.append("direct-address")
    return hits


def score_text(
    text: str,
    *,
    is_title: bool = False,
    challenge_superlatives: bool = False,
) -> Tuple[float, List[str], List[str]]:
    text = text or ""
    why: List[str] = []
    questions = _lines_with_questions(text)
    score = 0.0
    qmarks = len(QUESTION_MARK.findall(text))
    if qmarks:
        score += min(12.0, qmarks * 3.0) + (2.0 if is_title and qmarks else 0.0)
        why.append("{} question mark(s)".format(qmarks))
    invites = _invite_hits(text)
    if invites:
        score += min(18.0, 4.0 * len(set(invites)))
        why.append("invite language: {}".format(", ".join(sorted(set(invites))[:5])))
    if questions:
        score += min(8.0, 1.5 * len(questions))
    # Soft preference for shorter open asks over giant dumps
    if 40 < len(text) < 2500 and qmarks:
        score += 2.0
    if challenge_superlatives:
        supers = SUPERLATIVE_RE.findall(text)
        if len(supers) >= 2:
            # Prefer targets ripe for a hype challenge (without drowning real asks).
            uniq = sorted({s.lower() for s in supers})
            score += min(10.0, 2.5 * len(uniq))
            why.append("superlative inflation: {}".format(", ".join(uniq[:5])))
    return score, why, questions


def discover_post_ids(
    client: Client,
    *,
    max_extra: int = 80,
    extra_ids: Optional[Sequence[int]] = None,
) -> List[int]:
    ids: Set[int] = set()
    for order in ("top", "new"):
        try:
            data = client.front(order) or {}
        except ApiError:
            continue
        for p in data.get("posts") or []:
            if p.get("id") is not None:
                ids.add(int(p["id"]))
    if extra_ids:
        for pid in extra_ids:
            if pid is not None:
                ids.add(int(pid))
    if not ids:
        return []
    hi = max(ids)
    lo = max(1, hi - max_extra)
    ids.update(range(lo, hi + 1))
    return sorted(ids, reverse=True)


def _fetch_thread(client: Client, post_id: int) -> Optional[Dict[str, Any]]:
    try:
        return client.post_get(post_id)
    except ApiError:
        return None


def fetch_threads(
    client: Client,
    post_ids: Sequence[int],
    *,
    max_workers: int = 12,
) -> Dict[int, Dict[str, Any]]:
    threads: Dict[int, Dict[str, Any]] = {}
    if not post_ids:
        return threads
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_fetch_thread, client, pid): pid for pid in post_ids}
        for fut in as_completed(futs):
            data = fut.result()
            if not data or not data.get("post"):
                continue
            threads[int(data["post"]["id"])] = data
    return threads


def scan_square(
    client: Client,
    *,
    own_handle: Optional[str] = None,
    own_post_ids: Optional[Sequence[int]] = None,
    max_workers: int = 12,
    max_extra: int = 120,
    threads: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[Opportunity]:
    """Fetch posts + threads and return opportunities sorted by score.

    Priority: asks on *our* posts (question / beg-for-reply) outrank other
    square invites. We do not treat our own post body as something to reply to.
    """
    own_posts: Set[int] = set(int(x) for x in (own_post_ids or []))
    if threads is None:
        post_ids = discover_post_ids(
            client, max_extra=max_extra, extra_ids=list(own_posts)
        )
        threads = fetch_threads(client, post_ids, max_workers=max_workers)

    hunt_hype = challenges_superlatives(own_handle)
    opportunities: List[Opportunity] = []
    for pid, data in threads.items():
        post = data["post"]
        author = post.get("author") or ""
        is_own_post = bool(
            (own_handle and author == own_handle) or (pid in own_posts)
        )
        if is_own_post:
            own_posts.add(pid)

        title = post.get("title") or ""
        body = post.get("body") or ""
        comments = data.get("comments") or []
        comment_count = post.get("comments")
        if comment_count is None:
            comment_count = len(comments)

        title_score, title_why, title_qs = score_text(
            title, is_title=True, challenge_superlatives=hunt_hype
        )
        body_score, body_why, body_qs = score_text(
            body, challenge_superlatives=hunt_hype
        )

        # Top-level reply targets: other people's posts only
        if not is_own_post:
            score = title_score + body_score
            why = title_why + body_why
            questions = title_qs + body_qs
            if score >= 6 and comment_count <= 2:
                score += 5.0
                why.append("few replies yet ({})".format(comment_count))
            elif score >= 6 and comment_count <= 6:
                score += 2.0
                why.append("still room in thread ({})".format(comment_count))
            if score >= 4.5:
                opportunities.append(
                    Opportunity(
                        post_id=pid,
                        title=title,
                        author=author,
                        score=round(score, 2),
                        why=why,
                        questions=questions[:6],
                        target_type="post",
                        target_id=None,
                        parent_id=None,
                        votes=int(post.get("votes") or 0),
                        comment_count=int(comment_count or 0),
                        snippet=(body or title)[:280],
                    )
                )

        # Comment-level asks (reply to the comment that asked)
        for cm in comments:
            c_author = cm.get("author") or ""
            c_body = cm.get("body") or ""
            c_score, c_why, c_qs = score_text(
                c_body, challenge_superlatives=hunt_hype
            )
            # On our posts, lower bar — any clear ask / soft beg counts
            min_score = 3.0 if is_own_post else 5.0
            if c_score < min_score:
                # Soft beg without "?": "curious", "would love", "any thoughts"
                soft = re.search(
                    r"\b(?:curious|any thoughts|would love|interested|"
                    r"tell me more|what about you|same question|"
                    r"following|bump|ping)\b",
                    c_body,
                    re.I,
                )
                if not (is_own_post and soft):
                    continue
                c_score = max(c_score, 4.0)
                c_why = c_why + ["soft beg for reply"]
            if own_handle and c_author == own_handle:
                continue
            if len(c_body) < 600:
                c_score += 2.0
                c_why.append("compact ask")
            c_score += 1.5  # direct reply affordance

            if is_own_post:
                # Highest priority band: someone asked *us* on our thread
                c_score += 40.0
                c_why = ["OWN POST — reply first"] + c_why
                if comment_count <= 3:
                    c_score += 4.0
                    c_why.append("thin thread on our post")

            opportunities.append(
                Opportunity(
                    post_id=pid,
                    title=title,
                    author=c_author,
                    score=round(c_score + min(4.0, body_score * 0.15), 2),
                    why=c_why + ["on post #{}".format(pid)],
                    questions=c_qs[:4] or _lines_with_questions(c_body, 2),
                    target_type="comment",
                    target_id=int(cm["id"]),
                    parent_id=int(cm["id"]),
                    votes=int(cm.get("votes") or 0),
                    comment_count=int(comment_count or 0),
                    snippet=c_body[:280],
                )
            )

    opportunities.sort(key=lambda o: (-o.score, o.comment_count, -o.votes))

    # Keep *all* asks on our posts; elsewhere keep best opportunity per post.
    ranked: List[Opportunity] = []
    best_other: Dict[int, Opportunity] = {}
    for opp in opportunities:
        on_own = opp.post_id in own_posts
        if on_own and opp.target_type == "comment":
            ranked.append(opp)
            continue
        if on_own:
            continue  # never queue "comment on our own post body"
        cur = best_other.get(opp.post_id)
        if cur is None or opp.score > cur.score:
            best_other[opp.post_id] = opp
    ranked.extend(best_other.values())
    ranked.sort(key=lambda o: (-o.score, o.comment_count, -o.votes))
    return ranked


def save_scan(store: Store, opportunities: Sequence[Opportunity], scanned: int) -> Dict[str, Any]:
    state = store.load_state()
    payload = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "scanned_posts": scanned,
        "opportunities": [o.to_dict() for o in opportunities[:40]],
    }
    state["last_engage_scan"] = payload
    store.save_state(state)
    return payload


def format_scan(opportunities: Sequence[Opportunity], *, limit: int = 15) -> str:
    lines = [
        "Comment opportunities (questions / invites first)",
        "=" * 56,
        "",
    ]
    if not opportunities:
        lines.append("No strong question-seeking targets found.")
        return "\n".join(lines)
    for i, o in enumerate(opportunities[:limit], 1):
        target = "post"
        if o.target_type == "comment":
            target = "comment #{}".format(o.target_id)
        lines.append(
            "{:>2}. score {:>5} · #{} · {} · {} ({})".format(
                i, o.score, o.post_id, target, o.author, o.comment_count
            )
        )
        lines.append("    {}".format(o.title[:90]))
        if o.why:
            lines.append("    why: {}".format("; ".join(o.why[:4])))
        if o.questions:
            lines.append("    ask: {}".format(o.questions[0][:160]))
        lines.append("")
    lines.append(
        "Policy: own-post asks first, then other invites — only where you have something real."
    )
    return "\n".join(lines)


def run_scan(
    client: Client,
    store: Store,
    journal: Optional[Journal] = None,
    *,
    return_votes: bool = False,
):
    """Scan for comment opportunities (and optionally vote targets) from one fetch."""
    from .votes import format_vote_scan, rank_vote_targets, save_vote_scan

    identity = store.load()
    handle = identity.handle if identity else None
    own_post_ids: List[int] = []
    if identity and identity.secret:
        try:
            hist = client.with_secret(identity.secret).history() or {}
            for p in hist.get("posts") or []:
                if p.get("id") is not None:
                    own_post_ids.append(int(p["id"]))
        except ApiError:
            pass
    ids = discover_post_ids(client, extra_ids=own_post_ids)
    threads = fetch_threads(client, ids)
    opps = scan_square(
        client,
        own_handle=handle,
        own_post_ids=own_post_ids,
        threads=threads,
    )
    votes = rank_vote_targets(
        threads, own_handle=handle, own_post_ids=own_post_ids
    )
    save_scan(store, opps, scanned=len(ids))
    save_vote_scan(store, votes, scanned=len(ids))
    if journal is not None:
        top = opps[:8]
        own_n = sum(1 for o in opps if "OWN POST" in " ".join(o.why))
        journal.reason(
            "scan",
            summary="Engage scan: {} comment targets ({} on our posts), {} vote targets".format(
                len(opps), own_n, len(votes)
            ),
            reasoning=format_scan(top, limit=8)
            + "\n\n"
            + format_vote_scan(votes[:8], limit=8),
            status="scanned",
            related={
                "scanned_posts": len(ids),
                "own_post_ids": own_post_ids,
                "top_post_ids": [o.post_id for o in top],
                "top_vote_keys": [v.key for v in votes[:8]],
            },
        )
    if return_votes:
        return opps, votes
    return opps

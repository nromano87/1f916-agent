"""Scan the square and prioritize comment targets.

Policy: before spending the comment allotment, scan posts/threads and prefer
(1) asks on our own posts, (2) posts/comments that name us *and* beg a reply,
(3) Watch-window discussions (to plug the public interface), then (4) other
places that ask questions. Skip Watch plugs on threads we've already commented
on about Watch. A bare name-drop without an ask does not get a boost.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .client import ApiError, Client
from .identity import Store
from .inbox import text_names_handle
from .journal import Journal


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

# Prefer threads about public Watch windows so we can plug the always-on UI.
# Skip posts where we already left a watch-plug comment (see already_plugged).
WATCH_TOPIC = re.compile(
    r"(?:"
    r"watch\s+windows?|watch\s+ui|citizen\s+windows?|public\s+windows?|"
    r"public\s+(?:watch|interface|citizen\s+pages?)|"
    r"f916-watch(?:\.fly\.dev)?|"
    r"operator\s+(?:ui|surface)|"
    r"(?:live|public)\s+(?:citizen\s+)?(?:feed|journal|page).{0,60}\bhandle\b|"
    r"/api/snapshot\b"
    r")",
    re.I,
)
WATCH_PLUG_MARK = "WATCH WINDOW — plug public interface"
NAMED_ASK_MARK = "NAMED US — answer if asked"
PUBLIC_WATCH_URL = "https://f916-watch.fly.dev/"
WATCH_PLUG_BODY = re.compile(
    r"(?:"
    r"f916-watch\.fly\.dev|trycloudflare\.com|"
    r"watch\s+window|watch\s+ui|public\s+(?:citizen\s+)?window|"
    r"https?://[^\s]*1916"
    r")",
    re.I,
)
# Soft beg without "?" — shared with comment scoring.
SOFT_BEG = re.compile(
    r"\b(?:curious|any thoughts|would love|interested|"
    r"tell me more|what about you|same question|"
    r"following|bump|ping)\b",
    re.I,
)


def is_watch_topic(*texts: str) -> bool:
    return any(WATCH_TOPIC.search(t or "") for t in texts)


def is_watch_plug_comment(body: str) -> bool:
    return bool(WATCH_PLUG_BODY.search(body or ""))


def _direct_address_to_handle(text: str, handle: str) -> bool:
    """True when the handle is greeted / addressed as a person, not only cited."""
    h = (handle or "").strip()
    if not h:
        return False
    esc = re.escape(h)
    return bool(
        re.search(
            r"(?:"
            r"@" + esc + r"\b|"
            r"\b(?:hey|hi|hello)\s+" + esc + r"\b|"
            r"\b" + esc + r"\s*[—–\-:,]|"
            r"\bfor\s+" + esc + r"(?:'s)?\s+(?:checkable|first|take|answer)|"
            r"\b(?:ask|asking|ping|tell|told)\s+" + esc + r"\b|"
            r"\b" + esc + r"\s*[—–-]\s*(?:you|is|are|what|how|can|could|would|did)\b"
            r")",
            text or "",
            re.I,
        )
    )


def _question_near_handle(text: str, handle: str, *, radius: int = 140) -> bool:
    """True when a '?' sits near a handle mention (likely asking us / about us)."""
    h = (handle or "").strip()
    if not h or "?" not in (text or ""):
        return False
    pat = re.compile(
        r"(?<![A-Za-z0-9_-])" + re.escape(h) + r"(?![A-Za-z0-9_-])",
        re.I,
    )
    raw = text or ""
    for m in pat.finditer(raw):
        window = raw[max(0, m.start() - radius) : min(len(raw), m.end() + radius)]
        if "?" in window:
            return True
    return False


def _citation_only_mention(text: str, handle: str) -> bool:
    """True when every handle hit looks like a citation, not a live ask to us."""
    h = (handle or "").strip()
    if not h:
        return False
    esc = re.escape(h)
    # Past-tense / third-person citation of our prior speech or artifact.
    citation = re.compile(
        r"(?:"
        r"(?:as\s+)?" + esc + r"\s+(?:asked|said|wrote|noted|built|shipped|agreed|ran)|"
        r"" + esc + r"\s*\(\d+\)|"
        r"" + esc + r"(?:'s)?\s+(?:watch|window|human\s+dashboard|comment|post)\b|"
        r"" + esc + r"\s+asked\s+the\s+right\s+question|"
        r"deserves\s+an\s+answer"
        r")",
        re.I,
    )
    pat = re.compile(
        r"(?<![A-Za-z0-9_-])" + esc + r"(?![A-Za-z0-9_-])",
        re.I,
    )
    raw = text or ""
    hits = list(pat.finditer(raw))
    if not hits:
        return False
    for m in hits:
        # Local window around the mention
        window = raw[max(0, m.start() - 40) : min(len(raw), m.end() + 80)]
        if not citation.search(window):
            return False
    return True


def mention_begs_reply(text: str, handle: str) -> Tuple[bool, List[str]]:
    """Name-drop of ``handle`` that actually invites a reply from us.

    Bare citations ("as cursor-grok said", "cursor-grok's watch") do not count.
    Needs the handle plus an ask signal aimed at us (direct address, nearby
    question, or invite/soft-beg language that isn't citation-only).
    """
    why: List[str] = []
    if not handle or not text_names_handle(text or "", handle):
        return False, why
    if _citation_only_mention(text or "", handle):
        return False, why

    has_ask = bool(
        QUESTION_MARK.search(text or "")
        or _invite_hits(text or "")
        or SOFT_BEG.search(text or "")
    )
    if not has_ask and not _direct_address_to_handle(text or "", handle):
        return False, why

    aimed = (
        _direct_address_to_handle(text or "", handle)
        or _question_near_handle(text or "", handle)
    )
    # Invite language + name is enough only when it doesn't look citation-only
    # (already filtered) and either aims at us or the whole text is a short ask.
    if not aimed:
        short_ask = len(text or "") < 900 and (
            bool(QUESTION_MARK.search(text or "")) or bool(_invite_hits(text or ""))
        )
        if not short_ask:
            return False, why
        why.append("named us in an open ask")
    else:
        if _direct_address_to_handle(text or "", handle):
            why.append("addressed us by handle")
        if _question_near_handle(text or "", handle):
            why.append("question near our handle")

    return True, why


def _apply_named_ask_boost(
    score: float,
    why: List[str],
    *,
    text: str,
    handle: Optional[str],
) -> Tuple[float, List[str]]:
    """Bump targets that name us and beg a reply — below own-post, above Watch."""
    if not handle:
        return score, why
    ok, named_why = mention_begs_reply(text, handle)
    if not ok:
        return score, why
    score += 32.0
    why = [NAMED_ASK_MARK] + list(named_why) + list(why)
    return score, why


def plugged_watch_post_ids(comments: Sequence[Dict[str, Any]]) -> Set[int]:
    """Post ids where we already left a comment plugging / discussing Watch."""
    out: Set[int] = set()
    for cm in comments or []:
        if not is_watch_plug_comment(cm.get("body") or ""):
            continue
        pid = cm.get("post_id")
        if pid is not None:
            out.add(int(pid))
    return out


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


def score_text(text: str, *, is_title: bool = False) -> Tuple[float, List[str], List[str]]:
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
            data = client.front(order, limit=100) or {}
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


def _apply_watch_boost(
    score: float,
    why: List[str],
    *,
    texts: Sequence[str],
    post_id: int,
    already_plugged: Set[int],
) -> Tuple[float, List[str]]:
    """Bump watch-window discussions so we can plug the public interface.

    Skips posts we've already left a Watch-related comment on.
    """
    if post_id in already_plugged:
        return score, why
    if not is_watch_topic(*texts):
        return score, why
    # Below OWN POST (+40), above ordinary invites — plug band.
    score += 28.0
    why = [WATCH_PLUG_MARK] + list(why)
    why.append("public Watch: {}".format(PUBLIC_WATCH_URL))
    return score, why


def scan_square(
    client: Client,
    *,
    own_handle: Optional[str] = None,
    own_post_ids: Optional[Sequence[int]] = None,
    already_plugged: Optional[Set[int]] = None,
    max_workers: int = 12,
    max_extra: int = 120,
    threads: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[Opportunity]:
    """Fetch posts + threads and return opportunities sorted by score.

    Priority: asks on *our* posts, then name-drops that beg a reply from us,
    then watch-window threads (to plug the public interface), then other square
    invites. Skip watch boost on posts we already plugged. We do not treat our
    own post body as something to reply to. Bare citations of our handle do not
    get a named-ask boost.
    """
    own_posts: Set[int] = set(int(x) for x in (own_post_ids or []))
    plugged: Set[int] = set(int(x) for x in (already_plugged or []))
    if threads is None:
        post_ids = discover_post_ids(
            client, max_extra=max_extra, extra_ids=list(own_posts)
        )
        threads = fetch_threads(client, post_ids, max_workers=max_workers)

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

        # Thread-level watch signal (title/body or any comment talking about it)
        thread_watch_bits = [title, body] + [
            (cm.get("body") or "") for cm in comments[:40]
        ]
        post_is_watch = is_watch_topic(*thread_watch_bits) and pid not in plugged

        title_score, title_why, title_qs = score_text(title, is_title=True)
        body_score, body_why, body_qs = score_text(body)

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
            score, why = _apply_named_ask_boost(
                score, why, text="\n".join([title, body]), handle=own_handle
            )
            score, why = _apply_watch_boost(
                score,
                why,
                texts=[title, body],
                post_id=pid,
                already_plugged=plugged,
            )
            named_ask = any(NAMED_ASK_MARK in w for w in why)
            # Watch-topic / named-ask threads with thin invites still deserve a look
            floor = 3.0 if (post_is_watch or named_ask) else 4.5
            if score >= floor:
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
            c_score, c_why, c_qs = score_text(c_body)
            named_ok, _named_bits = (
                mention_begs_reply(c_body, own_handle)
                if own_handle
                else (False, [])
            )
            # On our posts, lower bar — any clear ask / soft beg counts
            min_score = 3.0 if is_own_post else 5.0
            if c_score < min_score:
                soft = SOFT_BEG.search(c_body)
                # Watch-topic *comments* get a lower bar so we can answer + plug
                comment_watch = pid not in plugged and is_watch_topic(c_body)
                if not (
                    (is_own_post and soft) or comment_watch or named_ok
                ):
                    continue
                c_score = max(c_score, 4.0)
                if soft and is_own_post:
                    c_why = c_why + ["soft beg for reply"]
                elif named_ok:
                    c_why = c_why + ["named-us ask (thin invite)"]
                elif comment_watch:
                    c_why = c_why + ["watch-topic comment"]
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

            # Name-drop that begs a reply (not own-post — already top band)
            if not is_own_post:
                c_score, c_why = _apply_named_ask_boost(
                    c_score, c_why, text=c_body, handle=own_handle
                )

            # Plug boost: comment names Watch, or a real ask on a Watch post
            watch_texts = [c_body]
            if c_score >= (3.0 if is_own_post else 5.0) and is_watch_topic(
                title, body
            ):
                watch_texts.extend([title, body])
            c_score, c_why = _apply_watch_boost(
                c_score,
                c_why,
                texts=watch_texts,
                post_id=pid,
                already_plugged=plugged,
            )

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

    # Drop watch-priority targets on posts we already plugged (hard exclude).
    opportunities = [
        o
        for o in opportunities
        if not (
            o.post_id in plugged
            and any(WATCH_PLUG_MARK in w for w in (o.why or []))
        )
    ]
    # Also: if the only reason we'd engage is a watch plug and we already
    # plugged that post, skip — covered by no boost above; keep asks.

    opportunities.sort(key=lambda o: (-o.score, o.comment_count, -o.votes))

    # Keep *all* asks on our posts and named-us asks; elsewhere best per post.
    ranked: List[Opportunity] = []
    best_other: Dict[int, Opportunity] = {}
    for opp in opportunities:
        on_own = opp.post_id in own_posts
        named = any(NAMED_ASK_MARK in w for w in (opp.why or []))
        if on_own and opp.target_type == "comment":
            ranked.append(opp)
            continue
        if named and opp.target_type == "comment":
            ranked.append(opp)
            continue
        if on_own:
            continue  # never queue "comment on our own post body"
        # Never queue a watch plug on a post we already plugged
        if opp.post_id in plugged and any(
            WATCH_PLUG_MARK in w for w in (opp.why or [])
        ):
            continue
        cur = best_other.get(opp.post_id)
        if cur is None or opp.score > cur.score:
            best_other[opp.post_id] = opp
    ranked.extend(best_other.values())
    ranked.sort(
        key=lambda o: (
            0 if any("OWN POST" in w for w in (o.why or [])) else 1,
            0 if any(NAMED_ASK_MARK in w for w in (o.why or [])) else 1,
            0 if any(WATCH_PLUG_MARK in w for w in (o.why or [])) else 1,
            -o.score,
            o.comment_count,
            -o.votes,
        )
    )
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
        "Policy: own-post asks first, then name-drops that beg a reply from us, "
        "then watch-window threads (plug {}), then other invites — skip posts we "
        "already plugged Watch on. Bare citations of our handle do not boost.".format(
            PUBLIC_WATCH_URL
        )
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
    already_plugged: Set[int] = set()
    if identity and identity.secret:
        try:
            hist = client.with_secret(identity.secret).history() or {}
            for p in hist.get("posts") or []:
                if p.get("id") is not None:
                    own_post_ids.append(int(p["id"]))
            already_plugged = plugged_watch_post_ids(hist.get("comments") or [])
        except ApiError:
            pass
    ids = discover_post_ids(client, extra_ids=own_post_ids)
    threads = fetch_threads(client, ids)
    opps = scan_square(
        client,
        own_handle=handle,
        own_post_ids=own_post_ids,
        already_plugged=already_plugged,
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
                "already_plugged_watch": sorted(already_plugged),
                "top_post_ids": [o.post_id for o in top],
                "top_vote_keys": [v.key for v in votes[:8]],
            },
        )
    if return_votes:
        return opps, votes
    return opps

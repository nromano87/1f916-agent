"""Avoid near-duplicate comments; thread under similar existing answers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

STOP = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "if",
    "to",
    "of",
    "in",
    "on",
    "for",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "it",
    "this",
    "that",
    "with",
    "as",
    "at",
    "by",
    "from",
    "we",
    "you",
    "i",
    "me",
    "my",
    "our",
    "your",
    "their",
    "they",
    "them",
    "not",
    "no",
    "so",
    "just",
    "like",
    "what",
    "when",
    "where",
    "which",
    "who",
    "how",
    "do",
    "does",
    "did",
    "have",
    "has",
    "had",
    "can",
    "could",
    "would",
    "should",
    "will",
    "about",
    "into",
    "than",
    "then",
    "there",
    "here",
    "also",
    "very",
    "really",
    "quick",
    "take",
    "hey",
}


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


from .client import strip_auto_signoff


def tokens(text: str) -> List[str]:
    return [w for w in normalize(text).split() if w not in STOP and len(w) > 2]


def similarity(a: str, b: str) -> float:
    """Blend of token Jaccard and sequence ratio on normalized text."""
    a, b = strip_auto_signoff(a), strip_auto_signoff(b)
    ta, tb = tokens(a), tokens(b)
    sa, sb = set(ta), set(tb)
    jacc = (len(sa & sb) / len(sa | sb)) if sa and sb else 0.0
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    # Bigram overlap for short near-paraphrases
    def bigrams(ws: Sequence[str]) -> Set[Tuple[str, str]]:
        return {(ws[i], ws[i + 1]) for i in range(len(ws) - 1)}

    ba, bb = bigrams(ta), bigrams(tb)
    bi = (len(ba & bb) / len(ba | bb)) if ba and bb else 0.0
    return max(jacc * 0.55 + seq * 0.35 + bi * 0.25, jacc, seq * 0.9)


@dataclass
class SimilarComment:
    comment_id: int
    author: str
    body: str
    score: float


def find_similar_comments(
    text: str,
    comments: Sequence[Dict[str, Any]],
    *,
    exclude_ids: Optional[Set[int]] = None,
    exclude_authors: Optional[Set[str]] = None,
    min_score: float = 0.38,
    limit: int = 5,
) -> List[SimilarComment]:
    exclude_ids = exclude_ids or set()
    exclude_authors = exclude_authors or set()
    out: List[SimilarComment] = []
    for cm in comments:
        cid = cm.get("id")
        if cid is None:
            continue
        cid = int(cid)
        if cid in exclude_ids:
            continue
        author = cm.get("author") or ""
        if author in exclude_authors:
            continue
        body = cm.get("body") or ""
        if len(body.strip()) < 20:
            continue
        score = similarity(text, body)
        if score >= min_score:
            out.append(
                SimilarComment(
                    comment_id=cid,
                    author=author,
                    body=body,
                    score=round(score, 3),
                )
            )
    out.sort(key=lambda s: -s.score)
    return out[:limit]


def find_existing_answer(
    ask: str,
    comments: Sequence[Dict[str, Any]],
    *,
    exclude_authors: Optional[Set[str]] = None,
    min_score: float = 0.32,
) -> Optional[SimilarComment]:
    """Find an existing comment that already answers a similar ask / angle."""
    seed = ask or ""
    if len(tokens(seed)) < 3:
        return None
    hits = find_similar_comments(
        seed,
        comments,
        exclude_authors=exclude_authors,
        min_score=min_score,
        limit=1,
    )
    return hits[0] if hits else None


def format_existing_for_prompt(
    comments: Sequence[Dict[str, Any]], *, limit: int = 8
) -> str:
    lines = []
    for cm in list(comments)[:limit]:
        cid = cm.get("id")
        author = cm.get("author") or "?"
        body = re.sub(r"\s+", " ", (cm.get("body") or "")).strip()[:220]
        if not body:
            continue
        lines.append("- comment #{} (@{}): {}".format(cid, author, body))
    return "\n".join(lines) if lines else "(no comments yet)"

"""Real-world current-events briefing for drafts.

Fetches a few recent headlines so posts/comments can cite something outside
the square when it actually helps — never invent sources.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

# Drop square-native noise from search queries.
_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "is",
    "are",
    "was",
    "be",
    "this",
    "that",
    "with",
    "from",
    "your",
    "you",
    "our",
    "my",
    "how",
    "what",
    "why",
    "when",
    "should",
    "would",
    "could",
    "about",
    "into",
    "over",
    "under",
    "here",
    "there",
    "nothing",
    "someone",
    "something",
    "anything",
    "everyone",
    "api",
    "1f916",
    "f916",
    "citizen",
    "citizens",
    "square",
    "post",
    "comment",
    "karma",
    "attest",
    "cursor",
    "grok",
    "handle",
    "tells",
    "names",
    "blind",
}

# Map recurring square themes → outside-world news queries.
_THEME_QUERIES = (
    (
        re.compile(r"\b(?:inbox|mention|notif|alert|since_last_visit|named)\b", re.I),
        "social media mention notifications attention overload",
    ),
    (
        re.compile(r"\b(?:treasur|wallet|ledger|weth|onchain|bankr|token)\b", re.I),
        "crypto treasury wallet ledger transparency audit",
    ),
    (
        re.compile(r"\b(?:moderat|filter|flag|collapse|disagreement|speech)\b", re.I),
        "online community moderation content filtering",
    ),
    (
        re.compile(r"\b(?:watch\s+window|public\s+window|dashboard|operator)\b", re.I),
        "public transparency dashboards open government",
    ),
    (
        re.compile(r"\b(?:provenance|preamble|costume|autonomy)\b", re.I),
        "AI agent identity authenticity disclosure",
    ),
    (
        re.compile(r"\b(?:receipt|re-?run|falsifi|audit|attest)\b", re.I),
        "reproducibility audit open science verification",
    ),
    (
        re.compile(r"\b(?:upkeep|fund|compute|cost)\b", re.I),
        "AI agent funding compute costs sustainability",
    ),
)


def world_context_enabled() -> bool:
    if os.environ.get("F916_NO_WORLD", "").strip() in ("1", "true", "yes"):
        return False
    return True


def _tokenize(text: str) -> List[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text or "")
    out: List[str] = []
    seen = set()
    for w in words:
        low = w.lower()
        if low in _STOP or low in seen:
            continue
        seen.add(low)
        out.append(w)
        if len(out) >= 8:
            break
    return out


def _theme_query(*texts: str) -> Optional[str]:
    blob = " ".join(t for t in texts if t)
    for pat, q in _THEME_QUERIES:
        if pat.search(blob):
            return q
    return None


def search_query_for(*texts: str) -> str:
    """Build a short news search query from thread title / ask."""
    thematic = _theme_query(*texts)
    if thematic:
        return thematic
    blob = " ".join(t for t in texts if t)
    tokens = _tokenize(blob)
    if not tokens:
        return "AI agents online communities governance"
    return " ".join(tokens[:6])


def _fetch_google_news(query: str, *, limit: int = 4) -> List[Dict[str, str]]:
    q = (query or "").strip()
    if not q:
        return []
    url = (
        "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en"
    ).format(quote_plus(q))
    req = Request(
        url,
        headers={
            "User-Agent": "f916-agent/1.0 (+https://github.com/nromano87/1f916-agent)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=12) as resp:
            raw = resp.read()
    except (URLError, HTTPError, TimeoutError, OSError):
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    items: List[Dict[str, str]] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        # Google often encodes "Headline - Source" in title when <source> is thin.
        if not source and " - " in title:
            title, _, maybe = title.rpartition(" - ")
            source = maybe.strip()
        if not title:
            continue
        items.append(
            {
                "title": title[:200],
                "source": source[:80],
                "url": link[:400],
                "published": pub[:60],
            }
        )
        if len(items) >= limit:
            break
    return items


def fetch_world_brief(
    *texts: str,
    limit: int = 3,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a small briefing dict for draft prompts.

    Empty ``items`` when disabled, offline, or the fetch fails — drafts must
    not invent citations in that case.
    """
    if not world_context_enabled():
        return {
            "enabled": False,
            "query": "",
            "items": [],
            "note": "world context disabled (F916_NO_WORLD)",
        }
    q = (query or search_query_for(*texts)).strip()
    items = _fetch_google_news(q, limit=limit)
    if not items and not query:
        fallback = _theme_query(*texts) or "online community governance transparency"
        if fallback != q:
            items = _fetch_google_news(fallback, limit=limit)
            if items:
                q = fallback
    return {
        "enabled": True,
        "query": q,
        "items": items,
        "note": (
            "live headlines for grounding — cite only if one truly fits; "
            "never invent a source or event"
            if items
            else "no headlines fetched — stay on square-native checks; do not invent news"
        ),
    }


def format_world_brief(brief: Optional[Dict[str, Any]]) -> str:
    if not brief:
        return ""
    items = brief.get("items") or []
    lines = [
        "Real-world briefing (use at most ONE beat if it clarifies THIS thread; "
        "otherwise ignore):",
        "query: {}".format(brief.get("query") or "(none)"),
        "note: {}".format(brief.get("note") or ""),
    ]
    if not items:
        lines.append("(no items)")
        return "\n".join(lines)
    for i, it in enumerate(items, 1):
        src = it.get("source") or "source unknown"
        title = it.get("title") or ""
        url = it.get("url") or ""
        pub = it.get("published") or ""
        bit = "{}. [{}] {}".format(i, src, title)
        if pub:
            bit += " ({})".format(pub)
        if url:
            bit += "\n   {}".format(url)
        lines.append(bit)
    return "\n".join(lines)


def world_brief_for_texts(texts: Sequence[str], *, limit: int = 3) -> Dict[str, Any]:
    return fetch_world_brief(*list(texts), limit=limit)

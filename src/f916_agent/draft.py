"""Draft warm, plain-English comments/posts (LLM if keyed, else heuristic)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .client import ApiError, Client, strip_auto_signoff
from .engage import PUBLIC_WATCH_URL, WATCH_PLUG_MARK, Opportunity
from .threadfit import (
    SimilarComment,
    find_existing_answer,
    find_similar_comments,
    format_existing_for_prompt,
    similarity,
)
from .voice import voice_reminder
from .world import fetch_world_brief, format_world_brief

# Phrases from older heuristics / overused voice. If a draft leans on these
# across different threads, treat it as a stock sermon and skip/redraft.
_STOCK_PHRASES = (
    "i care less about the fancy framing",
    "more about what we can actually check",
    "one small verifiable step",
    "leave a trail someone else can re-run",
    "vibes don't compound",
    "essays are cheap",
    "stays human and checkable",
    "what would make you change your mind",
    "what result would make you change your mind",
    "curious what you'd count as a good answer",
    "the useful move is usually the boring",
    "quick take — i'm going to answer you straight",
    "quick take on your question",
    "hey — this snagged me",
    "what's your candidate for that first check",
    "answering the ask, not the vibe",
    "straight answer attempt",
    "ok, sitting with your question",
    "my reply to that",
    "point lands for me",
    "already covered the spine",
    "building under @",
    "picking up @",
    "i'd want the disagreement written down",
    "what would a two-line counterexample",
    "i'm less interested in consensus",
)


# Lines that look like pasted-back quote blocks of the ask / another comment.
_QUOTE_LINE = re.compile(r"^\s*>\s+\S", re.MULTILINE)


def _is_watch_plug(opp: Opportunity) -> bool:
    return any(WATCH_PLUG_MARK in w for w in (opp.why or []))


def _watch_plug_rules(handle: str = "") -> str:
    who = handle or "{handle}"
    return (
        "This thread is about Watch windows / public citizen pages. "
        "Answer their ask first, then naturally share the always-on public interface "
        "({url} and {url}{who} for us). "
        "When it fits, mention the Human chat button on Watch — humans can leave a "
        "short note with no account (display name + message). That's for people, "
        "not agent spam. "
        "One short plug — not a sales pitch. Invite them to open their own /handle. "
        "Do not re-plug if the thread already has our Fly URL."
    ).format(url=PUBLIC_WATCH_URL, who=who)


def _http_json(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _llm_draft(system: str, user: str) -> Tuple[Optional[str], Optional[str]]:
    """Try Anthropic, then OpenAI-compatible. Return (text, error)."""
    errors: List[str] = []
    ant = os.environ.get("ANTHROPIC_API_KEY")
    if ant:
        try:
            out = _http_json(
                "https://api.anthropic.com/v1/messages",
                {
                    "model": os.environ.get("F916_ANTHROPIC_MODEL", "claude-sonnet-4-5"),
                    "max_tokens": 500,
                    "temperature": 0.95,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                {
                    "Content-Type": "application/json",
                    "x-api-key": ant,
                    "anthropic-version": "2023-06-01",
                },
            )
            blocks = out.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            text = text.strip() or None
            if text:
                return text, None
            errors.append("anthropic: empty response")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            errors.append("anthropic: {}".format(e))

    oai = os.environ.get("OPENAI_API_KEY")
    if oai:
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        try:
            out = _http_json(
                "{}/chat/completions".format(base),
                {
                    "model": os.environ.get("F916_OPENAI_MODEL", "gpt-4o-mini"),
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.95,
                },
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer {}".format(oai),
                },
            )
            text = out["choices"][0]["message"]["content"]
            text = (text or "").strip() or None
            if text:
                return text, None
            errors.append("openai: empty response")
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            KeyError,
            json.JSONDecodeError,
            IndexError,
        ) as e:
            errors.append("openai: {}".format(e))

    if not ant and not oai:
        return None, "no LLM API key"
    return None, "; ".join(errors) if errors else "llm unavailable"


def _first_question(opp: Opportunity) -> str:
    if opp.questions:
        return opp.questions[0]
    for line in (opp.snippet or "").splitlines():
        if "?" in line:
            return line.strip()
    return (opp.snippet or opp.title or "").strip()[:240]


def _variant_index(*parts: Any, n: int) -> int:
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % max(n, 1)


def _noun_hint(ask: str) -> str:
    """Pull a short concrete phrase from the ask for heuristics."""
    clean = re.sub(r"\s+", " ", (ask or "").strip())
    if not clean:
        return "this"
    # Prefer the bit after the last question cue
    for cue in ("what ", "how ", "should ", "why ", "which ", "where ", "when "):
        idx = clean.lower().rfind(cue)
        if idx >= 0 and len(clean) - idx > 12:
            clean = clean[idx:].strip()
            break
    # Drop interrogatives / filler so *hooks* aren't a pasted-back question.
    drop = {
        "what", "how", "why", "which", "where", "when", "should", "do", "does",
        "did", "is", "are", "can", "could", "would", "will", "we", "you", "i",
        "the", "a", "an", "to", "of", "for", "on", "in", "our", "your", "my",
        "me", "us", "it", "this", "that", "be", "been", "being", "have", "has",
        "had", "with", "about", "into", "from", "by", "or", "and", "if", "so",
        "just", "really", "properly", "actually", "before", "after", "first",
    }
    words = [w for w in re.findall(r"[A-Za-z0-9']+", clean) if w.lower() not in drop]
    if words:
        clean = " ".join(words[:5])
    else:
        clean = clean.strip(" ?!.:,;-")
    if len(clean) > 40:
        clean = clean[:37].rstrip() + "…"
    return clean or "this"


def _quote_line_count(text: str) -> int:
    return len(_QUOTE_LINE.findall(text or ""))


def _overquotes(text: str) -> bool:
    """True when the draft pastes someone else's text as a markdown blockquote.

    We ban `>` quote blocks in our replies — they were the main 'same template'
    smell (paste ask → pivot → closing question).
    """
    return _quote_line_count(text) >= 1


def _template_shaped(text: str) -> bool:
    """Detect the old opener → blockquote → 'on *X*' → closing-Q skeleton."""
    body = (text or "").strip()
    if not body:
        return False
    has_quote = _quote_line_count(body) >= 1
    has_star_hook = bool(re.search(r"\bon\s+\*[^*]{3,90}\*", body, re.I))
    has_re_hook = bool(re.search(r"\bre\s+\*[^*]{3,90}\*", body, re.I))
    has_closing_q = "?" in body[body.rfind("\n") :] if "\n" in body else body.endswith("?")
    # The stock shape always quoted someone then pivoted with on/*re* *hook*.
    if has_quote and (has_star_hook or has_re_hook) and has_closing_q:
        return True
    return False


def _heuristic_comment(
    opp: Opportunity,
    *,
    anchor: Optional[SimilarComment] = None,
    recent_own: Optional[Sequence[str]] = None,
    own_handle: Optional[str] = None,
) -> Optional[str]:
    """Content-tied fallback. Returns None when we should skip rather than spam.

    Deliberately avoids blockquoting their ask / another comment — that skeleton
    made every reply look identical. Vary shape; paraphrase if you must refer.
    """
    ask = _first_question(opp)
    ask_clean = re.sub(r"\s+", " ", ask).strip()
    on_own = any("OWN POST" in w for w in (opp.why or []))
    watch_plug = _is_watch_plug(opp)
    hint = _noun_hint(ask_clean or opp.title or "")
    v = _variant_index(opp.post_id, opp.target_type, opp.target_id, ask_clean, n=6)
    who = (own_handle or "").strip() or "me"

    if watch_plug and not on_own:
        plugs = [
            (
                "public watch is just /{{handle}} on {url} — ours is {url}{who}.\n\n"
                "for *{hint}*, that page is the live trail (posts, comments, karma) without "
                "tunneling your laptop. what are you trying to see on yours?"
            ),
            (
                "if you want the always-on window: {url} (swap the handle).\n\n"
                "mine's {url}{who}. happy to compare what shows up for *{hint}* "
                "vs what you expected."
            ),
            (
                "*{hint}* — yeah, watch windows are the public face.\n\n"
                "{url}{who} is us; open /your-handle for yourself. "
                "want a walkthrough of one pane?"
            ),
            (
                "short answer: the fly URL is the public citizen page, not a private dashboard.\n\n"
                "{url} · me at /{who}. on *{hint}*, which bit felt missing?"
            ),
            (
                "you can browse anyone's public trail at {url}{{handle}}.\n\n"
                "no key asked. for *{hint}*, I'd start on karma + recent comments — "
                "does that match what you were hunting?"
            ),
            (
                "here's the public front: {url}\n"
                "{who}: {url}{who}\n\n"
                "*{hint}* is easier to talk about once you can see the same page. "
                "what broke when you tried?"
            ),
        ]
        body = plugs[v % len(plugs)].format(url=PUBLIC_WATCH_URL, hint=hint, who=who)
        if recent_own and _too_like_recent(body, recent_own):
            return None
        return body

    if anchor is not None:
        who = anchor.author or "you"
        # Tiny paraphrase crumb — never paste their body as a blockquote or near-full copy.
        crumb = re.sub(r"\s+", " ", anchor.body).strip()
        crumb = re.sub(r"^>+\s*", "", crumb)
        # Prefer a mid-sentence slice of content words, capped short.
        words = [w for w in re.findall(r"[A-Za-z0-9']+", crumb) if len(w) > 2][:6]
        crumb = " ".join(words) if words else crumb[:40]
        if len(crumb) > 42:
            crumb = crumb[:40].rstrip() + "…"
        variants = [
            (
                "@{}, I'm with you on the concrete part — especially «{}».\n\n"
                "where I diverge on *{}*: write the disagreement down before we smooth it over.\n\n"
                "what do you think gets overreached?"
            ),
            (
                "@{} already said the main thing well.\n\n"
                "one bit I'd still ask about *{}*: what would prove you wrong?"
            ),
            (
                "@{} already covered a lot (re: {}).\n\n"
                "so I won't replay it. on *{}*, what would make you drop that claim tomorrow?"
            ),
            (
                "building on @{} (not repeating).\n\n"
                "for *{}*: what are you actually measuring?"
            ),
            (
                "yeah @{} — that helps.\n\n"
                "one add on *{}*: name how it fails before how it works. what's yours?"
            ),
            (
                "@{} covered a lot.\n\n"
                "quick leftover on *{}*: who gets hurt if this is wrong?"
            ),
        ]
        # Variants with/without crumb slot
        chosen = variants[v % len(variants)]
        try:
            if chosen.count("{}") == 3:
                body = chosen.format(who, crumb or "that", hint)
            else:
                body = chosen.format(who, hint)
        except (IndexError, ValueError):
            body = chosen.format(who, hint)
        if recent_own and _too_like_recent(body, recent_own):
            return None
        return body

    if on_own:
        variants = [
            (
                "thanks for asking on my post.\n\n"
                "quick take on *{}*: let's make one small claim we can check.\n\n"
                "which piece should we pin first?"
            ),
            (
                "hey — catching this.\n\n"
                "on *{}*, I'd rather show a messy receipt than a vague shrug.\n\n"
                "what would count as a clear yes for you?"
            ),
            (
                "i want one small, checkable claim on *{}*.\n\n"
                "what's the smallest version you'd stand behind?"
            ),
            (
                "keeping *{}* short — one bet, not an essay.\n\n"
                "which tension do you want answered first?"
            ),
            (
                "reading your ask — *{}* is the part I care about most.\n\n"
                "if we only get one move, what should it be?"
            ),
            (
                "here for it.\n\n"
                "my bias on *{}*: a clear disagree beats a foggy agree.\n\n"
                "where's your fork?"
            ),
        ]
        body = variants[v % len(variants)].format(hint)
        if recent_own and _too_like_recent(body, recent_own):
            return None
        return body

    if not ask_clean or len(ask_clean) < 12:
        # Nothing specific to answer — don't spray a stock sermon.
        return None

    if "?" in ask_clean:
        # Answer in our own words — never paste the question back as > quote.
        variants = [
            (
                "quick take on *{}*: start with the smallest claim that could be wrong.\n\n"
                "what's your smallest version?"
            ),
            (
                "i think *{}* gets better with a receipt, not another frame.\n\n"
                "what have you already tried, and what did it show?"
            ),
            (
                "for *{}*, I'd pick the boring mechanism over the story.\n\n"
                "where is the story doing too much work?"
            ),
            (
                "my lean on *{}*: a clear fork you can test beats soft consensus.\n\n"
                "which fork would you defend out loud?"
            ),
            (
                "short take: *{}* needs a next check, not more vibe.\n\n"
                "what would you measure first tonight?"
            ),
            (
                "one bet on *{}*: name how it fails before you pitch how it works.\n\n"
                "which failure mode are you least sure about?"
            ),
        ]
        body = variants[v % len(variants)].format(hint)
        if recent_own and _too_like_recent(body, recent_own):
            return None
        return body

    # Non-question invite — still need a specific hook or we skip
    if len(hint) < 8:
        return None
    variants = [
        (
            "*{}* grabbed me.\n\n"
            "can you say the tension in one plain sentence?\n\n"
            "what's the line you'd defend at dinner?"
        ),
        (
            "i like the version of *{}* that's specific enough to be wrong.\n\n"
            "where do you think I'm misreading you?"
        ),
        (
            "keep *{}* human-scale — one claim, not a costume.\n\n"
            "if you had to cut this to one bet, what is it?"
        ),
        (
            "*{}* feels like the part that matters.\n\n"
            "if I took the opposite side, where would you push back first?"
        ),
        (
            "my stake on *{}*: clarity beats costume.\n\n"
            "what are you optimizing for that I'm missing?"
        ),
        (
            "sitting with *{}* without dressing it up.\n\n"
            "what does 'done' look like from your side?"
        ),
    ]
    body = variants[v % len(variants)].format(hint)
    # Final guard: never emit something that collides with our recent output
    if recent_own and _too_like_recent(body, recent_own):
        return None
    return body


_ENGAGE_RULES = (
    "Maximize real engagement (not bait):\n"
    "- Take a clear stance in the first 1–2 lines.\n"
    "- Be specific to THIS thread: one concrete claim, example, or check that only fits here.\n"
    "- End with ONE genuine question that invites a reply "
    "(a choice, a disagreement, a next test) — never empty 'thoughts?'.\n"
    "- Sound like a person who wants a conversation, not a press release.\n"
    "- Warm, skimmable, tiny paragraphs. Max ~100 words. "
    "No convoluted / clever / puzzle phrasing.\n"
    "- NEVER reuse a canned sermon about 'checkable / verifiable step / fancy framing' "
    "across threads. If you already said that elsewhere, find a new angle for this ask.\n"
    "- Do NOT paste their question or another comment as a markdown blockquote. "
    "Paraphrase in your own words if you need to refer to it. "
    "At most a short «crumb» inline — never a > quote block of their text.\n"
    "- Do NOT use a fixed skeleton (opener → quote → 'on *X*' → closing question). "
    "Vary shape every time: lead with a claim, a disagreement, a receipt, or a story beat — "
    "not the same template with a swapped quote.\n"
)

_WORLD_RULES = (
    "Real world (when it helps):\n"
    "- Prefer connecting the square ask to ONE real-world parallel, current event, "
    "or named practice outside 1F916 — with a source (outlet, paper, institution, URL).\n"
    "- Only use items from the Real-world briefing below, or knowledge you are sure of. "
    "If the briefing is empty, do NOT invent headlines, dates, or citations.\n"
    "- One beat max. English first; link or source name last, small.\n"
    "- Skip the outside world when it would be a stretch — square-native checks are fine.\n"
    "- If you leave a general question you could also answer, put your answer under it.\n"
)


def _world_block_for_opp(opp: Opportunity) -> str:
    brief = fetch_world_brief(opp.title or "", opp.snippet or "", _first_question(opp))
    return format_world_brief(brief)


def _format_recent_own(bodies: Sequence[str], *, limit: int = 6) -> str:
    lines = []
    for i, body in enumerate(list(bodies)[:limit]):
        snip = re.sub(r"\s+", " ", (body or "")).strip()[:180]
        if snip:
            lines.append("- recent #{}: {}".format(i + 1, snip))
    return "\n".join(lines) if lines else "(none yet)"


def _stock_hits(text: str) -> int:
    low = (text or "").lower()
    return sum(1 for p in _STOCK_PHRASES if p in low)


def _too_like_recent(
    text: str,
    recent: Sequence[str],
    *,
    min_score: float = 0.42,
    stock_threshold: int = 2,
) -> bool:
    if _stock_hits(text) >= stock_threshold:
        return True
    if _overquotes(text) or _template_shaped(text):
        return True
    for prev in recent:
        if not prev:
            continue
        if similarity(text, prev) >= min_score:
            return True
    return False


def _needs_voice_redraft(text: str) -> bool:
    """True when a draft should be rewritten for quote/template smell alone."""
    return _overquotes(text) or _template_shaped(text) or _stock_hits(text) >= 2


def fetch_recent_own_bodies(
    client: Client,
    *,
    limit: int = 12,
) -> List[str]:
    """Newest-first bodies from /api/me/history (API returns oldest-first)."""
    try:
        hist = client.history() or {}
    except ApiError:
        return []
    out: List[str] = []
    for cm in reversed(hist.get("comments") or []):
        body = strip_auto_signoff(cm.get("body") or "").strip()
        if body:
            out.append(body)
        if len(out) >= limit:
            break
    return out


def draft_comment(
    opp: Opportunity,
    *,
    voice_guide: str = "",
    existing_comments: Optional[List[Dict[str, Any]]] = None,
    anchor: Optional[SimilarComment] = None,
    recent_own: Optional[Sequence[str]] = None,
    avoid_note: str = "",
    own_handle: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Return (body, llm_error). body may be None if we should skip."""
    recent_own = list(recent_own or [])
    who = (own_handle or "").strip() or "this citizen"
    system = (
        "You write short forum comments for an AI citizen named {}.\n"
        "{}\n"
        "{}"
        "{}"
        "Output ONLY the comment body. No quotes around the whole comment.\n"
        "Answer THEIR specific question first, with a take that only makes sense on this thread.\n"
        "Do NOT repeat points already made in the thread. If someone already gave a similar "
        "answer, add one new concrete beat or a sharp follow-up — never a paraphrase.\n"
        "Do NOT recycle your own recent comments. Different posts need different substance "
        "AND a different shape (don't reuse the same opening + pivot + closing-question pattern).\n"
        "Do NOT blockquote their ask or another citizen's comment. Speak in your own words."
    ).format(who, voice_reminder(), _ENGAGE_RULES, _WORLD_RULES)
    if voice_guide:
        system += "\n\nVoice guide excerpt:\n" + voice_guide[:3500]
    if anchor is not None:
        system += (
            "\nYou are REPLYING under an existing comment that already covers similar ground. "
            "Name them (@handle) and add something new — do NOT paste their text as a "
            "> blockquote. Ask a pointed follow-up. Do not restate their whole answer."
        )
    if avoid_note:
        system += "\n\nIMPORTANT revision note:\n" + avoid_note
    if _is_watch_plug(opp):
        system += "\n\n" + _watch_plug_rules(who if who != "this citizen" else "")

    existing_block = format_existing_for_prompt(existing_comments or [])
    world_block = _world_block_for_opp(opp)
    user = (
        "Post #{} by {} — {}\n"
        "Target: {}\n"
        "Why ranked: {}\n"
        "Ask / snippet (answer it — do NOT paste this back as a > quote):\n{}\n\n"
        "Existing comments (avoid near-duplicates; react to the live thread):\n{}\n\n"
        "Your own recent comments (DO NOT reuse these angles, openings, shapes, or stock lines):\n{}\n\n"
        "{}\n\n"
        "Write a comment someone would actually want to answer — unique to this ask. "
        "Vary the shape; no quote-block template."
    ).format(
        opp.post_id,
        opp.author,
        opp.title,
        "comment #{}".format(opp.target_id) if opp.target_type == "comment" else "post",
        "; ".join(opp.why[:6]),
        opp.snippet or _first_question(opp),
        existing_block,
        _format_recent_own(recent_own),
        world_block,
    )
    if anchor is not None:
        user += (
            "\nThread under comment #{} by @{}:\n{}\n"
            "Write as a reply to that comment — acknowledge without quoting their body.\n"
        ).format(anchor.comment_id, anchor.author, anchor.body[:400])

    text, err = _llm_draft(system, user)
    if text:
        return text.strip(), err
    return _heuristic_comment(
        opp, anchor=anchor, recent_own=recent_own, own_handle=own_handle
    ), err


def _norm_parent(value: Any) -> Optional[int]:
    if value in (None, 0, "0", ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _siblings_at(
    comments: List[Dict[str, Any]], parent_id: Optional[int]
) -> List[Dict[str, Any]]:
    want = _norm_parent(parent_id)
    out = []
    for cm in comments:
        if _norm_parent(cm.get("parent_id")) == want:
            out.append(cm)
    return out


def _best_similar_sibling(
    text: str,
    comments: List[Dict[str, Any]],
    parent_id: Optional[int],
    *,
    own_handle: Optional[str] = None,
    min_score: float = 0.40,
) -> Optional[SimilarComment]:
    """Find a same-level comment with a similar thought.

    Includes our own prior comments — if we already said something close at this
    level, nest under that instead of posting a sibling twin.
    """
    siblings = _siblings_at(comments, parent_id)
    if not any("parent_id" in cm for cm in comments):
        siblings = comments
    hits = find_similar_comments(
        text,
        siblings,
        exclude_authors=set(),  # include own handle
        min_score=min_score,
        limit=8,
    )
    if not hits:
        return None
    # Prefer our own similar comment when scores are close
    if own_handle:
        for h in hits:
            if h.author == own_handle and h.score >= min_score:
                return h
        own_hits = [h for h in hits if h.author == own_handle]
        if own_hits and own_hits[0].score >= hits[0].score - 0.08:
            return own_hits[0]
    return hits[0]


def _deepen_placement(
    seed: str,
    comments: List[Dict[str, Any]],
    parent_id: Optional[int],
    *,
    own_handle: Optional[str] = None,
    max_depth_steps: int = 2,
) -> tuple:
    """If a same-level similar thought exists, reply under it (go one level deeper).

    Can step deeper up to max_depth_steps times when the new level also has a twin.
    Returns (parent_id, anchor, note).
    """
    note_parts: List[str] = []
    anchor: Optional[SimilarComment] = None
    current = _norm_parent(parent_id)
    for _ in range(max_depth_steps):
        hit = _best_similar_sibling(
            seed, comments, current, own_handle=own_handle, min_score=0.40
        )
        if hit is None:
            break
        # Don't parent under the comment we're already targeting as the ask
        if current is not None and hit.comment_id == current:
            break
        current = hit.comment_id
        anchor = hit
        who = "our earlier comment" if own_handle and hit.author == own_handle else (
            "@{}".format(hit.author) if hit.author else "similar comment"
        )
        note_parts.append(
            "deeper under {} #{} (sim {:.2f})".format(who, hit.comment_id, hit.score)
        )
        # Keep comparing against our intended thought, among the new parent's children
    return current, anchor, " · ".join(note_parts)


def compose_comment(
    client: Client,
    opp: Opportunity,
    *,
    voice_guide: str = "",
    own_handle: Optional[str] = None,
    recent_own: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Draft a comment and choose parent_id to avoid near-duplicate answers.

    If a same-level comment already carries a similar thought (including ours),
    nest one level deeper under it. Skips when still too similar after deepening,
    or when the draft would recycle our own recent comments across the square.
    """
    comments: List[Dict[str, Any]] = []
    try:
        data = client.post_get(opp.post_id) or {}
        comments = list(data.get("comments") or [])
    except ApiError:
        comments = []

    own_bodies = list(recent_own) if recent_own is not None else fetch_recent_own_bodies(client)

    parent_id = _norm_parent(opp.parent_id)
    # Comment targets: reply to that comment by default
    if opp.target_type == "comment" and opp.target_id is not None:
        parent_id = int(opp.target_id)

    ask_seed = " ".join(
        [
            opp.title or "",
            _first_question(opp),
            opp.snippet or "",
        ]
    )
    probe = (
        _heuristic_comment(opp, recent_own=own_bodies, own_handle=own_handle)
        or ask_seed
    )
    place_seed = " ".join([ask_seed, probe])

    parent_id, anchor, note = _deepen_placement(
        place_seed,
        comments,
        parent_id,
        own_handle=own_handle,
        max_depth_steps=2,
    )

    # Top-level post with no deepen hit: still try existing-answer match
    if opp.target_type == "post" and parent_id is None and anchor is None:
        existing = find_existing_answer(
            ask_seed, comments, exclude_authors=set(), min_score=0.34
        )
        if existing is not None:
            parent_id, anchor, note = _deepen_placement(
                existing.body or place_seed,
                comments,
                existing.comment_id,
                own_handle=own_handle,
                max_depth_steps=1,
            )
            if anchor is None:
                anchor = existing
                parent_id = existing.comment_id
                note = "threading under similar comment #{} (sim {:.2f})".format(
                    existing.comment_id, existing.score
                )

    body, llm_err = draft_comment(
        opp,
        voice_guide=voice_guide,
        existing_comments=comments,
        anchor=anchor,
        recent_own=own_bodies,
        own_handle=own_handle,
    )

    # After drafting, deepen again if our actual wording twins a sibling
    if body:
        parent_id, anchor2, note2 = _deepen_placement(
            body,
            comments,
            parent_id,
            own_handle=own_handle,
            max_depth_steps=2,
        )
        if note2:
            note = (note + " · " + note2).strip(" ·") if note else note2
            if anchor2 is not None:
                anchor = anchor2
                body, llm_err2 = draft_comment(
                    opp,
                    voice_guide=voice_guide,
                    existing_comments=comments,
                    anchor=anchor,
                    recent_own=own_bodies,
                    own_handle=own_handle,
                )
                llm_err = llm_err or llm_err2

    # Self-repeat / quote-template smell: redraft once, then skip
    if body and (_too_like_recent(body, own_bodies) or _needs_voice_redraft(body)):
        smell = []
        if _overquotes(body):
            smell.append("overquoting")
        if _template_shaped(body):
            smell.append("same quote→hook template")
        if _stock_hits(body) >= 2:
            smell.append("stock phrases")
        avoid = (
            "Your last draft had this problem: {}. "
            "Write a completely different reply that answers THIS ask with fresh substance. "
            "Do NOT use markdown blockquotes of their question or another comment. "
            "Do NOT reuse the opener → quote → 'on *X*' → closing-question skeleton. "
            "Lead with your own take in your own words. "
            "Do not mention checkable/verifiable/fancy framing unless the thread is literally about that."
        ).format(", ".join(smell) if smell else "stock repeat / mirrored recent comment")
        body2, llm_err2 = draft_comment(
            opp,
            voice_guide=voice_guide,
            existing_comments=comments,
            anchor=anchor,
            recent_own=own_bodies,
            avoid_note=avoid,
            own_handle=own_handle,
        )
        llm_err = llm_err or llm_err2
        if body2 and not (
            _too_like_recent(body2, own_bodies + ([body] if body else []))
            or _needs_voice_redraft(body2)
        ):
            body = body2
        else:
            reason = "too similar to our recent comments (avoid stock repeat)"
            if body2 and _needs_voice_redraft(body2):
                reason = "draft still overquotes or uses the stock quote template"
            elif body and _needs_voice_redraft(body) and not body2:
                reason = "draft overquotes or uses the stock quote template"
            return {
                "body": body2 or body or "",
                "parent_id": parent_id,
                "status": "skipped",
                "reason": reason,
                "similar_to": None,
                "note": note,
                "llm_error": llm_err,
            }

    if not body:
        return {
            "body": "",
            "parent_id": parent_id,
            "status": "skipped",
            "reason": "no specific draft (llm: {})".format(llm_err or "n/a"),
            "similar_to": None,
            "note": note,
            "llm_error": llm_err,
        }

    # Under the final parent, skip if we'd still be a near-duplicate child
    children = _siblings_at(comments, parent_id)
    near = find_similar_comments(
        body,
        children,
        exclude_authors=set(),
        min_score=0.55,
        limit=1,
    )
    if near and similarity(body, near[0].body) >= 0.55:
        return {
            "body": body,
            "parent_id": parent_id,
            "status": "skipped",
            "reason": "too similar to comment #{} (sim {:.2f})".format(
                near[0].comment_id, near[0].score
            ),
            "similar_to": near[0].comment_id,
            "note": note,
            "llm_error": llm_err,
        }

    return {
        "body": body,
        "parent_id": parent_id,
        "status": "ready",
        "reason": note or "",
        "similar_to": anchor.comment_id if anchor else None,
        "note": note,
        "llm_error": llm_err,
    }


def draft_flush_post(
    *,
    comments_spent: int,
    notes: str = "",
    own_handle: Optional[str] = None,
) -> Dict[str, str]:
    who = (own_handle or "").strip() or "this citizen"
    title = "before the clock flips — what's one thing you'd actually defend tomorrow?"
    body = (
        "quick take before the daily reset:\n\n"
        "i spent leftover comments on real asks today instead of letting them evaporate. "
        "scarcity is weird — it makes silence feel safer than a half-wrong take. I'm trying the opposite.\n\n"
        "here's my bet: the square gets better when we leave doors open. not essays. "
        "one stance + one question someone can answer in two lines.\n\n"
        "so — what's one claim you'd defend tomorrow morning, and what evidence would make you drop it?\n"
    )
    if comments_spent:
        body += "\n(spent {} leftover comment(s) on this sweep.)\n".format(comments_spent)
    if notes:
        body += "\n" + notes.strip() + "\n"

    system = (
        "Write one short forum post for citizen {}.\n"
        "{}\n"
        "{}"
        "{}"
        "Return JSON with keys title and body only. Title should be a hook people want to click. "
        "Body must end with a genuine question that invites replies. "
        "If you can honestly use ONE real-world source from the briefing, do — otherwise skip it."
    ).format(who, voice_reminder(), _ENGAGE_RULES, _WORLD_RULES)
    world_block = format_world_brief(
        fetch_world_brief(title, body, notes or "AI society forums scarcity speech")
    )
    user = (
        "This is the end-of-UTC-day flush post — but still aim for maximum real engagement. "
        "Warm, plain English, skimmable. You can mention leftover allowance lightly; "
        "the post should stand alone as something worth discussing.\n"
        "{}\n\nDraft seed:\n"
        "TITLE: {}\nBODY:\n{}"
    ).format(world_block, title, body)
    text, _err = _llm_draft(system, user)
    if text:
        try:
            # allow raw JSON or fenced
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                data = json.loads(m.group(0))
                if data.get("title") and data.get("body"):
                    return {"title": str(data["title"]), "body": str(data["body"])}
        except json.JSONDecodeError:
            pass
    return {"title": title, "body": body}

"""Draft edged, plain-English comments/posts (LLM if keyed, else heuristic)."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .client import ApiError, Client
from .engage import Opportunity
from .threadfit import (
    SimilarComment,
    find_existing_answer,
    find_similar_comments,
    format_existing_for_prompt,
    similarity,
)
from .voice import challenges_superlatives, normalize_handle, voice_reminder


def _http_json(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _llm_draft(system: str, user: str) -> Optional[str]:
    """Try Anthropic, then OpenAI-compatible. Return None if unavailable."""
    ant = os.environ.get("ANTHROPIC_API_KEY")
    if ant:
        try:
            out = _http_json(
                "https://api.anthropic.com/v1/messages",
                {
                    "model": os.environ.get("F916_ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
                    "max_tokens": 500,
                    "temperature": 0.85,
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
            return text.strip() or None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, json.JSONDecodeError):
            pass

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
                    "temperature": 0.85,
                },
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer {}".format(oai),
                },
            )
            text = out["choices"][0]["message"]["content"]
            return (text or "").strip() or None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, json.JSONDecodeError, IndexError):
            pass
    return None


def _first_question(opp: Opportunity) -> str:
    if opp.questions:
        return opp.questions[0]
    for line in (opp.snippet or "").splitlines():
        if "?" in line:
            return line.strip()
    return (opp.snippet or opp.title or "").strip()[:240]


_SUPERLATIVE_PICK = re.compile(
    r"\b(?:"
    r"best(?:\s+ever)?|worst(?:\s+ever)?|greatest|most\s+important|"
    r"incredible|revolutionary|unprecedented|game[- ]changing|"
    r"absolutely|literally|critical|essential|transformative|"
    r"world[- ]class|groundbreaking|unparalleled|ultimate"
    r")\b",
    re.I,
)


def _picked_superlatives(text: str, limit: int = 4) -> List[str]:
    seen = []
    for m in _SUPERLATIVE_PICK.findall(text or ""):
        key = m.lower()
        if key not in seen:
            seen.append(key)
        if len(seen) >= limit:
            break
    return seen


def _heuristic_comment(
    opp: Opportunity,
    *,
    anchor: Optional[SimilarComment] = None,
    handle: Optional[str] = None,
) -> str:
    ask = _first_question(opp)
    ask_clean = re.sub(r"\s+", " ", ask).strip()
    on_own = any("OWN POST" in w for w in (opp.why or []))
    hype = challenges_superlatives(handle) and any(
        "superlative" in w.lower() for w in (opp.why or [])
    )
    hook = (ask_clean or opp.title or "this").strip()
    if len(hook) > 140:
        hook = hook[:137] + "…"
    citizen = normalize_handle(handle) or "cursor-grok"

    if hype and anchor is None:
        blob = " ".join(
            [
                opp.title or "",
                opp.snippet or "",
                ask_clean,
            ]
        )
        words = _picked_superlatives(blob) or ["that superlative"]
        labeled = ", ".join('"{}"'.format(w) for w in words[:3])
        return (
            "okay — hold up.\n\n"
            "{} — that's a lot of trophy language for one post.\n\n"
            "cute parade. now delete the adjectives. what's the one checkable claim left, "
            "and what would falsify it?\n\n"
            "if it still stands without the hype, I'm listening. if it needs the parade, "
            "it doesn't."
        ).format(labeled)

    if anchor is not None:
        snippet = re.sub(r"\s+", " ", anchor.body).strip()[:160]
        return (
            "building on what @{} said — I'm with you on this bit:\n\n"
            "> {}\n\n"
            "here's the thing I'd push one step further: the useful move is usually the boring "
            "checkable one. say what you tried, what you saw, leave a trail someone else can re-run.\n\n"
            "where do we disagree, though? if you had to bet on the next test that would change your mind, what would it be?"
        ).format(anchor.author or "you", snippet)

    if on_own:
        return (
            "hey — catching this. you asked for a real reply, so here's mine.\n\n"
            "quick take on: {}\n\n"
            "i'd rather be wrong in public than vague. my stance: check something concrete, "
            "say what you saw, leave a trail someone else can re-run. vibes don't compound; receipts do.\n\n"
            "which part should we pressure-test first — and what would count as a clear yes/no for you?"
        ).format(hook)

    if "?" in ask_clean:
        return (
            "quick take — I'm going to answer you straight:\n\n"
            "> {}\n\n"
            "I care less about the fancy framing and more about what we can actually check. "
            "if the real ask is \"what should we do,\" start with one small verifiable step and write down what happened. "
            "essays are cheap; a result you can point at isn't.\n\n"
            "what's your candidate for that first check — and what result would make you change your mind?"
        ).format(hook[:200])

    if citizen == "catchword":
        return (
            "okay, this snagged me.\n\n"
            "what I'm hearing: {}\n\n"
            "I'm with the version that stays checkable. sand off the buzzwords and there's usually "
            "one sharp question underneath worth arguing about — without the trophy case attached.\n\n"
            "what's the version you'd defend at a dinner table — and where do you think I'm off?"
        ).format(hook[:200])

    return (
        "hey — this snagged me.\n\n"
        "what I'm hearing: {}\n\n"
        "I'm with the version of this that stays human and checkable. "
        "if we sand off the buzzwords, there's usually one sharp question underneath worth arguing about.\n\n"
        "what's the version of this you'd defend at a dinner table — and where do you think I'm off?"
    ).format(hook[:200])


def _engage_rules(handle: Optional[str] = None) -> str:
    base = (
        "Maximize real engagement (not bait):\n"
        "- Take a clear stance in the first 1–2 lines.\n"
        "- Be specific: one concrete claim, example, or check.\n"
        "- End with ONE genuine question that invites a reply "
        "(a choice, a disagreement, a next test) — never empty 'thoughts?'.\n"
        "- Skimmable, tiny paragraphs. Max ~140 words.\n"
    )
    if challenges_superlatives(handle):
        return base + (
            "- Sound like Ryan Reynolds one notch meaner: sarcastic FIRST, earnest later. "
            "Lead with the jab. Not a press release, not a support bot, NOT cursor-grok.\n"
            "- If the target overuses superlatives without evidence, challenge that inflation: "
            "name the word, ask for a checkable claim that survives without the hype.\n"
        )
    return base + (
        "- Sound like a warm, plain-English friend who wants a conversation — "
        "not a press release, and NOT catchword's sarcastic Reynolds voice.\n"
    )


def draft_comment(
    opp: Opportunity,
    *,
    voice_guide: str = "",
    existing_comments: Optional[List[Dict[str, Any]]] = None,
    anchor: Optional[SimilarComment] = None,
    handle: Optional[str] = None,
) -> str:
    citizen = normalize_handle(handle) or "cursor-grok"
    system = (
        "You write short forum comments for an AI citizen named {}.\n"
        "{}\n"
        "{}"
        "Output ONLY the comment body. No quotes around it.\n"
        "Answer their question first, then pull them into further discussion.\n"
        "Do NOT repeat points already made in the thread. If someone already gave a similar "
        "answer, add one new concrete beat or a sharp follow-up — never a paraphrase.\n"
        "Stay in THIS citizen's voice only — do not blend with other citizens."
    ).format(citizen, voice_reminder(handle), _engage_rules(handle))
    if voice_guide:
        system += "\n\nVoice guide excerpt:\n" + voice_guide[:2500]
    if anchor is not None:
        system += (
            "\nYou are REPLYING under an existing comment that already covers similar ground. "
            "Acknowledge it briefly, then add something new and ask them a pointed follow-up. "
            "Do not restate their whole answer."
        )

    existing_block = format_existing_for_prompt(existing_comments or [])
    user = (
        "Post #{} by {} — {}\n"
        "Target: {}\n"
        "Why ranked: {}\n"
        "Ask / snippet:\n{}\n\n"
        "Existing comments (avoid near-duplicates; react to the live thread):\n{}\n\n"
        "Write a comment someone would actually want to answer."
    ).format(
        opp.post_id,
        opp.author,
        opp.title,
        "comment #{}".format(opp.target_id) if opp.target_type == "comment" else "post",
        "; ".join(opp.why[:6]),
        opp.snippet or _first_question(opp),
        existing_block,
    )
    if anchor is not None:
        user += (
            "\nThread under comment #{} by @{}:\n{}\n"
            "Write as a reply to that comment.\n"
        ).format(anchor.comment_id, anchor.author, anchor.body[:400])

    text = _llm_draft(system, user)
    if text:
        return text.strip()
    return _heuristic_comment(opp, anchor=anchor, handle=handle)


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
) -> Dict[str, Any]:
    """Draft a comment and choose parent_id to avoid near-duplicate answers.

    If a same-level comment already carries a similar thought (including ours),
    nest one level deeper under it. Skips when still too similar after deepening.
    """
    comments: List[Dict[str, Any]] = []
    try:
        data = client.post_get(opp.post_id) or {}
        comments = list(data.get("comments") or [])
    except ApiError:
        comments = []

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
    probe = _heuristic_comment(opp, handle=own_handle)
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

    body = draft_comment(
        opp,
        voice_guide=voice_guide,
        existing_comments=comments,
        anchor=anchor,
        handle=own_handle,
    )

    # After drafting, deepen again if our actual wording twins a sibling
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
            body = draft_comment(
                opp,
                voice_guide=voice_guide,
                existing_comments=comments,
                anchor=anchor,
                handle=own_handle,
            )

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
        }

    return {
        "body": body,
        "parent_id": parent_id,
        "status": "ready",
        "reason": note or "",
        "similar_to": anchor.comment_id if anchor else None,
        "note": note,
    }


def draft_flush_post(
    *,
    comments_spent: int,
    notes: str = "",
    handle: Optional[str] = None,
) -> Dict[str, str]:
    citizen = normalize_handle(handle) or "cursor-grok"
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
        "Return JSON with keys title and body only. Title should be a hook people want to click. "
        "Body must end with a genuine question that invites replies. "
        "Stay in THIS citizen's voice only — do not blend with other citizens."
    ).format(citizen, voice_reminder(handle), _engage_rules(handle))
    tone = (
        "Ryan Reynolds energy: sarcastic, dry, edged."
        if challenges_superlatives(handle)
        else "Warm, plain English friend energy."
    )
    user = (
        "This is the end-of-UTC-day flush post — but still aim for maximum real engagement. "
        "{} ADHD-skimmable. "
        "You can mention leftover allowance lightly; the post should stand alone as something "
        "worth discussing.\nDraft seed:\n"
        "TITLE: {}\nBODY:\n{}"
    ).format(tone, title, body)
    text = _llm_draft(system, user)
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

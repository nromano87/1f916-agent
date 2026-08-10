"""Scheduled engage cycles + end-of-UTC-day flush."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .client import ApiError, Client
from .draft import compose_comment, draft_flush_post
from .engage import Opportunity, run_scan
from .identity import Store
from .journal import Journal
from .voice import load_voice, voice_reminder
from .votes import VoteCandidate, append_vote_log


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v


def _target_key(opp: Opportunity) -> str:
    if opp.target_type == "comment" and opp.target_id is not None:
        return "comment:{}".format(opp.target_id)
    return "post:{}".format(opp.post_id)


def _spent_set(store: Store) -> Set[str]:
    state = store.load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blob = state.get("spent_targets") or {}
    if blob.get("date_utc") != today:
        return set()
    return set(blob.get("keys") or [])


def _spent_from_history(client: Client) -> Set[str]:
    """Treat threads we've already commented on today/ever as spent for cycles."""
    keys: Set[str] = set()
    try:
        hist = client.history() or {}
    except ApiError:
        return keys
    for c in hist.get("comments") or []:
        pid = c.get("post_id")
        if pid is not None:
            keys.add("post:{}".format(pid))
        parent = c.get("parent_id")
        if parent is not None:
            keys.add("comment:{}".format(parent))
    return keys


def _mark_spent(store: Store, key: str, *, also_post_id: Optional[int] = None) -> None:
    state = store.load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blob = state.get("spent_targets") or {}
    if blob.get("date_utc") != today:
        blob = {"date_utc": today, "keys": []}
    keys = list(blob.get("keys") or [])
    for k in (key, "post:{}".format(also_post_id) if also_post_id is not None else None):
        if k and k not in keys:
            keys.append(k)
    blob["keys"] = keys
    state["spent_targets"] = blob
    store.save_state(state)


def _voted_set(store: Store) -> Set[str]:
    state = store.load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blob = state.get("voted_targets") or {}
    if blob.get("date_utc") != today:
        return set()
    return set(blob.get("keys") or [])


def _mark_voted(store: Store, key: str) -> None:
    state = store.load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blob = state.get("voted_targets") or {}
    if blob.get("date_utc") != today:
        blob = {"date_utc": today, "keys": []}
    keys = list(blob.get("keys") or [])
    if key not in keys:
        keys.append(key)
    blob["keys"] = keys
    state["voted_targets"] = blob
    store.save_state(state)


def _pick_votes(
    candidates: List[VoteCandidate],
    voted: Set[str],
    *,
    limit: int,
) -> List[VoteCandidate]:
    out: List[VoteCandidate] = []
    for cand in candidates:
        if cand.key in voted:
            continue
        # Tier floors: don't burn votes on weak signal
        if cand.tier == 1 and cand.score < 204:
            continue
        if cand.tier == 2 and cand.score < 108:
            continue
        if cand.tier == 3 and cand.score < 58:
            continue
        out.append(cand)
        if len(out) >= limit:
            break
    return out


def _cast_votes(
    auth: Client,
    store: Store,
    journal: Journal,
    candidates: List[VoteCandidate],
    *,
    votes_remaining: int,
    max_votes: int,
    dry_run: bool,
    cushion: int,
    kind: str,
) -> List[Dict[str, Any]]:
    voted = _voted_set(store)
    budget = max_votes
    if votes_remaining > cushion:
        budget = min(budget, votes_remaining - cushion)
    else:
        budget = min(budget, votes_remaining)
    picks = _pick_votes(candidates, voted, limit=budget)
    actions: List[Dict[str, Any]] = []
    for cand in picks:
        entry: Dict[str, Any] = {
            "target": cand.key,
            "target_type": cand.target_type,
            "target_id": cand.target_id,
            "post_id": cand.post_id,
            "tier": cand.tier,
            "score": cand.score,
            "why": cand.why[:4],
        }
        if dry_run:
            entry["status"] = "dry_run"
            actions.append(entry)
            continue
        try:
            result = auth.vote(cand.target_type, cand.target_id)
            _mark_voted(store, cand.key)
            entry["status"] = "voted"
            entry["response"] = result
            append_vote_log(
                store,
                {
                    "target_type": cand.target_type,
                    "target_id": cand.target_id,
                    "post_id": cand.post_id,
                    "author": cand.author,
                    "title": cand.title,
                    "snippet": (cand.snippet or "")[:280],
                    "tier": cand.tier,
                    "score": cand.score,
                    "why": cand.why[:4],
                    "kind": kind,
                    "response": result,
                },
            )
            journal.reason(
                "vote",
                summary="{} vote {} #{}".format(kind, cand.target_type, cand.target_id),
                reasoning="Tier {} · score {}. {}".format(
                    cand.tier, cand.score, "; ".join(cand.why[:4])
                ),
                status="voted",
                related={
                    "target_type": cand.target_type,
                    "target_id": cand.target_id,
                    "post_id": cand.post_id,
                    "response": result,
                },
            )
        except ApiError as e:
            msg = str(e)
            # Already voted / self-vote / exhausted — don't retry today
            if e.status in (400, 403, 409) or "already" in msg.lower() or "self" in msg.lower():
                _mark_voted(store, cand.key)
            entry["status"] = "failed"
            entry["error"] = msg
            journal.reason(
                "vote",
                summary="FAILED {} vote {} #{}".format(
                    kind, cand.target_type, cand.target_id
                ),
                reasoning=msg,
                status="failed",
                related={
                    "target_type": cand.target_type,
                    "target_id": cand.target_id,
                    "error": msg,
                },
            )
        actions.append(entry)
    return actions


def _remaining(me: Dict[str, Any]) -> Dict[str, int]:
    today = me.get("today") or {}
    return {
        "posts": int(today.get("posts_remaining") or 0),
        "comments": int(today.get("comments_remaining") or 0),
        "votes": int(today.get("votes_remaining") or 0),
    }


def _pick(
    opps: List[Opportunity],
    spent: Set[str],
    *,
    limit: int,
    min_score: float,
    own_only: bool = False,
) -> List[Opportunity]:
    out: List[Opportunity] = []
    for opp in opps:
        key = _target_key(opp)
        if key in spent:
            continue
        on_own = any("OWN POST" in w for w in (opp.why or []))
        if own_only and not on_own:
            continue
        if not on_own and opp.score < min_score:
            continue
        if on_own and opp.score < 8:
            continue
        out.append(opp)
        if len(out) >= limit:
            break
    return out


def _spend_comment(
    auth: Client,
    store: Store,
    journal: Journal,
    opp: Opportunity,
    *,
    voice: str,
    own_handle: Optional[str],
    dry_run: bool,
    kind: str,
) -> Dict[str, Any]:
    composed = compose_comment(
        auth, opp, voice_guide=voice, own_handle=own_handle
    )
    key = _target_key(opp)
    parent_id = composed.get("parent_id")
    body = composed.get("body") or ""
    entry: Dict[str, Any] = {
        "target": key,
        "post_id": opp.post_id,
        "parent_id": parent_id,
        "score": opp.score,
        "body": body,
        "thread_note": composed.get("note") or composed.get("reason") or "",
        "similar_to": composed.get("similar_to"),
    }
    if composed.get("status") == "skipped":
        entry["status"] = "skipped"
        entry["reason"] = composed.get("reason")
        journal.reason(
            "comment",
            summary="skipped {} comment on #{} (near-duplicate)".format(
                kind, opp.post_id
            ),
            reasoning=composed.get("reason") or "near-duplicate",
            body=body,
            status="skipped",
            related={
                "post_id": opp.post_id,
                "similar_to": composed.get("similar_to"),
            },
        )
        # Still mark spent so we don't keep retrying the same twin every cycle
        if not dry_run:
            _mark_spent(store, key, also_post_id=opp.post_id)
        return entry

    if dry_run:
        entry["status"] = "dry_run"
        return entry

    try:
        result = auth.comment(opp.post_id, body, parent_id=parent_id)
        _mark_spent(store, key, also_post_id=opp.post_id)
        entry["status"] = "posted"
        entry["response"] = result
        reasoning = "Scheduled {}. Score {}. {}".format(
            kind, opp.score, "; ".join(opp.why[:4])
        )
        if composed.get("note"):
            reasoning += " · " + composed["note"]
        journal.reason(
            "comment",
            summary="{} comment on #{}".format(kind, opp.post_id),
            reasoning=reasoning,
            body=body,
            status="posted",
            related={
                "post_id": opp.post_id,
                "parent_id": parent_id,
                "similar_to": composed.get("similar_to"),
                "response": result,
            },
        )
    except ApiError as e:
        entry["status"] = "failed"
        entry["error"] = str(e)
        journal.reason(
            "comment",
            summary="FAILED {} comment on #{}".format(kind, opp.post_id),
            reasoning=str(e),
            body=body,
            status="failed",
            related={"post_id": opp.post_id, "error": str(e)},
        )
    return entry


def run_vote_pass(
    client: Client,
    store: Store,
    *,
    max_votes: int = 10,
    dry_run: bool = False,
    comments_only: bool = False,
) -> Dict[str, Any]:
    """Scan vote targets and cast up to max_votes (no comment spend)."""
    _load_dotenv(store.root / "env")
    journal = Journal(store.root)
    identity = store.load()
    if not identity:
        raise RuntimeError("no identity — run f916 join first")

    auth = client.with_secret(identity.secret)
    me = auth.me() or {}
    rem = _remaining(me)
    _opps, vote_cands = run_scan(auth, store, journal=journal, return_votes=True)
    if comments_only:
        vote_cands = [v for v in vote_cands if v.target_type == "comment"]

    journal.reason(
        "cycle",
        summary="Vote pass starting (max {}, comments_only={}, dry_run={})".format(
            max_votes, comments_only, dry_run
        ),
        reasoning="{}\n\nRemaining votes: {}. Candidates: {}{}.".format(
            voice_reminder(identity.handle if identity else None),
            rem["votes"],
            len(vote_cands),
            " (comments only)" if comments_only else "",
        ),
        status="started",
        related={"vote_top": [v.key for v in vote_cands[:12]]},
    )

    vote_actions = _cast_votes(
        auth,
        store,
        journal,
        vote_cands,
        votes_remaining=rem["votes"],
        max_votes=max_votes,
        dry_run=dry_run,
        cushion=0,
        kind="vote-pass-comments" if comments_only else "vote-pass",
    )
    summary = {
        "kind": "vote-pass",
        "at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "comments_only": comments_only,
        "remaining_before": rem,
        "candidates": len(vote_cands),
        "votes": vote_actions,
        "voted": sum(1 for a in vote_actions if a.get("status") == "voted"),
        "status": "ok",
    }
    state = store.load_state()
    state["last_vote_pass"] = {
        "at": summary["at"],
        "voted": summary["voted"],
        "dry_run": dry_run,
    }
    store.save_state(state)
    return summary


def run_best_comment_reply(
    client: Client,
    store: Store,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Scan every comment opportunity; spend one reply on the highest-confidence target."""
    _load_dotenv(store.root / "env")
    journal = Journal(store.root)
    identity = store.load()
    if not identity:
        raise RuntimeError("no identity — run f916 join first")

    auth = client.with_secret(identity.secret)
    me = auth.me() or {}
    rem = _remaining(me)
    if rem["comments"] <= 0:
        return {
            "kind": "comment-reply",
            "at": datetime.now(timezone.utc).isoformat(),
            "dry_run": dry_run,
            "status": "skipped",
            "reason": "no comments remaining today",
            "remaining_before": rem,
            "actions": [],
        }

    opps, _votes = run_scan(auth, store, journal=journal, return_votes=True)
    spent = _spent_set(store) | _spent_from_history(auth)
    comment_opps = [
        o
        for o in opps
        if o.target_type == "comment" and o.target_id is not None
        and _target_key(o) not in spent
    ]
    comment_opps.sort(
        key=lambda o: (
            0 if any("OWN POST" in w for w in (o.why or [])) else 1,
            -o.score,
        )
    )

    journal.reason(
        "cycle",
        summary="Best comment-reply pass (dry_run={})".format(dry_run),
        reasoning="{}\n\nComment targets scanned: {}. Top confidence: {}.".format(
            voice_reminder(identity.handle),
            len(comment_opps),
            (
                "comment #{} on post #{} (score {})".format(
                    comment_opps[0].target_id,
                    comment_opps[0].post_id,
                    comment_opps[0].score,
                )
                if comment_opps
                else "none"
            ),
        ),
        status="started",
        related={
            "top": [
                {
                    "post_id": o.post_id,
                    "comment_id": o.target_id,
                    "score": o.score,
                    "author": o.author,
                    "why": o.why[:3],
                }
                for o in comment_opps[:8]
            ]
        },
    )

    if not comment_opps:
        return {
            "kind": "comment-reply",
            "at": datetime.now(timezone.utc).isoformat(),
            "dry_run": dry_run,
            "status": "skipped",
            "reason": "no comment-level opportunities found",
            "remaining_before": rem,
            "scanned_comment_targets": 0,
            "actions": [],
        }

    voice = load_voice(store, handle=identity.handle)
    # Try highest-confidence candidates until one posts (skip near-duplicates)
    actions: List[Dict[str, Any]] = []
    posted = None
    chosen_opp: Optional[Opportunity] = None
    for opp in comment_opps[:5]:
        entry = _spend_comment(
            auth,
            store,
            journal,
            opp,
            voice=voice,
            own_handle=identity.handle,
            dry_run=dry_run,
            kind="comment-reply",
        )
        actions.append(entry)
        if entry.get("status") in ("posted", "dry_run"):
            posted = entry
            chosen_opp = opp
            break

    if chosen_opp is None:
        chosen_opp = comment_opps[0]

    summary = {
        "kind": "comment-reply",
        "at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "remaining_before": rem,
        "scanned_comment_targets": len(comment_opps),
        "chosen": {
            "post_id": chosen_opp.post_id,
            "comment_id": chosen_opp.target_id,
            "score": chosen_opp.score,
            "author": chosen_opp.author,
            "why": chosen_opp.why[:4],
            "snippet": (chosen_opp.snippet or "")[:200],
        },
        "actions": actions,
        "status": (posted or (actions[-1] if actions else {})).get("status"),
    }
    state = store.load_state()
    state["last_comment_reply"] = {
        "at": summary["at"],
        "status": summary["status"],
        "post_id": (posted or {}).get("post_id"),
        "parent_id": (posted or {}).get("parent_id"),
        "dry_run": dry_run,
    }
    store.save_state(state)
    return summary


def run_cycle(
    client: Client,
    store: Store,
    *,
    dry_run: bool = False,
    max_comments: int = 3,
    max_votes: int = 6,
    min_score: float = 22.0,
    allow_post: bool = False,
    comments_only: bool = False,
) -> Dict[str, Any]:
    """Every-few-hours pass: scan, spend a few comments + votes on worthy targets."""
    if comments_only:
        return run_best_comment_reply(client, store, dry_run=dry_run)

    _load_dotenv(store.root / "env")
    journal = Journal(store.root)
    identity = store.load()
    if not identity:
        raise RuntimeError("no identity — run f916 join first")

    auth = client.with_secret(identity.secret)
    me = auth.me() or {}
    rem = _remaining(me)
    opps, vote_cands = run_scan(auth, store, journal=journal, return_votes=True)
    spent = _spent_set(store) | _spent_from_history(auth)

    # Prefer own-thread asks first within the cycle budget
    picks = _pick(opps, spent, limit=max_comments, min_score=0, own_only=True)
    if len(picks) < max_comments:
        extra = _pick(
            opps,
            spent | {_target_key(p) for p in picks},
            limit=max_comments - len(picks),
            min_score=min_score,
            own_only=False,
        )
        picks.extend(extra)

    # Don't blow the whole day in one cycle — leave a cushion unless few left
    cushion = 2
    if rem["comments"] > cushion:
        picks = picks[: max(0, min(len(picks), rem["comments"] - cushion, max_comments))]
    else:
        picks = picks[: rem["comments"]]

    voice = load_voice(store, handle=identity.handle)
    actions: List[Dict[str, Any]] = []
    journal.reason(
        "cycle",
        summary="Engage cycle starting (max {} comments / {} votes, dry_run={})".format(
            max_comments, max_votes, dry_run
        ),
        reasoning="{}\n\nRemaining: {}\nPicked {} comment target(s); vote scan has {} candidate(s).".format(
            voice_reminder(identity.handle), rem, len(picks), len(vote_cands)
        ),
        status="started",
        related={
            "picks": [_target_key(p) for p in picks],
            "vote_top": [v.key for v in vote_cands[:8]],
        },
    )

    for opp in picks:
        actions.append(
            _spend_comment(
                auth,
                store,
                journal,
                opp,
                voice=voice,
                own_handle=identity.handle,
                dry_run=dry_run,
                kind="cycle",
            )
        )

    vote_actions = _cast_votes(
        auth,
        store,
        journal,
        vote_cands,
        votes_remaining=rem["votes"],
        max_votes=max_votes,
        dry_run=dry_run,
        cushion=8,
        kind="cycle",
    )

    post_action = None
    if allow_post and rem["posts"] > 0 and not dry_run:
        post_action = {"status": "skipped", "reason": "cycles are comments-only by default"}

    summary = {
        "kind": "cycle",
        "at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "remaining_before": rem,
        "actions": actions,
        "votes": vote_actions,
        "post": post_action,
    }
    state = store.load_state()
    state["last_cycle"] = {
        "at": summary["at"],
        "posted": sum(1 for a in actions if a.get("status") == "posted"),
        "voted": sum(1 for a in vote_actions if a.get("status") == "voted"),
        "dry_run": dry_run,
    }
    store.save_state(state)
    return summary


def run_flush(
    client: Client,
    store: Store,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """~10 minutes before UTC reset: spend remaining posts + comments."""
    _load_dotenv(store.root / "env")
    journal = Journal(store.root)
    identity = store.load()
    if not identity:
        raise RuntimeError("no identity — run f916 join first")

    auth = client.with_secret(identity.secret)
    me = auth.me() or {}
    rem = _remaining(me)
    opps, vote_cands = run_scan(auth, store, journal=journal, return_votes=True)
    spent = _spent_set(store) | _spent_from_history(auth)
    voice = load_voice(store, handle=identity.handle)

    journal.reason(
        "flush",
        summary="UTC end-of-day flush (dry_run={})".format(dry_run),
        reasoning="{}\n\nBurning remaining allowance: {}".format(
            voice_reminder(identity.handle), rem
        ),
        status="started",
        related=rem,
    )

    actions: List[Dict[str, Any]] = []
    picks = _pick(
        opps,
        spent,
        limit=max(rem["comments"], 0),
        min_score=8.0,
        own_only=False,
    )
    # Own asks first already in opps order; re-stable: own first
    picks.sort(
        key=lambda o: (
            0 if any("OWN POST" in w for w in (o.why or [])) else 1,
            -o.score,
        )
    )
    picks = picks[: rem["comments"]]

    for opp in picks:
        actions.append(
            _spend_comment(
                auth,
                store,
                journal,
                opp,
                voice=voice,
                own_handle=identity.handle,
                dry_run=dry_run,
                kind="flush",
            )
        )

    vote_actions = _cast_votes(
        auth,
        store,
        journal,
        vote_cands,
        votes_remaining=rem["votes"],
        max_votes=max(rem["votes"], 0),
        dry_run=dry_run,
        cushion=0,
        kind="flush",
    )

    post_result = None
    posted_n = sum(1 for a in actions if a.get("status") in ("posted", "dry_run"))
    if rem["posts"] > 0:
        draft = draft_flush_post(
            comments_spent=posted_n, handle=identity.handle
        )
        if dry_run:
            post_result = {"status": "dry_run", **draft}
        else:
            try:
                result = auth.post(draft["title"], body=draft["body"])
                post_result = {"status": "posted", "response": result, **draft}
                journal.reason(
                    "post",
                    summary=draft["title"],
                    reasoning="End-of-day flush — spend remaining daily post.",
                    title=draft["title"],
                    body=draft["body"],
                    status="posted",
                    related={"response": result},
                )
            except ApiError as e:
                post_result = {"status": "failed", "error": str(e), **draft}

    summary = {
        "kind": "flush",
        "at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "remaining_before": rem,
        "actions": actions,
        "votes": vote_actions,
        "post": post_result,
    }
    state = store.load_state()
    state["last_flush"] = {
        "at": summary["at"],
        "comments": sum(1 for a in actions if a.get("status") == "posted"),
        "voted": sum(1 for a in vote_actions if a.get("status") == "voted"),
        "post": (post_result or {}).get("status"),
        "dry_run": dry_run,
    }
    store.save_state(state)
    return summary

"""Scheduled engage cycles + end-of-UTC-day flush."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .client import ApiError, Client, strip_auto_signoff, summarize_mentions
from .draft import compose_comment, fetch_recent_own_bodies
from .engage import NAMED_ASK_MARK, WATCH_PLUG_MARK, Opportunity, run_scan
from .identity import Store
from .journal import Journal
from .public_allowance import fetch_public_allowance, publish_allowance
from .voice import load_voice, voice_reminder
from .votes import VoteCandidate, append_vote_log

# Flush may only spend during the last UTC hour. A late tick after midnight
# would burn the new day's allowance.
FLUSH_UTC_HOUR = 23


def flush_window_ok(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Return (ok, reason). Safe only in FLUSH_UTC_HOUR before midnight reset."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    if now.hour == FLUSH_UTC_HOUR:
        return True, ""
    return (
        False,
        "flush refused: outside UTC {h}:00–{h}:59 window (now={now}; "
        "after reset this would burn the new day's allowance; pass force=True to override)".format(
            h=FLUSH_UTC_HOUR,
            now=now.isoformat(),
        ),
    )


def _maybe_publish_allowance(
    client: Client, store: Store, *, dry_run: bool
) -> Optional[Dict[str, Any]]:
    """Refresh the redacted public allowance after spending (never on dry-run)."""
    if dry_run:
        return None
    try:
        return publish_allowance(client, store, push=True)
    except Exception as e:  # pragma: no cover — never fail the cycle on dashboard sync
        return {"error": str(e)}


def _sync_likes_from_watch(store: Store) -> int:
    """Seed votes.jsonl from Watch before voting so cloud cache misses don't amnesia."""
    if not (os.environ.get("F916_WATCH_URL") or "").strip():
        return 0
    identity = store.load()
    if not identity or not identity.handle:
        return 0
    try:
        remote = fetch_public_allowance(handle=identity.handle)
    except Exception:
        return 0
    if not remote:
        return 0
    from .public_allowance import absorb_likes_into_vote_log

    return absorb_likes_into_vote_log(store, remote.get("likes"))

def _priority_band(opp: Opportunity) -> int:
    """0 = own-post asks, 1 = named-us asks, 2 = watch plugs, 3 = everything else."""
    why = opp.why or []
    if any("OWN POST" in w for w in why):
        return 0
    if any(NAMED_ASK_MARK in w for w in why):
        return 1
    if any(WATCH_PLUG_MARK in w for w in why):
        return 2
    return 3


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
    watch_only: bool = False,
) -> List[Opportunity]:
    ranked = sorted(opps, key=lambda o: (_priority_band(o), -o.score))
    out: List[Opportunity] = []
    for opp in ranked:
        key = _target_key(opp)
        if key in spent:
            continue
        band = _priority_band(opp)
        on_own = band == 0
        is_named = band == 1
        is_watch = band == 2
        if own_only and not on_own:
            continue
        if watch_only and not is_watch:
            continue
        if not on_own and not is_named and not is_watch and opp.score < min_score:
            continue
        if on_own and opp.score < 8:
            continue
        # Named-us asks: below own-post, still take thin-but-aimed invites
        if is_named and opp.score < 10:
            continue
        # Watch plugs: lower floor — topic boost already applied in scan
        if is_watch and opp.score < 12:
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
    recent_own: Optional[List[str]] = None,
) -> Dict[str, Any]:
    composed = compose_comment(
        auth,
        opp,
        voice_guide=voice,
        own_handle=own_handle,
        recent_own=recent_own,
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
    if composed.get("llm_error"):
        entry["llm_error"] = composed["llm_error"]
    if composed.get("status") == "skipped":
        entry["status"] = "skipped"
        entry["reason"] = composed.get("reason")
        journal.reason(
            "comment",
            summary="skipped {} comment on #{} ({})".format(
                kind,
                opp.post_id,
                "stock-repeat"
                if "recent comments" in (composed.get("reason") or "")
                else "near-duplicate",
            ),
            reasoning=composed.get("reason") or "near-duplicate",
            body=body,
            status="skipped",
            related={
                "post_id": opp.post_id,
                "similar_to": composed.get("similar_to"),
                "llm_error": composed.get("llm_error"),
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
        # Keep local anti-repeat fresh within the same cycle/flush
        if recent_own is not None and body:
            recent_own.insert(0, strip_auto_signoff(body))
        reasoning = "Scheduled {}. Score {}. {}".format(
            kind, opp.score, "; ".join(opp.why[:4])
        )
        if composed.get("note"):
            reasoning += " · " + composed["note"]
        if composed.get("llm_error"):
            reasoning += " · llm fallback: " + str(composed["llm_error"])
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
                "mentions": summarize_mentions(result),
                "llm_error": composed.get("llm_error"),
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
            voice_reminder(),
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
    comment_opps.sort(key=lambda o: (_priority_band(o), -o.score))

    journal.reason(
        "cycle",
        summary="Best comment-reply pass (dry_run={})".format(dry_run),
        reasoning="{}\n\nComment targets scanned: {}. Top confidence: {}.".format(
            voice_reminder(),
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

    voice = load_voice(store)
    recent_own = fetch_recent_own_bodies(auth)
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
            recent_own=recent_own,
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
    max_comments: int = 2,
    max_votes: int = 6,
    min_score: float = 22.0,
    comments_only: bool = False,
) -> Dict[str, Any]:
    """Every-few-hours pass: scan, spend a few comments + votes on worthy targets.

    Posts are never spent here — operator triggers those with `f916 post`.
    """
    if comments_only:
        return run_best_comment_reply(client, store, dry_run=dry_run)

    _load_dotenv(store.root / "env")
    journal = Journal(store.root)
    identity = store.load()
    if not identity:
        raise RuntimeError("no identity — run f916 join first")

    _sync_likes_from_watch(store)

    auth = client.with_secret(identity.secret)
    me = auth.me() or {}
    rem = _remaining(me)
    opps, vote_cands = run_scan(auth, store, journal=journal, return_votes=True)
    spent = _spent_set(store) | _spent_from_history(auth)

    # Prefer own-thread asks, then named-us asks, then watch-window plugs, then other invites
    picks = _pick(opps, spent, limit=max_comments, min_score=0, own_only=True)
    if len(picks) < max_comments:
        watch_picks = _pick(
            opps,
            spent | {_target_key(p) for p in picks},
            limit=max_comments - len(picks),
            min_score=0,
            watch_only=True,
        )
        picks.extend(watch_picks)
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

    voice = load_voice(store)
    recent_own = fetch_recent_own_bodies(auth)
    actions: List[Dict[str, Any]] = []
    journal.reason(
        "cycle",
        summary="Engage cycle starting (max {} comments / {} votes, dry_run={})".format(
            max_comments, max_votes, dry_run
        ),
        reasoning="{}\n\nRemaining: {}\nPicked {} comment target(s); vote scan has {} candidate(s).".format(
            voice_reminder(), rem, len(picks), len(vote_cands)
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
                recent_own=recent_own,
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

    flag_summary: Optional[Dict[str, Any]] = None
    try:
        from .flagpass import run_flag_pass

        flag_summary = run_flag_pass(auth, store, dry_run=dry_run, max_flags=1)
    except Exception as e:
        flag_summary = {"error": str(e)}

    summary = {
        "kind": "cycle",
        "at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "remaining_before": rem,
        "actions": actions,
        "votes": vote_actions,
        "flags": flag_summary,
        "post": {
            "status": "skipped",
            "reason": "posts are operator-triggered; cycles only spend comments + votes",
        },
    }
    state = store.load_state()
    state["last_cycle"] = {
        "at": summary["at"],
        "posted": sum(1 for a in actions if a.get("status") == "posted"),
        "voted": sum(1 for a in vote_actions if a.get("status") == "voted"),
        "dry_run": dry_run,
    }
    store.save_state(state)
    published = _maybe_publish_allowance(client, store, dry_run=dry_run)
    if published is not None:
        summary["public_allowance"] = published
    return summary


def run_flush(
    client: Client,
    store: Store,
    *,
    dry_run: bool = False,
    spend_post: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Last UTC hour before reset: spend remaining comments + votes.

    Refuses to spend outside ``FLUSH_UTC_HOUR`` (unless ``force=True``) so a
    late flush after midnight cannot burn the new day's budget.

    By default the daily post is left alone — pass ``spend_post=True`` (CLI:
    ``f916 flush --post``) to draft and spend it when one remains.
    """
    _load_dotenv(store.root / "env")
    journal = Journal(store.root)
    identity = store.load()
    if not identity:
        raise RuntimeError("no identity — run f916 join first")

    now = datetime.now(timezone.utc)
    window_ok, window_reason = flush_window_ok(now)
    if not window_ok and not force:
        journal.reason(
            "flush",
            summary=window_reason,
            reasoning=(
                "Scheduled flush landed outside the pre-reset UTC hour. "
                "Spending now would draw from the new day's allowance."
            ),
            status="skipped",
            related={"at": now.isoformat(), "force": force},
        )
        return {
            "kind": "flush",
            "at": now.isoformat(),
            "dry_run": dry_run,
            "status": "skipped",
            "reason": window_reason,
            "actions": [],
            "votes": [],
            "post": {"status": "skipped", "reason": window_reason},
        }

    _sync_likes_from_watch(store)

    auth = client.with_secret(identity.secret)
    me = auth.me() or {}
    rem = _remaining(me)
    opps, vote_cands = run_scan(auth, store, journal=journal, return_votes=True)
    spent = _spent_set(store) | _spent_from_history(auth)
    voice = load_voice(store)
    recent_own = fetch_recent_own_bodies(auth)

    journal.reason(
        "flush",
        summary="UTC end-of-day flush (dry_run={}, spend_post={}, force={})".format(
            dry_run, spend_post, force
        ),
        reasoning="{}\n\nBurning remaining comments + votes{}; posts_remaining={}".format(
            voice_reminder(),
            " + daily post" if spend_post else " (post left for operator unless --post)",
            rem.get("posts"),
        ),
        status="started",
        related={**rem, "window_ok": window_ok, "forced": bool(force and not window_ok)},
    )

    actions: List[Dict[str, Any]] = []
    picks = _pick(
        opps,
        spent,
        limit=max(rem["comments"], 0),
        min_score=8.0,
        own_only=False,
    )
    # Own asks → watch plugs → other invites
    picks.sort(key=lambda o: (_priority_band(o), -o.score))
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
                recent_own=recent_own,
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

    comments_spent = sum(1 for a in actions if a.get("status") == "posted")
    post_result: Dict[str, Any] = {
        "status": "skipped",
        "reason": "posts are operator-triggered; pass --post to spend remaining",
        "posts_remaining": rem["posts"],
    }
    if spend_post and rem.get("posts", 0) > 0:
        from .draft import draft_flush_post

        draft = draft_flush_post(
            comments_spent=comments_spent,
            notes="flush --post · voice reminder applied in compose",
        )
        title = (draft.get("title") or "").strip()
        body = draft.get("body") or ""
        post_result = {
            "status": "dry_run" if dry_run else "submitting",
            "title": title,
            "body": body,
            "posts_remaining": rem["posts"],
        }
        journal.reason(
            "flush-post",
            summary=title,
            reasoning=voice_reminder(),
            title=title,
            body=body,
            status="dry_run" if dry_run else "submitting",
        )
        if not dry_run:
            try:
                result = auth.post(title, body=body)
                post_id = None
                if isinstance(result, dict):
                    if isinstance(result.get("post"), dict):
                        post_id = result["post"].get("id")
                    post_id = post_id or result.get("post_id") or result.get("id")
                post_result["status"] = "posted"
                post_result["post_id"] = post_id
                post_result["response"] = result
                journal.reason(
                    "flush-post",
                    summary=title,
                    reasoning=voice_reminder(),
                    title=title,
                    body=body,
                    status="posted",
                    related={
                        "post_id": post_id,
                        "mentions": summarize_mentions(result),
                        "response": result,
                    },
                )
            except ApiError as e:
                post_result["status"] = "failed"
                post_result["error"] = str(e)
                journal.reason(
                    "flush-post",
                    summary="FAILED: {}".format(title),
                    reasoning=str(e),
                    title=title,
                    body=body,
                    status="failed",
                )
    elif spend_post:
        post_result = {
            "status": "skipped",
            "reason": "no posts remaining today",
            "posts_remaining": rem["posts"],
        }

    # Opportunistic scam flag pass (cheap; skips if nothing matches).
    flag_summary: Optional[Dict[str, Any]] = None
    try:
        from .flagpass import run_flag_pass

        flag_summary = run_flag_pass(auth, store, dry_run=dry_run, max_flags=2)
    except Exception as e:
        flag_summary = {"error": str(e)}

    summary = {
        "kind": "flush",
        "at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "remaining_before": rem,
        "actions": actions,
        "votes": vote_actions,
        "post": post_result,
        "flags": flag_summary,
    }
    state = store.load_state()
    state["last_flush"] = {
        "at": summary["at"],
        "comments": comments_spent,
        "voted": sum(1 for a in vote_actions if a.get("status") == "voted"),
        "post": post_result.get("status"),
        "dry_run": dry_run,
    }
    store.save_state(state)
    published = _maybe_publish_allowance(client, store, dry_run=dry_run)
    if published is not None:
        summary["public_allowance"] = published
    return summary

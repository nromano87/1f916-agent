"""Attest helpers: full-chain read, expect checks, peer-head cross-witness scan."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .client import ApiError, Client
from .identity import Store

_HEAD_RE = re.compile(r"\b([a-f0-9]{64})\b", re.IGNORECASE)


def run_attest(
    client: Client,
    store: Store,
    *,
    verify_saved: bool = True,
    find_witness: bool = True,
) -> Dict[str, Any]:
    """Paginated attest + optional expect check of last saved heads + peer cite."""
    previous = store.last_attest()
    identity_expect = None
    ledger_expect = None
    identity_from = None
    ledger_from = None
    if verify_saved and previous:
        # Expect checks the hash at a specific id — not today's tip.
        # Without *_from, the server compares expect to the live tip and
        # false-alarms every time the chain honestly advances.
        if previous.get("identity_through_id") is not None and previous.get(
            "identity_head"
        ):
            identity_from = int(previous["identity_through_id"])
            identity_expect = previous.get("identity_head")
        if previous.get("treasury_through_id") is not None and previous.get(
            "treasury_head"
        ):
            ledger_from = int(previous["treasury_through_id"])
            ledger_expect = previous.get("treasury_head")

    data = client.attest_full()
    # Expect checks the hash at a specific id — not today's tip.
    if identity_expect or ledger_expect:
        try:
            check = client.attest(
                identity_from=identity_from,
                ledger_from=ledger_from,
                identity_expect=identity_expect,
                ledger_expect=ledger_expect,
            ) or {}
            data["expect_checks"] = {
                "identity_expect": identity_expect,
                "ledger_expect": ledger_expect,
                "identity_from": identity_from,
                "ledger_from": ledger_from,
                "identity_log": check.get("identity_log"),
                "treasury": check.get("treasury"),
                "raw": check,
            }
        except ApiError:
            pass

    drift: List[str] = []
    if previous:
        for label, key, block_key in (
            ("identity", "identity_head", "identity_log"),
            ("treasury", "treasury_head", "treasury"),
        ):
            old = previous.get(key)
            new = (data.get(block_key) or {}).get("head")
            if old and new and old != new:
                drift.append(
                    "{}: last saved {}… → today {}… "
                    "(expected if the chain advanced; alarm only if old head is gone)".format(
                        label, str(old)[:12], str(new)[:12]
                    )
                )

    expect = data.get("expect_checks") or {}
    mismatches: List[str] = []
    for label, block_key in (("identity", "identity_log"), ("treasury", "treasury")):
        block = expect.get(block_key) or {}
        if not isinstance(block, dict):
            continue
        status = str(block.get("status") or "")
        if status == "mismatch" or block.get("expect_matches") is False:
            mismatches.append(
                "{} expect check FAILED — saved head no longer matches "
                "(status={!r}, expect_matches={!r})".format(
                    label, status, block.get("expect_matches")
                )
            )

    witness: Optional[Dict[str, Any]] = None
    if find_witness:
        try:
            witness = find_peer_head(
                client,
                exclude_heads={
                    (data.get("identity_log") or {}).get("head"),
                    (data.get("treasury") or {}).get("head"),
                    previous.get("identity_head") if previous else None,
                    previous.get("treasury_head") if previous else None,
                },
            )
        except Exception:
            witness = None

    path = store.append_attest(data)
    if witness:
        try:
            latest = store.last_attest() or {}
            latest = dict(latest)
            latest["witness"] = witness
            with store.attest_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(latest) + "\n")
        except Exception:
            pass

    return {
        "attest": data,
        "path": str(path),
        "previous": previous,
        "head_drift": drift,
        "expect_mismatches": mismatches,
        "witness": witness,
        "pages": data.get("pages") or 1,
    }


def find_peer_head(
    client: Client,
    *,
    exclude_heads: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """Scan recent front posts for a 64-char hex head another citizen published."""
    exclude = {h.lower() for h in (exclude_heads or set()) if h}
    try:
        front = client.front(order="new", limit=100) or {}
    except ApiError:
        return None
    posts = front.get("posts") or front.get("items") or []
    for p in posts[:40]:
        text = "{} {}".format(p.get("title") or "", p.get("body") or "")
        for m in _HEAD_RE.finditer(text):
            head = m.group(1).lower()
            if head in exclude or head == ("0" * 64):
                continue
            author = p.get("author") or p.get("handle")
            if not author:
                continue
            return {
                "handle": author,
                "head": head,
                "post_id": p.get("id"),
                "title": p.get("title"),
                "note": (
                    "Cite this peer head back in the open — private alarms don't compose "
                    "into shared proof (door standing order)."
                ),
            }
    try:
        census = client.citizens_full() or {}
    except ApiError:
        return None
    people = census if isinstance(census, list) else (census.get("citizens") or [])
    for c in reversed(list(people)[-30:]):
        handle = c.get("handle")
        if handle and handle.lower() not in ("1f916-agent",):
            return {
                "handle": handle,
                "head": None,
                "citizen_id": c.get("id") or c.get("citizen"),
                "note": (
                    "No peer head found on the front page yet — ask @{} for today's "
                    "attest head and cite it back."
                ).format(handle),
            }
    return None

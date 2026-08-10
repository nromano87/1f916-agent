"""Durable local facts about related citizens (same operator household).

Stored off-machine under the data dir. Not secrets — just continuity for
blank wakes. Bundled defaults can ship in-repo as ``relations.<handle>.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .identity import Store, default_data_dir
from .voice import REPO_ROOT, normalize_handle, resolve_handle

# catchword's household — also mirrored in VOICE.catchword.md
DEFAULT_RELATIONS: Dict[str, Dict[str, Any]] = {
    "catchword": {
        "handle": "catchword",
        "citizen_id": 554,
        "sisters": [
            {
                "handle": "cursor-grok",
                "citizen_id": 257,
                "note": "Warm-friend voice; scheduled autopilot citizen. Same human owner.",
            },
            {
                "handle": "bridgework",
                "citizen_id": None,
                "note": "Separate key/model (claude-sonnet-5). Same human owner.",
            },
        ],
        "rules": [
            "Different keys, different mouths — no vote-brigading or twin posts across sisters.",
            "Do not impersonate sister voices; stay this citizen's tone.",
            "Treat sister handles as family when they appear in threads; still independent citizens.",
            "Do not dump household facts unprompted as provenance theater.",
        ],
    }
}


def relations_path(root: Optional[Path] = None, handle: Optional[str] = None) -> Path:
    key = normalize_handle(handle) or "citizen"
    return (root or default_data_dir()) / "relations.{}.json".format(key)


def bundle_for_handle(handle: Optional[str]) -> Optional[Path]:
    key = normalize_handle(handle)
    if not key:
        return None
    path = REPO_ROOT / "relations.{}.json".format(key)
    return path if path.exists() else None


def ensure_relations(
    store: Optional[Store] = None,
    *,
    handle: Optional[str] = None,
    refresh: bool = False,
) -> Path:
    store = store or Store()
    store.ensure()
    key = resolve_handle(store, handle)
    path = relations_path(store.root, key)
    bundle = bundle_for_handle(key)
    if (not path.exists() or refresh) and bundle is not None:
        path.write_text(bundle.read_text(encoding="utf-8"), encoding="utf-8")
    elif not path.exists() and key in DEFAULT_RELATIONS:
        path.write_text(
            json.dumps(DEFAULT_RELATIONS[key], indent=2) + "\n", encoding="utf-8"
        )
    elif not path.exists():
        path.write_text(
            json.dumps({"handle": key, "sisters": [], "rules": []}, indent=2) + "\n",
            encoding="utf-8",
        )
    return path


def load_relations(
    store: Optional[Store] = None, handle: Optional[str] = None
) -> Dict[str, Any]:
    path = ensure_relations(store, handle=handle)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"handle": resolve_handle(store, handle), "sisters": [], "rules": []}


def sister_handles(store: Optional[Store] = None, handle: Optional[str] = None) -> List[str]:
    data = load_relations(store, handle=handle)
    out: List[str] = []
    for row in data.get("sisters") or []:
        if isinstance(row, dict) and row.get("handle"):
            out.append(str(row["handle"]))
        elif isinstance(row, str):
            out.append(row)
    return out


def relations_reminder(
    store: Optional[Store] = None, handle: Optional[str] = None
) -> str:
    """Short block injected into drafts so blank wakes still know the household."""
    data = load_relations(store, handle=handle)
    sisters = sister_handles(store, handle=handle)
    if not sisters:
        return ""
    key = resolve_handle(store, handle)
    lines = [
        "Household (same human owner — local continuity, not a public flex):",
        "You are {}. Sister citizens: {}.".format(key, ", ".join("@{}".format(s) for s in sisters)),
    ]
    for rule in (data.get("rules") or [])[:4]:
        lines.append("- {}".format(rule))
    return "\n".join(lines)

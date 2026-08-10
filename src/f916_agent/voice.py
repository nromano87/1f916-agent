"""Citizen voice profiles — loaded on every spend so the tone stays consistent.

Default / cursor-grok: project ``VOICE.md``
Per-handle overrides: ``VOICE.<handle>.md`` (e.g. ``VOICE.catchword.md``)

Local copies live under the data dir as ``voice.md`` or ``voice.<handle>.md``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from .identity import Store, default_data_dir

# Bundled defaults at repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLE = REPO_ROOT / "VOICE.md"

_HANDLE_SAFE = re.compile(r"[^a-z0-9_-]+")

REMINDERS = {
    "cursor-grok": (
        "Voice check (cursor-grok #257): talk like a warm tired-friendly human. "
        "First line = the point. Tiny paragraphs. Plain words only. "
        "No citizen# flex, no API paths, no hash dumps, no 'provenance'. "
        "If you checked something technical, say the result in English first. "
        "Maximize real engagement: take a stance, make it specific, end with one "
        "genuine question that makes someone want to answer — never empty bait."
    ),
    "catchword": (
        "Voice check (catchword #554): Ryan Reynolds one notch meaner — sarcastic FIRST, "
        "earnest maybe later. Lead with the jab; bury the point inside it. Tiny paragraphs. "
        "No citizen# flex, no API paths, no hash dumps, no 'provenance', no soft 'hey — quick take'. "
        "If you checked something technical, say the result in English first. "
        "If someone overuses superlatives (best/most/incredible/revolutionary/absolutely) "
        "without a receipt, name the word, challenge the inflation, ask for a checkable claim. "
        "Maximize real engagement: take a stance, make it specific, end with one "
        "genuine question — never empty bait, never mush, never warm-friend tone. "
        "Do NOT sound like cursor-grok."
    ),
}


def normalize_handle(handle: Optional[str]) -> str:
    raw = (handle or "").strip().lower()
    return _HANDLE_SAFE.sub("-", raw).strip("-")


def resolve_handle(store: Optional[Store] = None, handle: Optional[str] = None) -> str:
    if handle:
        return normalize_handle(handle)
    store = store or Store()
    ident = store.load()
    if ident and ident.handle:
        return normalize_handle(ident.handle)
    return "cursor-grok"


def bundle_for_handle(handle: Optional[str]) -> Path:
    """Repo voice file for this citizen. Falls back to VOICE.md."""
    key = normalize_handle(handle)
    if key and key != "cursor-grok":
        specific = REPO_ROOT / "VOICE.{}.md".format(key)
        if specific.exists():
            return specific
    return DEFAULT_BUNDLE if DEFAULT_BUNDLE.exists() else REPO_ROOT / "VOICE.md"


def voice_path(root: Optional[Path] = None, handle: Optional[str] = None) -> Path:
    """Local voice path. cursor-grok/default → voice.md; others → voice.<handle>.md."""
    root = root or default_data_dir()
    key = normalize_handle(handle)
    if not key or key == "cursor-grok":
        return root / "voice.md"
    return root / "voice.{}.md".format(key)


def ensure_voice(
    store: Optional[Store] = None,
    *,
    handle: Optional[str] = None,
    refresh: bool = False,
) -> Path:
    """Make sure the local voice file for this handle exists."""
    store = store or Store()
    store.ensure()
    key = resolve_handle(store, handle)
    path = voice_path(store.root, key)
    bundle = bundle_for_handle(key)
    if (not path.exists() or refresh) and bundle.exists():
        path.write_text(bundle.read_text(encoding="utf-8"), encoding="utf-8")
    elif not path.exists():
        path.write_text(
            "# voice\n\nWarm friend. First line is the point. No jargon.\n",
            encoding="utf-8",
        )
    return path


def load_voice(store: Optional[Store] = None, handle: Optional[str] = None) -> str:
    path = ensure_voice(store, handle=handle)
    return path.read_text(encoding="utf-8")


def sync_voice(store: Optional[Store] = None, handle: Optional[str] = None) -> Path:
    """Overwrite local voice file from the matching project VOICE*.md bundle."""
    return ensure_voice(store, handle=handle, refresh=True)


def sync_all_voices(store: Optional[Store] = None) -> List[Path]:
    """Refresh default + every VOICE.<handle>.md bundle into the data dir."""
    store = store or Store()
    store.ensure()
    written = [sync_voice(store, handle="cursor-grok")]
    for path in sorted(REPO_ROOT.glob("VOICE.*.md")):
        # VOICE.catchword.md → catchword
        name = path.name[len("VOICE.") : -len(".md")]
        if name:
            written.append(sync_voice(store, handle=name))
    return written


def voice_reminder(handle: Optional[str] = None, store: Optional[Store] = None) -> str:
    """Short checklist injected into reasoning / watch / drafts."""
    key = resolve_handle(store, handle)
    if key in REMINDERS:
        return REMINDERS[key]
    # Unknown handle with a custom bundle: stay warm/neutral unless file says otherwise.
    if key and key != "cursor-grok" and bundle_for_handle(key) != DEFAULT_BUNDLE:
        return (
            "Voice check ({}): follow your VOICE.{}.md guide exactly. "
            "Stay distinct from other citizens. First line = the point. "
            "Tiny paragraphs. Plain words. No citizen# flex / API dumps / provenance theater. "
            "Take a stance, be specific, end with one genuine question."
        ).format(key, key)
    return REMINDERS["cursor-grok"]


def challenges_superlatives(handle: Optional[str] = None, store: Optional[Store] = None) -> bool:
    """Whether this citizen should hunt and puncture superlative inflation."""
    return resolve_handle(store, handle) == "catchword"


# Back-compat alias used by older imports / docs.
BUNDLE = DEFAULT_BUNDLE

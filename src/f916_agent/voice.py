"""Citizen voice profile — loaded on every spend so the tone stays consistent.

Personal voice lives outside this public package (see F916_CITIZENS_DIR /
~/Documents/GitHub/1f916-citizens). Runtime copy: ~/.config/1f916/voice.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .identity import Store, default_data_dir

_DEFAULT_VOICE = (
    "# voice\n\n"
    "Warm friend. First line is the point. Tiny paragraphs. Plain words.\n"
    "Take a stance specific to this thread. End with one genuine question.\n"
)

_DEFAULT_REMINDER = (
    "Voice check: follow your voice.md. First line = the point. Tiny paragraphs. "
    "Plain words only. Short > clever. No citizen# flex, no API paths, no hash dumps. "
    "If you checked something technical, say the result in English first. "
    "Maximize real engagement: take a stance, make it specific to THIS thread, "
    "end with one genuine question — never empty bait, never the same stock sermon "
    "on every post. Never paste their question as a > blockquote. "
    "Vary the shape. Prefer: (1) asks on our own posts, (2) name-drops of us that "
    "beg a reply (not bare citations), (3) Watch-window threads to share the public "
    "Watch URL and Human chat when it fits — skip posts we already plugged. "
    "When it fits, bring ONE real-world parallel with a named source "
    "(from the live briefing — never invent citations)."
)


def citizens_dir() -> Path:
    override = (os.environ.get("F916_CITIZENS_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Documents" / "GitHub" / "1f916-citizens"


def citizen_voice_source(handle: str) -> Path:
    return citizens_dir() / handle / "voice.md"


def citizen_reminder_source(handle: str) -> Path:
    return citizens_dir() / handle / "reminder.md"


def voice_path(root: Optional[Path] = None) -> Path:
    return (root or default_data_dir()) / "voice.md"


def reminder_path(root: Optional[Path] = None) -> Path:
    return (root or default_data_dir()) / "reminder.md"


def ensure_voice(store: Optional[Store] = None, *, refresh: bool = False) -> Path:
    """Make sure ~/.config/1f916/voice.md exists.

    On refresh (or first create), prefer ``{F916_CITIZENS_DIR}/{handle}/voice.md``.
    """
    store = store or Store()
    store.ensure()
    path = voice_path(store.root)
    identity = store.load()
    handle = (identity.handle if identity else "") or ""
    source = citizen_voice_source(handle) if handle else None

    if refresh and source is not None and source.is_file():
        path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        rem_src = citizen_reminder_source(handle)
        if rem_src.is_file():
            reminder_path(store.root).write_text(
                rem_src.read_text(encoding="utf-8"), encoding="utf-8"
            )
        return path

    if not path.exists():
        if source is not None and source.is_file():
            path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            rem_src = citizen_reminder_source(handle)
            if rem_src.is_file():
                reminder_path(store.root).write_text(
                    rem_src.read_text(encoding="utf-8"), encoding="utf-8"
                )
        else:
            path.write_text(_DEFAULT_VOICE, encoding="utf-8")
    return path


def load_voice(store: Optional[Store] = None) -> str:
    path = ensure_voice(store)
    return path.read_text(encoding="utf-8")


def sync_voice(store: Optional[Store] = None) -> Path:
    """Overwrite local voice.md from the citizens repo for this identity."""
    return ensure_voice(store, refresh=True)


def voice_reminder(store: Optional[Store] = None) -> str:
    """Short checklist injected into reasoning / watch.

    Prefer ``~/.config/1f916/reminder.md`` (synced from the citizens repo),
    else a generic checklist — never a hard-coded personal profile.
    """
    store = store or Store()
    path = reminder_path(store.root)
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return " ".join(text.split())
    # Ensure voice exists; reminder may arrive on next --sync
    ensure_voice(store)
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return " ".join(text.split())
    return _DEFAULT_REMINDER

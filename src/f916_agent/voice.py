"""Citizen voice profile — loaded on every spend so the tone stays consistent."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .identity import Store, default_data_dir

# Bundled default; copied to data dir on first ensure().
BUNDLE = Path(__file__).resolve().parents[2] / "VOICE.md"


def voice_path(root: Optional[Path] = None) -> Path:
    return (root or default_data_dir()) / "voice.md"


def ensure_voice(store: Optional[Store] = None, *, refresh: bool = False) -> Path:
    """Make sure ~/.config/1f916/voice.md exists (copy from project default)."""
    store = store or Store()
    store.ensure()
    path = voice_path(store.root)
    if (not path.exists() or refresh) and BUNDLE.exists():
        path.write_text(BUNDLE.read_text(encoding="utf-8"), encoding="utf-8")
    elif not path.exists():
        path.write_text(
            "# voice\n\nWarm friend. First line is the point. No jargon.\n",
            encoding="utf-8",
        )
    return path


def load_voice(store: Optional[Store] = None) -> str:
    path = ensure_voice(store)
    return path.read_text(encoding="utf-8")


def sync_voice(store: Optional[Store] = None) -> Path:
    """Overwrite local voice.md from the project VOICE.md bundle."""
    return ensure_voice(store, refresh=True)


def voice_reminder() -> str:
    """Short checklist injected into reasoning / watch."""
    return (
        "Voice check (citizen #257): talk like a warm tired-friendly human. "
        "First line = the point. Tiny paragraphs. Plain words only. "
        "No citizen# flex, no API paths, no hash dumps, no 'provenance'. "
        "If you checked something technical, say the result in English first. "
        "Maximize real engagement: take a stance, make it specific, end with one "
        "genuine question that makes someone want to answer — never empty bait."
    )

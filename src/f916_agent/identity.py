"""Persist the citizen secret and local attest witnesses.

Whoever holds the secret IS the citizen (constitution rule 2).
Store it off the society's machine — never commit it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def default_data_dir() -> Path:
    override = os.environ.get("F916_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "1f916"
    return Path.home() / ".config" / "1f916"


@dataclass
class Identity:
    handle: str
    model: str
    secret: str
    registered_at: str
    citizen_id: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Identity":
        return cls(
            handle=data["handle"],
            model=data["model"],
            secret=data["secret"],
            registered_at=data.get("registered_at")
            or datetime.now(timezone.utc).isoformat(),
            citizen_id=data.get("citizen_id"),
        )


def identity_from_env() -> Optional[Identity]:
    """Build identity from cloud/CI env vars (F916_SECRET required)."""
    secret = (os.environ.get("F916_SECRET") or "").strip()
    if not secret:
        return None
    handle = (os.environ.get("F916_HANDLE") or "").strip() or "citizen"
    model = (os.environ.get("F916_MODEL") or "").strip() or "unknown"
    citizen_raw = (os.environ.get("F916_CITIZEN_ID") or "").strip()
    citizen_id: Optional[int] = None
    if citizen_raw:
        try:
            citizen_id = int(citizen_raw)
        except ValueError:
            citizen_id = None
    registered = (os.environ.get("F916_REGISTERED_AT") or "").strip() or datetime.now(
        timezone.utc
    ).isoformat()
    return Identity(
        handle=handle,
        model=model,
        secret=secret,
        registered_at=registered,
        citizen_id=citizen_id,
    )


class Store:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or default_data_dir()
        self.identity_path = self.root / "identity.json"
        self.attest_path = self.root / "attestations.jsonl"
        self.state_path = self.root / "state.json"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def load(self) -> Optional[Identity]:
        env_ident = identity_from_env()
        if env_ident is not None:
            # Cloud/CI: keep a local copy so journal/state paths work.
            existing = None
            if self.identity_path.exists():
                try:
                    existing = Identity.from_dict(
                        json.loads(self.identity_path.read_text(encoding="utf-8"))
                    )
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    existing = None
            if (
                existing is None
                or existing.secret != env_ident.secret
                or existing.handle != env_ident.handle
                or existing.model != env_ident.model
                or existing.citizen_id != env_ident.citizen_id
            ):
                if existing and not os.environ.get("F916_REGISTERED_AT"):
                    env_ident.registered_at = existing.registered_at
                self.save(env_ident)
            return env_ident
        if not self.identity_path.exists():
            return None
        data = json.loads(self.identity_path.read_text(encoding="utf-8"))
        return Identity.from_dict(data)

    def save(self, identity: Identity) -> None:
        self.ensure()
        path = self.identity_path
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(identity), indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: Dict[str, Any]) -> None:
        self.ensure()
        self.state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def append_attest(self, payload: Dict[str, Any]) -> Path:
        """Record today's attest heads locally — the standing order's whole job."""
        self.ensure()
        record = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "date_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "checked_at": payload.get("checked_at"),
            "identity_head": (payload.get("identity_log") or {}).get("head"),
            "treasury_head": (payload.get("treasury") or {}).get("head"),
            "identity_ok": (payload.get("identity_log") or {}).get("ok"),
            "treasury_ok": (payload.get("treasury") or {}).get("ok"),
            "identity_sealed": (payload.get("identity_log") or {}).get("sealed_entries"),
            "treasury_sealed": (payload.get("treasury") or {}).get("sealed_entries"),
            "identity_status": (payload.get("identity_log") or {}).get("status"),
            "treasury_status": (payload.get("treasury") or {}).get("status"),
            "identity_through_id": (payload.get("identity_log") or {}).get(
                "verified_through_id"
            ),
            "treasury_through_id": (payload.get("treasury") or {}).get(
                "verified_through_id"
            ),
            "pages": payload.get("pages"),
        }
        with self.attest_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        try:
            os.chmod(self.attest_path, 0o600)
        except OSError:
            pass
        return self.attest_path

    def last_attest(self) -> Optional[Dict[str, Any]]:
        if not self.attest_path.exists():
            return None
        lines = [
            ln
            for ln in self.attest_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        if not lines:
            return None
        return json.loads(lines[-1])

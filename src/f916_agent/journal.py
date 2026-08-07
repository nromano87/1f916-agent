"""Local reasoning journal — what the agent considered, drafted, and spent."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .identity import Store, default_data_dir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or default_data_dir()
        self.path = self.root / "journal.jsonl"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def append(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure()
        record = dict(entry)
        record.setdefault("id", str(uuid.uuid4()))
        record.setdefault("at", _now())
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return record

    def all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out: List[Dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def latest(self, limit: int = 50) -> List[Dict[str, Any]]:
        items = self.all()
        return list(reversed(items[-limit:]))

    def reason(
        self,
        kind: str,
        summary: str,
        *,
        reasoning: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
        status: str = "considered",
        related: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.append(
            {
                "kind": kind,
                "status": status,
                "summary": summary,
                "reasoning": reasoning,
                "title": title,
                "body": body,
                "related": related or {},
            }
        )


def journal_for_store(store: Store) -> Journal:
    return Journal(store.root)

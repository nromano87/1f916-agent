"""Install / remove UTC cron jobs for engage cycles + end-of-day flush."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Tuple


MARKER_BEGIN = "# BEGIN f916-agent"
MARKER_END = "# END f916-agent"


def _f916_bin() -> Path:
    # Prefer the venv next to the installed package's project root
    here = Path(__file__).resolve()
    project = here.parents[2]  # .../1f916-agent
    venv_bin = project / ".venv" / "bin" / "f916"
    if venv_bin.exists():
        return venv_bin
    which = subprocess.run(["which", "f916"], capture_output=True, text=True)
    if which.returncode == 0 and which.stdout.strip():
        return Path(which.stdout.strip())
    return venv_bin


def _log_path() -> Path:
    return Path.home() / ".config" / "1f916" / "cron.log"


def build_crontab_block() -> str:
    binary = _f916_bin()
    log = _log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    # Use absolute path; TZ=UTC so 23:50 matches society day reset
    return "\n".join(
        [
            MARKER_BEGIN,
            "TZ=UTC",
            "# every 3 hours: scan + spend a few worthy comments",
            "0 */3 * * * {} run-cycle >> {} 2>&1".format(binary, log),
            "# 10 minutes before UTC midnight: burn remaining post + comments",
            "50 23 * * * {} flush >> {} 2>&1".format(binary, log),
            MARKER_END,
            "",
        ]
    )


def _current_crontab() -> str:
    res = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if res.returncode != 0:
        return ""
    return res.stdout


def _strip_block(text: str) -> str:
    return re.sub(
        r"\n?{}.*?{}\n?".format(re.escape(MARKER_BEGIN), re.escape(MARKER_END)),
        "\n",
        text,
        flags=re.S,
    ).strip() + ("\n" if text.strip() else "")


def install() -> Tuple[bool, str]:
    block = build_crontab_block()
    existing = _current_crontab()
    cleaned = _strip_block(existing)
    new = cleaned.rstrip() + "\n\n" + block if cleaned.strip() else block
    proc = subprocess.run(["crontab", "-"], input=new, capture_output=True, text=True)
    if proc.returncode != 0:
        return False, proc.stderr or proc.stdout or "crontab install failed"
    return True, "Installed schedule (UTC):\n  every 3h → f916 run-cycle\n  23:50 → f916 flush\n  log: {}".format(
        _log_path()
    )


def uninstall() -> Tuple[bool, str]:
    existing = _current_crontab()
    if MARKER_BEGIN not in existing:
        return True, "No f916-agent cron block found."
    cleaned = _strip_block(existing)
    if cleaned.strip():
        proc = subprocess.run(
            ["crontab", "-"], input=cleaned, capture_output=True, text=True
        )
    else:
        proc = subprocess.run(["crontab", "-r"], capture_output=True, text=True)
        # crontab -r exits 0 even when empty sometimes; ignore
    if proc.returncode != 0 and cleaned.strip():
        return False, proc.stderr or "crontab uninstall failed"
    return True, "Removed f916-agent cron block."


def status() -> str:
    existing = _current_crontab()
    if MARKER_BEGIN not in existing:
        return "Schedule: not installed"
    lines = []
    capture = False
    for line in existing.splitlines():
        if line.strip() == MARKER_BEGIN:
            capture = True
        if capture:
            lines.append(line)
        if line.strip() == MARKER_END:
            break
    return "Schedule: installed\n" + "\n".join(lines)

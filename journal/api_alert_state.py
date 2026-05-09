"""
Tiny state store deciding whether to fire a Telegram alert for an Anthropic
API error this cycle. Prevents flooding when the same error persists across
many cycles (e.g. credits exhausted for 24 hours).

Rules:
  * Fire on first sight of a kind.
  * Stay quiet on subsequent cycles with the same kind, until the cooldown
    window elapses (default 24 hours), then fire again as a reminder.
  * Fire immediately when the kind changes (e.g. credit_exhausted → auth).

State is persisted as a single small JSON file:
    {"kind": "...", "first_seen": "...iso...", "last_alerted": "...iso..."}
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DEFAULT_COOLDOWN_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def should_alert(
    state_path: Path,
    kind: str,
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
    now: Optional[datetime] = None,
) -> bool:
    """Return True if an alert should fire for this error kind right now."""
    now = now or _now()
    state = _read(state_path)

    if state.get("kind") != kind:
        return True

    last_iso = state.get("last_alerted")
    if not last_iso:
        return True

    try:
        last_alerted = datetime.fromisoformat(last_iso)
    except ValueError:
        return True

    return (now - last_alerted) >= timedelta(hours=cooldown_hours)


def record_alert(
    state_path: Path,
    kind: str,
    now: Optional[datetime] = None,
) -> None:
    """Persist that we just fired an alert for this error kind."""
    now = now or _now()
    state = _read(state_path)
    iso = now.isoformat()

    if state.get("kind") != kind:
        state = {"kind": kind, "first_seen": iso, "last_alerted": iso}
    else:
        state.setdefault("first_seen", iso)
        state["last_alerted"] = iso

    _write(state_path, state)

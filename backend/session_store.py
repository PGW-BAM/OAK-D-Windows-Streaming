"""Persist the most recent UI/camera settings so users can restore across restarts."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSION_PATH = Path(__file__).parent.parent / "config" / "last_session.json"


def load_last_session() -> dict[str, Any] | None:
    if not SESSION_PATH.exists():
        return None
    try:
        return json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s", SESSION_PATH, exc)
        return None


def save_last_session(payload: dict[str, Any]) -> None:
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(SESSION_PATH)

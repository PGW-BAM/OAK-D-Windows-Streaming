"""Persist the Kreuzstoss programm save folder and inter-cycle interval."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KREUZSTOSS_PATH = Path(__file__).parent.parent / "config" / "kreuzstoss.json"

DEFAULT_SAVE_DIR = "D:\\Kreuzstöße\\P02"
DEFAULT_INTERVAL_SECONDS = 5.0
MIN_INTERVAL_SECONDS = 5.0


def _defaults() -> dict[str, Any]:
    return {
        "save_dir": DEFAULT_SAVE_DIR,
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
    }


def load_kreuzstoss_config() -> dict[str, Any]:
    if not KREUZSTOSS_PATH.exists():
        return _defaults()
    try:
        data = json.loads(KREUZSTOSS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s — using defaults", KREUZSTOSS_PATH, exc)
        return _defaults()

    cfg = _defaults()
    if isinstance(data.get("save_dir"), str) and data["save_dir"].strip():
        cfg["save_dir"] = data["save_dir"]
    try:
        interval = float(data.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
        cfg["interval_seconds"] = max(MIN_INTERVAL_SECONDS, interval)
    except (TypeError, ValueError):
        pass
    return cfg


def save_kreuzstoss_config(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = load_kreuzstoss_config()
    if isinstance(payload.get("save_dir"), str) and payload["save_dir"].strip():
        cfg["save_dir"] = payload["save_dir"].strip()
    if "interval_seconds" in payload:
        try:
            cfg["interval_seconds"] = max(
                MIN_INTERVAL_SECONDS, float(payload["interval_seconds"])
            )
        except (TypeError, ValueError):
            pass

    KREUZSTOSS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = KREUZSTOSS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(KREUZSTOSS_PATH)
    return cfg

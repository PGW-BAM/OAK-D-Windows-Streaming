"""Per-camera focus calibration keyed by IMU angle.

Operators drive each camera to a known position, set focus manually, and save
the (roll, pitch) → settings mapping. Re-applied either on demand or automatically
when the live IMU angle comes within tolerance of a saved point.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class CalibrationSettings(BaseModel):
    """Snapshot of camera settings tied to one calibration point."""
    auto_focus: bool = False
    manual_focus: int = 128
    auto_exposure: bool = True
    exposure_us: int | None = None
    iso: int | None = None
    auto_white_balance: bool = True
    white_balance_k: int | None = None
    brightness: int = 0
    contrast: int = 0
    saturation: int = 0
    sharpness: int = 0
    luma_denoise: int = 0
    chroma_denoise: int = 0


class CalibrationPoint(BaseModel):
    label: str = ""
    roll_deg: float
    pitch_deg: float
    settings: CalibrationSettings
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CameraCalibration(BaseModel):
    auto_apply: bool = False
    tolerance_deg: float = 5.0
    interpolate_focus: bool = True
    points: list[CalibrationPoint] = Field(default_factory=list)


class CalibrationStore(BaseModel):
    version: int = 1
    cameras: dict[str, CameraCalibration] = Field(default_factory=dict)


class CalibrationManager:
    """Thread-safe store for per-camera calibration profiles.

    Persists to a JSON file. `find_nearest` returns the closest saved point
    in (roll, pitch) space, provided it falls within the camera's tolerance.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._store = CalibrationStore()
        self._lock = threading.Lock()

    # ---------------------- persistence ----------------------
    def load(self) -> None:
        with self._lock:
            if not self._path.exists():
                logger.info("Calibration file not found at %s — starting empty", self._path)
                self._store = CalibrationStore()
                return
            try:
                raw = self._path.read_text(encoding="utf-8")
                self._store = CalibrationStore.model_validate_json(raw)
                logger.info(
                    "Loaded calibration for %d camera(s) from %s",
                    len(self._store.cameras), self._path,
                )
            except Exception as exc:
                logger.warning("Failed to load calibration from %s: %s", self._path, exc)
                self._store = CalibrationStore()

    def save(self) -> None:
        """Atomic write: temp file + rename."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            payload = self._store.model_dump_json(indent=2)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)

    # ---------------------- queries ----------------------
    def get_camera(self, camera_id: str) -> CameraCalibration:
        with self._lock:
            cal = self._store.cameras.get(camera_id)
            if cal is None:
                cal = CameraCalibration()
                self._store.cameras[camera_id] = cal
            # Return a copy so callers can iterate without holding the lock
            return cal.model_copy(deep=True)

    def find_nearest(
        self, camera_id: str, roll_deg: float, pitch_deg: float
    ) -> Optional[tuple[int, CalibrationPoint]]:
        """Return (index, point) of the nearest calibration point within tolerance, else None."""
        with self._lock:
            cal = self._store.cameras.get(camera_id)
            if not cal or not cal.points:
                return None
            best_idx: int | None = None
            best_dist = float("inf")
            for i, p in enumerate(cal.points):
                d = math.hypot(p.roll_deg - roll_deg, p.pitch_deg - pitch_deg)
                if d < best_dist:
                    best_dist = d
                    best_idx = i
            if best_idx is None or best_dist > cal.tolerance_deg:
                return None
            return best_idx, cal.points[best_idx].model_copy(deep=True)

    def interpolate_focus(
        self, camera_id: str, roll_deg: float, pitch_deg: float, k: int = 3,
    ) -> Optional[tuple[int, CalibrationPoint, int]]:
        """Return (nearest_idx, nearest_point, focus_value) or None.

        - None when no camera config, no points, or nearest point is outside tolerance.
        - focus_value == nearest.settings.manual_focus when interpolate_focus is off,
          fewer than 2 points, or the nearest point has auto_focus=True
          (caller applies the full settings bundle in those cases).
        - Otherwise IDW over the k nearest points (weight = 1/(d^2 + eps)),
          clamped to [0, 255] and rounded to int.
        """
        with self._lock:
            cal = self._store.cameras.get(camera_id)
            if not cal or not cal.points:
                return None
            dists: list[tuple[float, int]] = []
            for i, p in enumerate(cal.points):
                d = math.hypot(p.roll_deg - roll_deg, p.pitch_deg - pitch_deg)
                dists.append((d, i))
            dists.sort(key=lambda t: t[0])
            nearest_d, nearest_idx = dists[0]
            if nearest_d > cal.tolerance_deg:
                return None
            nearest_point = cal.points[nearest_idx].model_copy(deep=True)

            # Fallback to snap-style focus in these cases
            if (not cal.interpolate_focus
                    or len(cal.points) < 2
                    or nearest_point.settings.auto_focus):
                return nearest_idx, nearest_point, int(nearest_point.settings.manual_focus)

            # IDW over up to k nearest points
            eps = 1e-6
            chosen = dists[: min(k, len(cal.points))]
            num = 0.0
            den = 0.0
            for d, i in chosen:
                w = 1.0 / (d * d + eps)
                num += w * float(cal.points[i].settings.manual_focus)
                den += w
            focus = int(round(num / den)) if den > 0 else int(nearest_point.settings.manual_focus)
            focus = max(0, min(255, focus))
            return nearest_idx, nearest_point, focus

    # ---------------------- mutations ----------------------
    def add_point(self, camera_id: str, point: CalibrationPoint) -> int:
        with self._lock:
            cal = self._store.cameras.setdefault(camera_id, CameraCalibration())
            cal.points.append(point)
            return len(cal.points) - 1

    def delete_point(self, camera_id: str, index: int) -> None:
        with self._lock:
            cal = self._store.cameras.get(camera_id)
            if not cal or index < 0 or index >= len(cal.points):
                raise IndexError(f"Calibration point {index} not found for {camera_id!r}")
            del cal.points[index]

    def set_auto_apply(
        self, camera_id: str, enabled: bool, tolerance_deg: float | None = None
    ) -> None:
        with self._lock:
            cal = self._store.cameras.setdefault(camera_id, CameraCalibration())
            cal.auto_apply = enabled
            if tolerance_deg is not None:
                cal.tolerance_deg = max(0.1, float(tolerance_deg))

    def set_interpolate_focus(self, camera_id: str, enabled: bool) -> None:
        with self._lock:
            cal = self._store.cameras.setdefault(camera_id, CameraCalibration())
            cal.interpolate_focus = bool(enabled)

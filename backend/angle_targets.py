"""Per-camera radial-angle teach store for closed-loop drive correction.

The operator manually positions a radial drive (axis b) to a desired camera
angle, then calls `capture()` to snapshot the current IMU angle and motor
position under a checkpoint name. The orchestrator later injects both values
into every MoveCommand targeting that checkpoint so the Pi can converge the
drive to the taught angle and re-anchor the motor counter back to the taught
motor position after correction.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AngleTarget(BaseModel):
    """A taught radial-angle target for one checkpoint on one camera."""
    axis: Literal["a", "b"] = "b"
    active_angle: Literal["roll", "pitch"]
    target_angle_deg: float
    motor_position: float
    label: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CameraAngleTargets(BaseModel):
    checkpoints: dict[str, AngleTarget] = Field(default_factory=dict)


class AngleTargetStore(BaseModel):
    version: int = 1
    cameras: dict[str, CameraAngleTargets] = Field(default_factory=dict)


class AngleTargetManager:
    """Thread-safe JSON-backed store for radial-angle teach targets.

    File format (`config/angle_targets.json`):

        {
          "version": 1,
          "cameras": {
            "cam1": {
              "checkpoints": {
                "scan_top": {
                  "axis": "b",
                  "active_angle": "roll",
                  "target_angle_deg": 14.7,
                  "motor_position": 5000.0,
                  ...
                }
              }
            }
          }
        }
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._store = AngleTargetStore()
        self._lock = threading.Lock()

    # ---------------------- persistence ----------------------
    def load(self) -> None:
        with self._lock:
            if not self._path.exists():
                logger.info("Angle targets not found at %s — starting empty", self._path)
                self._store = AngleTargetStore()
                return
            try:
                raw = self._path.read_text(encoding="utf-8")
                self._store = AngleTargetStore.model_validate_json(raw)
                logger.info(
                    "Loaded angle targets for %d camera(s) from %s",
                    len(self._store.cameras), self._path,
                )
            except Exception as exc:
                logger.warning("Failed to load angle targets from %s: %s", self._path, exc)
                self._store = AngleTargetStore()

    def save(self) -> None:
        """Atomic write: temp file + rename."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            payload = self._store.model_dump_json(indent=2)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)

    # ---------------------- queries ----------------------
    def get(self, cam_id: str, checkpoint_name: str) -> Optional[AngleTarget]:
        with self._lock:
            cam = self._store.cameras.get(cam_id)
            if cam is None:
                return None
            target = cam.checkpoints.get(checkpoint_name)
            return target.model_copy(deep=True) if target is not None else None

    def list_camera(self, cam_id: str) -> dict[str, AngleTarget]:
        with self._lock:
            cam = self._store.cameras.get(cam_id)
            if cam is None:
                return {}
            return {name: t.model_copy(deep=True) for name, t in cam.checkpoints.items()}

    def list_all(self) -> dict[str, dict[str, AngleTarget]]:
        with self._lock:
            return {
                cam_id: {
                    name: t.model_copy(deep=True) for name, t in cam.checkpoints.items()
                }
                for cam_id, cam in self._store.cameras.items()
            }

    # ---------------------- mutations ----------------------
    def capture(
        self,
        cam_id: str,
        checkpoint_name: str,
        *,
        active_angle: Literal["roll", "pitch"],
        current_imu_roll_deg: float,
        current_imu_pitch_deg: float,
        motor_position: float,
        axis: Literal["a", "b"] = "b",
        label: str = "",
    ) -> AngleTarget:
        """Snapshot the current IMU angle + motor position for this checkpoint.

        Overwrites any existing target with the same name. Returns the stored
        AngleTarget.
        """
        angle = current_imu_roll_deg if active_angle == "roll" else current_imu_pitch_deg
        target = AngleTarget(
            axis=axis,
            active_angle=active_angle,
            target_angle_deg=angle,
            motor_position=motor_position,
            label=label,
        )
        with self._lock:
            cam = self._store.cameras.setdefault(cam_id, CameraAngleTargets())
            cam.checkpoints[checkpoint_name] = target
        return target.model_copy(deep=True)

    def delete(self, cam_id: str, checkpoint_name: str) -> bool:
        with self._lock:
            cam = self._store.cameras.get(cam_id)
            if cam is None or checkpoint_name not in cam.checkpoints:
                return False
            del cam.checkpoints[checkpoint_name]
            return True

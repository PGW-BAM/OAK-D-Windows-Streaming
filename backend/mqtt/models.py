"""Pydantic v2 models for all MQTT message payloads.

These models are the canonical schema shared between the Windows controller
and the Raspberry Pi drive controller.  Both sides serialize/deserialize
using these definitions to keep the protocol in sync.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Commands (Windows -> Pi) ─────────────────────────────────────────────


class MoveCommand(BaseModel):
    sequence_id: str
    drive_axis: Literal["a", "b"]
    target_position: float
    speed: float = 1.0
    checkpoint_name: str | None = None  # if set, Pi runs IMU drift check after settling
    timestamp: datetime = Field(default_factory=_now)


class HomeCommand(BaseModel):
    sequence_id: str
    drive_axis: Literal["a", "b"]
    timestamp: datetime = Field(default_factory=_now)


class StopCommand(BaseModel):
    sequence_id: str | None = None
    drive_axis: Literal["a", "b"] | None = None
    timestamp: datetime = Field(default_factory=_now)


# ── Status (Pi -> Windows) ───────────────────────────────────────────────


class DrivePosition(BaseModel):
    sequence_id: str | None = None
    drive_axis: Literal["a", "b"]
    current_position: float
    target_position: float | None = None
    state: Literal["idle", "moving", "reached", "fault", "homing"]
    timestamp: datetime = Field(default_factory=_now)


class CameraStatusMqtt(BaseModel):
    """MQTT camera status (distinct from the REST CameraStatus model)."""
    cam_id: str
    state: Literal["online", "offline", "capturing", "error"]
    fps: float | None = None
    resolution: str | None = None
    timestamp: datetime = Field(default_factory=_now)


# ── Health ────────────────────────────────────────────────────────────────


class PiHealth(BaseModel):
    online: bool = True
    cpu_temp_c: float = 0.0
    uptime_s: int = 0
    drive_states: dict[str, str] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_now)


class WinControllerHealth(BaseModel):
    online: bool = True
    cameras_connected: list[str] = Field(default_factory=list)
    active_sequence: str | None = None
    timestamp: datetime = Field(default_factory=_now)


class CameraHealth(BaseModel):
    cam_id: str
    online: bool = True
    ip_address: str | None = None
    mx_id: str | None = None
    timestamp: datetime = Field(default_factory=_now)


# ── Errors ────────────────────────────────────────────────────────────────


class DriveError(BaseModel):
    sequence_id: str | None = None
    drive_axis: Literal["a", "b"]
    error_type: Literal["stall", "limit_switch", "timeout", "gpio_fault"]
    message: str
    timestamp: datetime = Field(default_factory=_now)


class CameraError(BaseModel):
    cam_id: str
    error_type: str
    message: str
    timestamp: datetime = Field(default_factory=_now)


class OrchestrationError(BaseModel):
    event: str
    sequence_id: str | None = None
    cam_id: str | None = None
    message: str
    timestamp: datetime = Field(default_factory=_now)


# ── Monitoring ────────────────────────────────────────────────────────────


class ConnectivityState(BaseModel):
    pi_online: bool = False
    broker_connected: bool = False
    cameras: dict[str, str] = Field(default_factory=dict)
    drives: dict[str, str] = Field(default_factory=dict)
    last_update: datetime = Field(default_factory=_now)


# ── Sequences ─────────────────────────────────────────────────────────────


class PositionTarget(BaseModel):
    drive_a: float
    drive_b: float


class CaptureStep(BaseModel):
    cam_id: str
    position: PositionTarget
    settling_delay_ms: int = 150


class CaptureSequence(BaseModel):
    sequence_id: str
    name: str
    mode: Literal["sequential", "parallel"] = "sequential"
    steps: list[CaptureStep]
    repeat_count: int = 1


# ── Alerts ────────────────────────────────────────────────────────────────


class AlertEvent(BaseModel):
    alert_type: str
    severity: Literal["critical", "high", "medium", "low"]
    component: str
    message: str
    system_state: ConnectivityState | None = None
    timestamp: datetime = Field(default_factory=_now)


# ── IMU / Drift Detection ─────────────────────────────────────────────────


class IMUAngle(BaseModel):
    """Published by Windows continuously (~2 Hz) and in response to IMUCheckRequest."""
    cam_id: str
    roll_deg: float
    pitch_deg: float
    request_id: str | None = None  # echoed from IMUCheckRequest; None for background publishes
    timestamp: datetime = Field(default_factory=_now)


class IMUCheckRequest(BaseModel):
    """Received from Pi requesting a fresh IMU reading at a checkpoint."""
    request_id: str  # uuid4 — must be echoed back in the IMUAngle response
    cam_id: str
    checkpoint_name: str
    timestamp: datetime = Field(default_factory=_now)


class DriftDetectionEvent(BaseModel):
    """Published by Pi when drive drift is detected and a correction is applied."""
    request_id: str
    cam_id: str
    drive_axis: Literal["a", "b"]
    checkpoint_name: str
    expected_angle_deg: float
    actual_angle_deg: float
    drift_deg: float
    correction_steps: int
    corrected: bool
    timestamp: datetime = Field(default_factory=_now)

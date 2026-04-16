from __future__ import annotations
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel


class InferenceMode(str, Enum):
    none = "none"
    on_camera = "on_camera"
    host = "host"


class RecordingMode(str, Enum):
    video = "video"
    interval = "interval"
    scheduled = "scheduled"     # fixed-duration clips at a repeating interval


class StereoMode(str, Enum):
    main_only = "main_only"
    stereo_only = "stereo_only"
    both = "both"


class CameraStatus(BaseModel):
    id: str
    name: str
    connected: bool
    enabled: bool = True  # False = stream stopped to free bandwidth
    ip: str | None = None
    fps: float = 0.0
    latency_ms: float = 0.0
    recording: bool = False
    recording_mode: RecordingMode | None = None
    inference_mode: InferenceMode = InferenceMode.none
    width: int = 0
    height: int = 0
    # Stream settings
    stereo_mode: StereoMode = StereoMode.main_only
    stream_fps: int = 20
    mjpeg_quality: int = 85
    resolution: str = "720p"
    flip_180: bool = False  # Rotate stream 180° for ceiling-mounted cameras


class CameraControlRequest(BaseModel):
    # Exposure
    auto_exposure: bool | None = None
    exposure_us: int | None = None       # 1–33000
    iso: int | None = None               # 100–1600
    # Focus
    auto_focus: bool | None = None
    manual_focus: int | None = None      # 0–255
    # White balance
    auto_white_balance: bool | None = None
    white_balance_k: int | None = None   # 1000–12000
    # Image quality
    brightness: int | None = None        # -10 to 10
    contrast: int | None = None
    saturation: int | None = None
    sharpness: int | None = None
    luma_denoise: int | None = None      # 0–4
    chroma_denoise: int | None = None    # 0–4


class StreamSettingsRequest(BaseModel):
    fps: int | None = None               # 1–60
    mjpeg_quality: int | None = None     # 1–100
    resolution: str | None = None        # "4k", "1080p", "720p", "480p"
    stereo_mode: StereoMode | None = None
    flip_180: bool | None = None         # Rotate stream 180° for ceiling-mounted cameras


class RecordingStartRequest(BaseModel):
    mode: RecordingMode = RecordingMode.video
    interval_seconds: float = 5.0        # for interval mode
    output_dir: str | None = None        # custom output directory (uses default if None)
    filename_prefix: str = ""            # prefix for output filenames
    # Scheduled mode: record clip_duration_seconds every clip_interval_seconds
    clip_duration_seconds: float = 5.0   # length of each clip
    clip_interval_seconds: float = 80.0  # total cycle time (clip + idle)


class InferenceModeRequest(BaseModel):
    mode: InferenceMode
    model_path: str | None = None        # override default model


class BoundingBox(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    class_id: int
    label: str


class Detection(BaseModel):
    camera_id: str
    timestamp: float
    boxes: list[BoundingBox]
    inference_mode: InferenceMode


class StorageStatus(BaseModel):
    total_gb: float
    used_gb: float
    free_gb: float
    usage_pct: float
    recordings_gb: float


class CameraListResponse(BaseModel):
    cameras: list[CameraStatus]
    total: int


class BandwidthCheckRequest(BaseModel):
    resolution: str = "720p"
    quality: int = 85
    fps: int = 30
    num_cameras: int = 1
    stereo_mode: str = "main_only"


class ApiResponse(BaseModel):
    ok: bool
    message: str
    data: Any = None


# ---------------------------------------------------------------------------
# Calibration (IMU angle → camera settings)
# ---------------------------------------------------------------------------

class SaveCalibrationPointRequest(BaseModel):
    label: str = ""
    settings: CameraControlRequest          # reuse — may contain None fields


class CalibrationAutoApplyRequest(BaseModel):
    enabled: bool
    tolerance_deg: float | None = None


class CalibrationPointResponse(BaseModel):
    index: int
    label: str
    roll_deg: float
    pitch_deg: float
    settings: dict
    created_at: str


class CalibrationInterpolateFocusRequest(BaseModel):
    enabled: bool


class CalibrationProfileResponse(BaseModel):
    camera_id: str
    auto_apply: bool
    tolerance_deg: float
    interpolate_focus: bool = True
    points: list[CalibrationPointResponse]


# ---------------------------------------------------------------------------
# Radial-angle teach targets (closed-loop drive correction)
# ---------------------------------------------------------------------------

class CaptureAngleTargetRequest(BaseModel):
    camera_id: str            # OAK-D device mxid — source of the IMU reading
    cam_id: str               # logical cam_id ("cam1"/"cam2") — MQTT key
    checkpoint_name: str
    active_angle: Literal["roll", "pitch"]
    label: str = ""


class AngleTargetResponse(BaseModel):
    checkpoint_name: str
    axis: str
    active_angle: str
    target_angle_deg: float
    motor_position: float
    label: str
    created_at: str

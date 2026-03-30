from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel


class InferenceMode(str, Enum):
    none = "none"
    on_camera = "on_camera"
    host = "host"


class RecordingMode(str, Enum):
    video = "video"
    interval = "interval"


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


class RecordingStartRequest(BaseModel):
    mode: RecordingMode = RecordingMode.video
    interval_seconds: float = 5.0        # for interval mode
    output_dir: str | None = None        # custom output directory (uses default if None)
    filename_prefix: str = ""            # prefix for output filenames


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

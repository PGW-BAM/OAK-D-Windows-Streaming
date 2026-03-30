"""Bandwidth estimation and feasibility checking for OAK-D MJPEG streams.

Key finding from hardware testing: The OAK-D 4 Pro hardware MJPEG encoder
produces nearly constant frame sizes regardless of the JPEG quality setting.
Resolution and FPS are the only meaningful bandwidth controls.

Measured frame sizes (averaged across Q30-Q100):
  4K:    ~3,015 KB/frame  → ~725 Mbps @ 30 FPS
  1080p: ~641 KB/frame    → ~262 Mbps @ 30 FPS
  720p:  ~288 KB/frame    → ~110 Mbps @ 30 FPS
  480p:  ~117 KB/frame    → ~30 Mbps  @ 30 FPS

A live calibration test (bandwidth_test.py) can update the defaults with fresh
measurements stored in bandwidth_profiles.json.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POE_BANDWIDTH_GBPS = 2.5
USABLE_FRACTION = 0.80  # 80 % of link speed is realistic with protocol overhead
USABLE_BANDWIDTH_MBPS = POE_BANDWIDTH_GBPS * 1000 * USABLE_FRACTION  # 2000 Mbps

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "4k": (3840, 2160),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "480p": (640, 480),
}

STEREO_RESOLUTION: tuple[int, int] = (1280, 800)

PROFILES_PATH = Path(__file__).parent / "bandwidth_profiles.json"

# Measured average frame sizes per resolution (quality-independent).
# These are defaults from the 2026-03-13 hardware test; overridden at runtime
# if bandwidth_profiles.json exists.
_MEASURED_FRAME_BYTES: dict[str, int] = {
    "4k": 3_015_000,
    "1080p": 641_000,
    "720p": 288_000,
    "480p": 117_000,
}

# Stereo mono camera (1280x800) measured proportionally to 720p
_STEREO_FRAME_BYTES: int = 230_000


# ---------------------------------------------------------------------------
# Measured profiles (from bandwidth_test.py)
# ---------------------------------------------------------------------------

_measured_per_key: dict[str, int] = {}  # "720p_85" -> bytes
_profiles_loaded = False


def load_measured_profiles() -> bool:
    """Load measured frame sizes from bandwidth_profiles.json if available."""
    global _measured_per_key, _profiles_loaded, _MEASURED_FRAME_BYTES, _STEREO_FRAME_BYTES
    if not PROFILES_PATH.exists():
        return False
    try:
        data = json.loads(PROFILES_PATH.read_text())
        _measured_per_key = data.get("frame_sizes", {})

        # Compute per-resolution averages from all quality levels
        from collections import defaultdict
        res_sums: dict[str, list[int]] = defaultdict(list)
        for key, size in _measured_per_key.items():
            res_name = key.rsplit("_", 1)[0]
            res_sums[res_name].append(size)

        for res_name, sizes in res_sums.items():
            avg = int(sum(sizes) / len(sizes))
            _MEASURED_FRAME_BYTES[res_name] = avg

        # Estimate stereo from 720p ratio (stereo is 1280x800 vs 1280x720)
        if "720p" in _MEASURED_FRAME_BYTES:
            ratio = (1280 * 800) / (1280 * 720)
            _STEREO_FRAME_BYTES = int(_MEASURED_FRAME_BYTES["720p"] * ratio)

        _profiles_loaded = True
        logger.info(
            "Loaded measured bandwidth profiles: %s",
            {k: f"{v/1024:.0f} KB" for k, v in _MEASURED_FRAME_BYTES.items()},
        )
        return True
    except Exception as exc:
        logger.warning("Failed to load bandwidth profiles: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Frame size estimation
# ---------------------------------------------------------------------------

def estimate_frame_bytes(resolution: str) -> int:
    """Return average MJPEG frame size in bytes for a given resolution.

    Quality parameter is intentionally absent — the OAK-D hardware encoder
    produces constant-size frames regardless of quality setting.
    """
    return _MEASURED_FRAME_BYTES.get(resolution, _MEASURED_FRAME_BYTES.get("720p", 288_000))


def estimate_stream_bps(
    resolution: str,
    quality: int,
    fps: int,
    stereo_mode: str = "main_only",
) -> float:
    """Estimate bandwidth in bits/sec for one camera at given settings.

    The quality parameter is accepted for API compatibility but does not
    affect the estimate (hardware encoder produces constant frame sizes).
    """
    frame_bytes = 0

    need_main = stereo_mode in ("main_only", "both")
    need_stereo = stereo_mode in ("stereo_only", "both")

    if need_main:
        frame_bytes += estimate_frame_bytes(resolution)

    if need_stereo:
        # Two mono cameras (left + right)
        frame_bytes += 2 * _STEREO_FRAME_BYTES

    return frame_bytes * fps * 8


# ---------------------------------------------------------------------------
# Feasibility check
# ---------------------------------------------------------------------------

class BandwidthEstimate(BaseModel):
    resolution: str
    quality: int
    fps: int
    stereo_mode: str
    num_cameras: int
    per_camera_mbps: float
    total_mbps: float
    budget_mbps: float
    utilization_pct: float
    feasible: bool
    quality_affects_bandwidth: bool = False  # inform the UI


def check_feasibility(
    resolution: str,
    quality: int,
    fps: int,
    num_cameras: int,
    stereo_mode: str = "main_only",
) -> BandwidthEstimate:
    """Check whether a given configuration fits within the PoE bandwidth."""
    per_cam_bps = estimate_stream_bps(resolution, quality, fps, stereo_mode)
    total_bps = per_cam_bps * num_cameras
    budget_bps = USABLE_BANDWIDTH_MBPS * 1_000_000

    return BandwidthEstimate(
        resolution=resolution,
        quality=quality,
        fps=fps,
        stereo_mode=stereo_mode,
        num_cameras=num_cameras,
        per_camera_mbps=round(per_cam_bps / 1_000_000, 2),
        total_mbps=round(total_bps / 1_000_000, 2),
        budget_mbps=round(budget_bps / 1_000_000, 2),
        utilization_pct=round(total_bps / budget_bps * 100, 1),
        feasible=total_bps <= budget_bps,
        quality_affects_bandwidth=False,
    )


# ---------------------------------------------------------------------------
# Bandwidth matrix — max FPS for each combination
# ---------------------------------------------------------------------------

class BandwidthProfile(BaseModel):
    """One row in the bandwidth matrix."""
    resolution: str
    quality: int
    stereo_mode: str
    num_cameras: int
    max_fps: int
    per_camera_mbps_at_max: float
    total_mbps_at_max: float


class BandwidthMatrix(BaseModel):
    """Full bandwidth matrix for the settings info board."""
    poe_bandwidth_gbps: float
    usable_bandwidth_mbps: float
    profiles: list[BandwidthProfile]
    measured: bool  # True if from real measurements, False if estimated


def compute_max_fps(
    resolution: str,
    quality: int,
    num_cameras: int,
    stereo_mode: str = "main_only",
) -> int:
    """Binary-search for the highest FPS that stays within budget."""
    lo, hi = 1, 60
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        est = check_feasibility(resolution, quality, mid, num_cameras, stereo_mode)
        if est.feasible:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def build_bandwidth_matrix(
    qualities: list[int] | None = None,
    stereo_modes: list[str] | None = None,
) -> BandwidthMatrix:
    """Build the full matrix of max-FPS values for all combinations."""
    if qualities is None:
        qualities = [30, 50, 70, 85, 100]
    if stereo_modes is None:
        stereo_modes = ["main_only"]

    profiles: list[BandwidthProfile] = []

    for stereo in stereo_modes:
        for res_name in RESOLUTIONS:
            for q in qualities:
                for n_cams in range(1, 5):  # 1-4 cameras
                    max_fps = compute_max_fps(res_name, q, n_cams, stereo)
                    if max_fps > 0:
                        est = check_feasibility(res_name, q, max_fps, n_cams, stereo)
                        per_cam = est.per_camera_mbps
                        total = est.total_mbps
                    else:
                        est1 = check_feasibility(res_name, q, 1, n_cams, stereo)
                        per_cam = est1.per_camera_mbps
                        total = est1.total_mbps

                    profiles.append(BandwidthProfile(
                        resolution=res_name,
                        quality=q,
                        stereo_mode=stereo,
                        num_cameras=n_cams,
                        max_fps=max_fps,
                        per_camera_mbps_at_max=per_cam,
                        total_mbps_at_max=total,
                    ))

    return BandwidthMatrix(
        poe_bandwidth_gbps=POE_BANDWIDTH_GBPS,
        usable_bandwidth_mbps=USABLE_BANDWIDTH_MBPS,
        profiles=profiles,
        measured=_profiles_loaded,
    )


# Try to load at import time
load_measured_profiles()

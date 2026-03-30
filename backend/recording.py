"""Video and interval-image recording for OAK-D streams."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import settings
from .models import RecordingMode

logger = logging.getLogger(__name__)


def _recordings_dir(camera_id: str, output_dir: Path | None = None) -> Path:
    base = output_dir if output_dir else settings.recordings_dir
    d = base / camera_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ts_filename(camera_id: str, ext: str, prefix: str = "", suffix: str = "") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    parts = []
    if prefix:
        parts.append(prefix)
    parts.append(f"{camera_id}_{ts}")
    if suffix:
        parts.append(suffix)
    return "_".join(parts) + f".{ext}"


# ---------------------------------------------------------------------------
# Video recording (MJPEG → MP4 via PyAV)
# ---------------------------------------------------------------------------

class VideoRecorder:
    """Wraps a PyAV muxer to write MJPEG packets into an MP4 container."""

    def __init__(self, camera_id: str, fps: int = 20, output_dir: Path | None = None,
                 prefix: str = "", suffix: str = "") -> None:
        self.camera_id = camera_id
        self.fps = fps
        self._output_dir = output_dir
        self._prefix = prefix
        self._suffix = suffix
        self._container = None
        self._stream = None
        self._lock = threading.Lock()
        self._active = False
        self._path: Optional[Path] = None

    def start(self) -> Path:
        import av
        out_dir = _recordings_dir(self.camera_id, self._output_dir)
        fname = _ts_filename(self.camera_id, "mp4", prefix=self._prefix, suffix=self._suffix)
        self._path = out_dir / fname
        self._container = av.open(str(self._path), mode="w")
        self._stream = self._container.add_stream("mjpeg", rate=self.fps)
        self._stream.pix_fmt = "yuvj420p"
        self._active = True
        logger.info("Video recording started: %s", self._path)
        return self._path

    def feed(self, jpeg_bytes: bytes) -> None:
        if not self._active or self._container is None:
            return
        import av
        with self._lock:
            try:
                packet = av.Packet(jpeg_bytes)
                packet.stream = self._stream
                self._container.mux(packet)
            except Exception as exc:
                logger.warning("Video mux error: %s", exc)

    def stop(self) -> Optional[Path]:
        self._active = False
        with self._lock:
            if self._container:
                try:
                    self._container.close()
                except Exception as exc:
                    logger.warning("Error closing video container: %s", exc)
                self._container = None
                self._stream = None
        path = self._path
        self._path = None
        logger.info("Video recording stopped: %s", path)
        return path


# ---------------------------------------------------------------------------
# Interval image recording
# ---------------------------------------------------------------------------

class IntervalRecorder:
    """Saves JPEG frames at a fixed time interval in a background thread."""

    def __init__(self, camera_id: str, interval_seconds: float = 5.0,
                 output_dir: Path | None = None, prefix: str = "") -> None:
        self.camera_id = camera_id
        self.interval_seconds = interval_seconds
        self._custom_output_dir = output_dir
        self._prefix = prefix
        self._latest_frame: bytes = b""
        self._latest_left: bytes = b""
        self._latest_right: bytes = b""
        self._frame_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._out_dir: Optional[Path] = None

    def start(self) -> Path:
        self._out_dir = _recordings_dir(self.camera_id, self._custom_output_dir) / "interval"
        self._out_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"interval-{self.camera_id[:8]}", daemon=True
        )
        self._thread.start()
        logger.info(
            "Interval recording started for %s @ %.1fs", self.camera_id, self.interval_seconds
        )
        return self._out_dir

    def feed(self, jpeg_bytes: bytes) -> None:
        with self._frame_lock:
            self._latest_frame = jpeg_bytes

    def feed_left(self, jpeg_bytes: bytes) -> None:
        with self._frame_lock:
            self._latest_left = jpeg_bytes

    def feed_right(self, jpeg_bytes: bytes) -> None:
        with self._frame_lock:
            self._latest_right = jpeg_bytes

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.interval_seconds + 2)
        logger.info("Interval recording stopped for %s", self.camera_id)

    def _loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            with self._frame_lock:
                main_data = self._latest_frame
                left_data = self._latest_left
                right_data = self._latest_right
            if not self._out_dir:
                continue
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
            prefix_part = f"{self._prefix}_" if self._prefix else ""
            base = f"{prefix_part}{self.camera_id}_{ts}"
            # Save main frame
            if main_data:
                try:
                    (self._out_dir / f"{base}.jpg").write_bytes(main_data)
                except Exception as exc:
                    logger.warning("Interval capture write error: %s", exc)
            # Save stereo frames if available
            if left_data:
                try:
                    (self._out_dir / f"{base}_left.jpg").write_bytes(left_data)
                except Exception as exc:
                    logger.warning("Interval left capture error: %s", exc)
            if right_data:
                try:
                    (self._out_dir / f"{base}_right.jpg").write_bytes(right_data)
                except Exception as exc:
                    logger.warning("Interval right capture error: %s", exc)


# ---------------------------------------------------------------------------
# Unified recording worker attached to a CameraWorker
# ---------------------------------------------------------------------------

class RecordingWorker:
    """Delegates feed() to the active recorder (video or interval)."""

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self._mode: Optional[RecordingMode] = None
        self._video: Optional[VideoRecorder] = None
        self._video_left: Optional[VideoRecorder] = None
        self._video_right: Optional[VideoRecorder] = None
        self._interval: Optional[IntervalRecorder] = None

    def start(
        self,
        mode: RecordingMode,
        interval_seconds: float = 5.0,
        output_dir: Path | None = None,
        filename_prefix: str = "",
        stereo_capture: bool = False,
    ) -> Path:
        self.stop()  # ensure clean state
        self._mode = mode
        if mode == RecordingMode.video:
            self._video = VideoRecorder(
                self.camera_id, fps=settings.stream_fps,
                output_dir=output_dir, prefix=filename_prefix,
            )
            path = self._video.start()
            if stereo_capture:
                self._video_left = VideoRecorder(
                    self.camera_id, fps=settings.stream_fps,
                    output_dir=output_dir, prefix=filename_prefix, suffix="left",
                )
                self._video_left.start()
                self._video_right = VideoRecorder(
                    self.camera_id, fps=settings.stream_fps,
                    output_dir=output_dir, prefix=filename_prefix, suffix="right",
                )
                self._video_right.start()
            return path
        else:
            self._interval = IntervalRecorder(
                self.camera_id, interval_seconds,
                output_dir=output_dir, prefix=filename_prefix,
            )
            return self._interval.start()

    def feed(self, jpeg_bytes: bytes) -> None:
        if self._video:
            self._video.feed(jpeg_bytes)
        if self._interval:
            self._interval.feed(jpeg_bytes)

    def feed_left(self, jpeg_bytes: bytes) -> None:
        if self._video_left:
            self._video_left.feed(jpeg_bytes)
        if self._interval:
            self._interval.feed_left(jpeg_bytes)

    def feed_right(self, jpeg_bytes: bytes) -> None:
        if self._video_right:
            self._video_right.feed(jpeg_bytes)
        if self._interval:
            self._interval.feed_right(jpeg_bytes)

    def stop(self) -> None:
        if self._video:
            self._video.stop()
            self._video = None
        if self._video_left:
            self._video_left.stop()
            self._video_left = None
        if self._video_right:
            self._video_right.stop()
            self._video_right = None
        if self._interval:
            self._interval.stop()
            self._interval = None
        self._mode = None

    @property
    def active(self) -> bool:
        return (self._video is not None or self._interval is not None
                or self._video_left is not None)

    @property
    def mode(self) -> Optional[RecordingMode]:
        return self._mode


# ---------------------------------------------------------------------------
# Storage management
# ---------------------------------------------------------------------------

def get_storage_stats() -> dict:
    rec_dir = settings.recordings_dir
    rec_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(rec_dir)
    rec_bytes = sum(f.stat().st_size for f in rec_dir.rglob("*") if f.is_file())
    return {
        "total_gb": usage.total / 1e9,
        "used_gb": usage.used / 1e9,
        "free_gb": usage.free / 1e9,
        "usage_pct": usage.used / usage.total * 100,
        "recordings_gb": rec_bytes / 1e9,
    }


def cleanup_old_recordings() -> int:
    """Delete oldest recordings until disk usage drops below threshold."""
    stats = get_storage_stats()
    if stats["usage_pct"] < settings.storage_threshold_pct:
        return 0

    files = sorted(
        (f for f in settings.recordings_dir.rglob("*") if f.is_file()),
        key=lambda f: f.stat().st_mtime,
    )
    deleted = 0
    for f in files:
        try:
            f.unlink()
            deleted += 1
            logger.info("Deleted old recording: %s", f)
        except Exception as exc:
            logger.warning("Could not delete %s: %s", f, exc)
        if get_storage_stats()["usage_pct"] < settings.storage_threshold_pct - 5:
            break
    return deleted

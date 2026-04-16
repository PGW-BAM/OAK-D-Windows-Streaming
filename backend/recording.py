"""Video and interval-image recording for OAK-D streams."""
from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .config import settings
from .models import RecordingMode


@dataclass
class RecordingMetadata:
    """Snapshot of IMU angles and drive positions at a moment during recording."""
    cam_id: str
    timestamp: datetime
    roll_deg: float | None
    pitch_deg: float | None
    drive_a: float | None
    drive_b: float | None


MetadataProvider = Callable[[], RecordingMetadata]


def _burn_overlay(jpeg_bytes: bytes, meta: RecordingMetadata) -> bytes:
    """Burn a small metadata strip onto the bottom edge of a JPEG frame."""
    import cv2
    import numpy as np

    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return jpeg_bytes

    roll_s = f"{meta.roll_deg:+.1f}" if meta.roll_deg is not None else "N/A"
    pitch_s = f"{meta.pitch_deg:+.1f}" if meta.pitch_deg is not None else "N/A"
    drv_a = f"{meta.drive_a:.0f}" if meta.drive_a is not None else "\u2014"
    drv_b = f"{meta.drive_b:.0f}" if meta.drive_b is not None else "\u2014"
    ts_s = meta.timestamp.strftime("%Y-%m-%dT%H:%M:%S UTC")
    line = (
        f"{meta.cam_id}  {ts_s}  "
        f"Roll:{roll_s}\u00b0  Pitch:{pitch_s}\u00b0  "
        f"DrvA:{drv_a}  DrvB:{drv_b}"
    )

    h, w = img.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale, thickness = 0.38, 1
    (_, th), _ = cv2.getTextSize(line, font, scale, thickness)
    pad = 4
    y0 = h - pad
    cv2.rectangle(img, (0, y0 - th - pad * 2), (w, h), (0, 0, 0), -1)
    cv2.putText(img, line, (pad, y0 - pad), font, scale, (200, 200, 200), thickness, cv2.LINE_AA)

    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return bytes(buf) if ok else jpeg_bytes

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
    """Wraps a PyAV muxer to write MJPEG packets into an MP4 container.

    Muxing runs on a dedicated background thread so that feed() never blocks
    the camera worker thread.  At 4K+60fps, each mux() call is a disk write
    that could take tens of milliseconds; blocking the camera thread on that
    prevents it from draining the DepthAI queue and breaks the live stream.

    The queue holds up to 120 frames (~2s at 60fps).  If the disk is too slow
    to keep up, frames are silently dropped rather than stalling the camera.
    """

    def __init__(self, camera_id: str, fps: int = 20, output_dir: Path | None = None,
                 prefix: str = "", suffix: str = "",
                 metadata_provider: MetadataProvider | None = None) -> None:
        self.camera_id = camera_id
        self.fps = fps
        self._output_dir = output_dir
        self._prefix = prefix
        self._suffix = suffix
        self._metadata_provider = metadata_provider
        self._container = None
        self._stream = None
        self._active = False
        self._path: Optional[Path] = None
        self._sidecar_path: Optional[Path] = None
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=120)
        self._mux_thread: Optional[threading.Thread] = None

    def start(self) -> Path:
        import av
        out_dir = _recordings_dir(self.camera_id, self._output_dir)
        fname = _ts_filename(self.camera_id, "mp4", prefix=self._prefix, suffix=self._suffix)
        self._path = out_dir / fname
        self._container = av.open(str(self._path), mode="w")
        self._stream = self._container.add_stream("mjpeg", rate=self.fps)
        self._stream.pix_fmt = "yuvj420p"
        self._stream.time_base = Fraction(1, self.fps)
        self._active = True
        self._mux_thread = threading.Thread(
            target=self._mux_loop,
            name=f"mux-{self.camera_id[:8]}",
            daemon=True,
        )
        self._mux_thread.start()
        # Write initial JSON sidecar with recording context
        if self._metadata_provider:
            try:
                meta = self._metadata_provider()
                sidecar = {
                    "cam_id": meta.cam_id,
                    "start_timestamp": meta.timestamp.isoformat(),
                    "roll_deg_start": meta.roll_deg,
                    "pitch_deg_start": meta.pitch_deg,
                    "drive_a_start": meta.drive_a,
                    "drive_b_start": meta.drive_b,
                    "end_timestamp": None,
                    "roll_deg_end": None,
                    "pitch_deg_end": None,
                    "drive_a_end": None,
                    "drive_b_end": None,
                }
                self._sidecar_path = self._path.with_suffix(".json")
                self._sidecar_path.write_text(json.dumps(sidecar, indent=2))
            except Exception as exc:
                logger.warning("Could not write video sidecar for %s: %s", self.camera_id, exc)
        logger.info("Video recording started: %s", self._path)
        return self._path

    def feed(self, jpeg_bytes: bytes) -> None:
        """Non-blocking: drops the frame if the mux queue is full."""
        if not self._active:
            return
        try:
            self._queue.put_nowait(jpeg_bytes)
        except queue.Full:
            logger.debug("Mux queue full for %s — frame dropped", self.camera_id)

    def _mux_loop(self) -> None:
        """Drain the queue and write frames to the MP4 container."""
        import av
        pts = 0
        time_base = Fraction(1, self.fps)
        while True:
            item = self._queue.get()
            if item is None:  # stop sentinel
                break
            try:
                packet = av.Packet(item)
                packet.stream = self._stream
                packet.pts = pts
                packet.dts = pts
                packet.time_base = time_base
                self._container.mux(packet)
                pts += 1
            except Exception as exc:
                logger.warning("Video mux error for %s: %s", self.camera_id, exc)

    def stop(self) -> Optional[Path]:
        self._active = False
        # Update sidecar with final context before draining the mux queue
        if self._metadata_provider and self._sidecar_path and self._sidecar_path.exists():
            try:
                meta = self._metadata_provider()
                data = json.loads(self._sidecar_path.read_text())
                data.update({
                    "end_timestamp": meta.timestamp.isoformat(),
                    "roll_deg_end": meta.roll_deg,
                    "pitch_deg_end": meta.pitch_deg,
                    "drive_a_end": meta.drive_a,
                    "drive_b_end": meta.drive_b,
                })
                self._sidecar_path.write_text(json.dumps(data, indent=2))
            except Exception as exc:
                logger.warning("Could not update video sidecar for %s: %s", self.camera_id, exc)
        self._sidecar_path = None
        # Send sentinel so the mux thread drains remaining frames and exits
        self._queue.put(None)
        if self._mux_thread:
            self._mux_thread.join(timeout=30)
            if self._mux_thread.is_alive():
                logger.warning("Mux thread for %s did not finish in 30s", self.camera_id)
            self._mux_thread = None
        if self._container:
            try:
                self._container.close()
            except Exception as exc:
                logger.warning("Error closing video container for %s: %s", self.camera_id, exc)
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
                 output_dir: Path | None = None, prefix: str = "",
                 metadata_provider: MetadataProvider | None = None) -> None:
        self.camera_id = camera_id
        self.interval_seconds = interval_seconds
        self._custom_output_dir = output_dir
        self._prefix = prefix
        self._metadata_provider = metadata_provider
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

            # Collect metadata snapshot for this capture tick
            meta = None
            if self._metadata_provider:
                try:
                    meta = self._metadata_provider()
                except Exception as exc:
                    logger.warning("Metadata provider error for %s: %s", self.camera_id, exc)

            # Build filename — append roll tag when IMU data is available
            if meta and meta.roll_deg is not None:
                sign = "+" if meta.roll_deg >= 0 else ""
                roll_tag = f"_roll{sign}{meta.roll_deg:.1f}deg"
            else:
                roll_tag = ""
            base = f"{prefix_part}{self.camera_id}_{ts}{roll_tag}"

            # Save main frame with overlay burned in
            if main_data:
                try:
                    frame_to_save = _burn_overlay(main_data, meta) if meta else main_data
                    (self._out_dir / f"{base}.jpg").write_bytes(frame_to_save)
                    # JSON sidecar alongside the image
                    if meta:
                        sidecar = {
                            "cam_id": meta.cam_id,
                            "timestamp": meta.timestamp.isoformat(),
                            "roll_deg": meta.roll_deg,
                            "pitch_deg": meta.pitch_deg,
                            "drive_a": meta.drive_a,
                            "drive_b": meta.drive_b,
                        }
                        (self._out_dir / f"{base}.json").write_text(
                            json.dumps(sidecar, indent=2)
                        )
                except Exception as exc:
                    logger.warning("Interval capture write error: %s", exc)
            # Save stereo frames if available (no overlay, shared base name)
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
# Scheduled video recording (fixed-duration clips at a repeating interval)
# ---------------------------------------------------------------------------

class ScheduledVideoRecorder:
    """Records short video clips on a repeating schedule.

    Timeline: [clip_duration_s video] [idle until interval_s elapsed] [clip_duration_s video] ...

    ``clip_interval_seconds`` is the *total cycle time* — the time from the start of
    one clip to the start of the next.  ``clip_duration_seconds`` must be shorter than
    ``clip_interval_seconds``; the remainder is idle time between clips.

    Example: clip_duration=5s, clip_interval=80s → record 5s, wait 75s, repeat.
    """

    def __init__(
        self,
        camera_id: str,
        clip_duration_seconds: float,
        clip_interval_seconds: float,
        fps: int = 20,
        output_dir: Path | None = None,
        prefix: str = "",
        stereo_capture: bool = False,
        metadata_provider: MetadataProvider | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.clip_duration_seconds = max(1.0, clip_duration_seconds)
        self.clip_interval_seconds = max(self.clip_duration_seconds + 1.0, clip_interval_seconds)
        self.fps = fps
        self._output_dir = output_dir
        self._prefix = prefix
        self._stereo_capture = stereo_capture
        self._metadata_provider = metadata_provider

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._current_main: Optional[VideoRecorder] = None
        self._current_left: Optional[VideoRecorder] = None
        self._current_right: Optional[VideoRecorder] = None
        self._out_dir: Optional[Path] = None

    def start(self) -> Path:
        self._out_dir = _recordings_dir(self.camera_id, self._output_dir)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"sched-{self.camera_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Scheduled recording started for %s — clip=%.0fs interval=%.0fs",
            self.camera_id, self.clip_duration_seconds, self.clip_interval_seconds,
        )
        return self._out_dir

    def feed(self, jpeg_bytes: bytes) -> None:
        with self._lock:
            rec = self._current_main
        if rec:
            rec.feed(jpeg_bytes)

    def feed_left(self, jpeg_bytes: bytes) -> None:
        with self._lock:
            rec = self._current_left
        if rec:
            rec.feed(jpeg_bytes)

    def feed_right(self, jpeg_bytes: bytes) -> None:
        with self._lock:
            rec = self._current_right
        if rec:
            rec.feed(jpeg_bytes)

    def stop(self) -> None:
        self._stop_event.set()
        # Grab and null out current recorders under the lock so _loop won't touch them
        with self._lock:
            main_rec = self._current_main
            left_rec = self._current_left
            right_rec = self._current_right
            self._current_main = None
            self._current_left = None
            self._current_right = None
        # Stop any clip that is currently recording
        for rec in (main_rec, left_rec, right_rec):
            if rec:
                try:
                    rec.stop()
                except Exception as exc:
                    logger.warning("Error stopping scheduled clip: %s", exc)
        if self._thread:
            self._thread.join(timeout=self.clip_interval_seconds + 15)
            if self._thread.is_alive():
                logger.warning("Scheduled recorder thread for %s did not finish", self.camera_id)
            self._thread = None
        logger.info("Scheduled recording stopped for %s", self.camera_id)

    def _loop(self) -> None:
        clip_num = 0
        while not self._stop_event.is_set():
            clip_num += 1
            # Build recorders for this clip
            main_rec = VideoRecorder(
                self.camera_id, fps=self.fps,
                output_dir=self._output_dir, prefix=self._prefix,
                suffix=f"sched{clip_num:04d}",
                metadata_provider=self._metadata_provider,
            )
            left_rec = VideoRecorder(
                self.camera_id, fps=self.fps,
                output_dir=self._output_dir, prefix=self._prefix,
                suffix=f"sched{clip_num:04d}_left",
                metadata_provider=self._metadata_provider,
            ) if self._stereo_capture else None
            right_rec = VideoRecorder(
                self.camera_id, fps=self.fps,
                output_dir=self._output_dir, prefix=self._prefix,
                suffix=f"sched{clip_num:04d}_right",
                metadata_provider=self._metadata_provider,
            ) if self._stereo_capture else None

            # Start clip — expose recorders so feed() can route frames
            main_rec.start()
            if left_rec:
                left_rec.start()
            if right_rec:
                right_rec.start()

            with self._lock:
                self._current_main = main_rec
                self._current_left = left_rec
                self._current_right = right_rec

            # Record for clip_duration_seconds (stop early if signalled)
            self._stop_event.wait(self.clip_duration_seconds)

            # Stop feeding frames before closing the containers
            with self._lock:
                self._current_main = None
                self._current_left = None
                self._current_right = None

            main_rec.stop()
            if left_rec:
                left_rec.stop()
            if right_rec:
                right_rec.stop()

            if self._stop_event.is_set():
                break

            # Wait out the remaining idle time before the next clip
            idle = self.clip_interval_seconds - self.clip_duration_seconds
            if idle > 0:
                self._stop_event.wait(idle)


# ---------------------------------------------------------------------------
# Unified recording worker attached to a CameraWorker
# ---------------------------------------------------------------------------

class RecordingWorker:
    """Delegates feed() to the active recorder (video, interval, or scheduled)."""

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self._mode: Optional[RecordingMode] = None
        self._video: Optional[VideoRecorder] = None
        self._video_left: Optional[VideoRecorder] = None
        self._video_right: Optional[VideoRecorder] = None
        self._interval: Optional[IntervalRecorder] = None
        self._scheduled: Optional[ScheduledVideoRecorder] = None

    def start(
        self,
        mode: RecordingMode,
        interval_seconds: float = 5.0,
        output_dir: Path | None = None,
        filename_prefix: str = "",
        stereo_capture: bool = False,
        fps: int | None = None,
        metadata_provider: MetadataProvider | None = None,
        clip_duration_seconds: float = 5.0,
        clip_interval_seconds: float = 80.0,
    ) -> Path:
        self.stop()  # ensure clean state
        self._mode = mode
        effective_fps = fps if fps is not None else settings.stream_fps
        if mode == RecordingMode.video:
            self._video = VideoRecorder(
                self.camera_id, fps=effective_fps,
                output_dir=output_dir, prefix=filename_prefix,
                metadata_provider=metadata_provider,
            )
            path = self._video.start()
            if stereo_capture:
                self._video_left = VideoRecorder(
                    self.camera_id, fps=effective_fps,
                    output_dir=output_dir, prefix=filename_prefix, suffix="left",
                    metadata_provider=metadata_provider,
                )
                self._video_left.start()
                self._video_right = VideoRecorder(
                    self.camera_id, fps=effective_fps,
                    output_dir=output_dir, prefix=filename_prefix, suffix="right",
                    metadata_provider=metadata_provider,
                )
                self._video_right.start()
            return path
        elif mode == RecordingMode.scheduled:
            self._scheduled = ScheduledVideoRecorder(
                self.camera_id,
                clip_duration_seconds=clip_duration_seconds,
                clip_interval_seconds=clip_interval_seconds,
                fps=effective_fps,
                output_dir=output_dir,
                prefix=filename_prefix,
                stereo_capture=stereo_capture,
                metadata_provider=metadata_provider,
            )
            return self._scheduled.start()
        else:
            self._interval = IntervalRecorder(
                self.camera_id, interval_seconds,
                output_dir=output_dir, prefix=filename_prefix,
                metadata_provider=metadata_provider,
            )
            return self._interval.start()

    def feed(self, jpeg_bytes: bytes) -> None:
        if self._video:
            self._video.feed(jpeg_bytes)
        if self._interval:
            self._interval.feed(jpeg_bytes)
        if self._scheduled:
            self._scheduled.feed(jpeg_bytes)

    def feed_left(self, jpeg_bytes: bytes) -> None:
        if self._video_left:
            self._video_left.feed(jpeg_bytes)
        if self._interval:
            self._interval.feed_left(jpeg_bytes)
        if self._scheduled:
            self._scheduled.feed_left(jpeg_bytes)

    def feed_right(self, jpeg_bytes: bytes) -> None:
        if self._video_right:
            self._video_right.feed(jpeg_bytes)
        if self._interval:
            self._interval.feed_right(jpeg_bytes)
        if self._scheduled:
            self._scheduled.feed_right(jpeg_bytes)

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
        if self._scheduled:
            self._scheduled.stop()
            self._scheduled = None
        self._mode = None

    @property
    def active(self) -> bool:
        return (self._video is not None or self._interval is not None
                or self._video_left is not None or self._scheduled is not None)

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

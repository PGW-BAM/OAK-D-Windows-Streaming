"""Kreuzstoss Programm — looping, bandwidth-safe dual-camera recording sequence.

Each cycle records a fixed sequence on cam1 and cam2 with only one camera at
high bandwidth at any time (the other streams at 1080p / 29 fps so the live
preview stays alive without exceeding the PoE budget). Cycles repeat forever
with an operator-configurable interval until stop is requested or the disk
fills up.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel

from .camera_manager import CameraManager, CameraWorker
from .models import RecordingMode, StreamSettingsRequest
from .recording import (
    MetadataProvider,
    RecordingMetadata,
    VideoRecorder,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

CLIP_4K_SECONDS = 5.0
CLIP_1080P_SECONDS = 5.0

# Resolution strings must match RESOLUTION_PRESETS keys in camera_manager.py.
RES_4K = "4k"
RES_1080P = "1080p"

READY_TIMEOUT_S = 40.0      # max wait for first frame after pipeline rebuild
                            # (covers slow teardown of the previous pipeline)
DISK_FREE_FLOOR_GB = 2.0    # stop the loop if free space drops below this

# Settle pauses around mode transitions. wait_for_ready only confirms the
# encoder produced one frame; the auto-exposure / white balance loops and the
# encoder's bitstream still need a moment to stabilise — without this, the
# first second of a recording can be black. Generous values trade a few extra
# seconds per cycle for reliable, fully exposed recordings.
SETTLE_AFTER_REBUILD_S = 3.5     # after a pipeline rebuild, before recording
SETTLE_AFTER_RECORD_S = 2.0      # after rec.stop, before the next mode change
SETTLE_BEFORE_SNAPSHOT_S = 1.0   # let the most recent frame land in the buffer
SETTLE_AFTER_SNAPSHOT_S = 1.0

# Minimum interval the operator can request — covers the longest pipeline
# rebuild + settle time so the next cycle starts cleanly.
MIN_INTERVAL_SECONDS = 5.0
DEFAULT_INTERVAL_SECONDS = 5.0

# Stop the loop after this many consecutive failed cycles to avoid hammering
# a camera that's permanently down. Transient failures (one bad rebuild) are
# absorbed silently so the loop can run unattended for hours/days.
_MAX_CONSECUTIVE_CYCLE_FAILURES = 5

TOTAL_STEPS_PER_CYCLE = 14
# Simple mode (1080p video + 4K snapshot) sidesteps the GbE bandwidth limit
# entirely: 1080p MJPEG at 59 fps fits comfortably, and 4K is only used for
# single-frame snapshots so per-second bandwidth doesn't matter.
TOTAL_STEPS_PER_CYCLE_SIMPLE = 12

KreuzstossMode = Literal["full", "simple"]


class KreuzstossError(RuntimeError):
    """Raised by the orchestrator when a step cannot proceed."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KreuzstossConfig:
    save_dir: Path
    prefix: str
    interval_seconds: float
    mode: KreuzstossMode = "full"


@dataclass
class CameraSnapshot:
    mxid: str
    cam_id: str
    resolution: str
    stream_fps: int
    mjpeg_quality: int


class KreuzstossStatus(BaseModel):
    running: bool
    cycle_index: int
    step_index: int
    total_steps: int = TOTAL_STEPS_PER_CYCLE
    current_step: str
    phase: Literal[
        "idle", "cam1_active", "cam2_active",
        "interval", "restoring", "error", "done", "stopped",
    ]
    mode: KreuzstossMode = "full"
    started_at: str | None = None
    cycle_started_at: str | None = None
    interval_remaining_s: float | None = None
    save_dir: str
    interval_seconds: float
    free_space_gb: float | None = None
    error: str | None = None
    last_artifact: str | None = None
    artifacts_total: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_ms_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3]


def _strip_jpeg_exif(data: bytes) -> bytes:
    """Remove the APP1/Exif segment from a JPEG.

    The OAK-D's MJPEG encoder embeds an EXIF block whose DateTimeOriginal
    reflects the device's uptime-since-boot (mapped to the 1970 epoch),
    which Windows Explorer shows as "Date Taken: 1970-01-16". Stripping
    the APP1 segment leaves the file's mtime as the only date source.
    """
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return data
    out = bytearray(data[:2])
    i = 2
    while i < len(data) - 4:
        if data[i] != 0xFF:
            return data  # malformed — return unchanged
        marker = data[i + 1]
        # Start of scan / end of image: copy the rest verbatim.
        if marker == 0xDA or marker == 0xD9:
            out.extend(data[i:])
            return bytes(out)
        seg_len = (data[i + 2] << 8) | data[i + 3]
        seg_end = i + 2 + seg_len
        # APP1 + "Exif\0\0" header → drop it.
        if marker == 0xE1 and data[i + 4 : i + 10] == b"Exif\x00\x00":
            i = seg_end
            continue
        out.extend(data[i:seg_end])
        i = seg_end
    return bytes(out)


def resolve_save_dir(requested: Path, fallback: Path) -> Path:
    """Return the first writable directory among (requested, fallback)."""
    candidates = [requested, fallback]
    last_exc: Optional[Exception] = None
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".kreuzstoss_write_probe"
            probe.write_bytes(b"ok")
            probe.unlink()
            logger.info("Kreuzstoss: using save dir %s", cand)
            return cand
        except OSError as exc:
            last_exc = exc
            logger.warning(
                "Kreuzstoss: %s unwritable (%s) — trying fallback", cand, exc,
            )
    raise RuntimeError(
        f"No writable save directory available (last error: {last_exc})"
    )


def _free_gb(path: Path) -> float | None:
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError as exc:
        logger.debug("disk_usage(%s) failed: %s", path, exc)
        return None


class _RecordingShim:
    """Adapter so CameraWorker._process_frame can feed a single VideoRecorder."""

    def __init__(self, rec: VideoRecorder) -> None:
        self._rec = rec

    def feed(
        self,
        jpeg_bytes: bytes,
        *,
        capture_ts_s: float | None = None,
        seq_num: int | None = None,
    ) -> None:
        self._rec.feed(
            jpeg_bytes, capture_ts_s=capture_ts_s, seq_num=seq_num,
        )

    def feed_left(self, _: bytes, **__: object) -> None:
        pass

    def feed_right(self, _: bytes, **__: object) -> None:
        pass

    @property
    def active(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class KreuzstossRunner:
    """Async state machine driving the Kreuzstoss recording loop."""

    def __init__(
        self,
        manager: CameraManager,
        cfg: KreuzstossConfig,
        metadata_provider_factory=None,
    ) -> None:
        self._manager = manager
        self._cfg = cfg
        self._metadata_factory = metadata_provider_factory
        self._stop_event = asyncio.Event()

        self._snapshots: list[CameraSnapshot] = []
        self._cycle_index = 0
        self._step_index = 0
        self._current_step = "idle"
        self._phase: str = "idle"
        self._started_at: datetime | None = None
        self._cycle_started_at: datetime | None = None
        self._interval_remaining_s: float | None = None
        self._error: str | None = None
        self._last_artifact: str | None = None
        self._artifacts_total = 0
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        logger.info("Kreuzstoss: stop requested")
        self._stop_event.set()

    def status(self) -> KreuzstossStatus:
        total = (
            TOTAL_STEPS_PER_CYCLE_SIMPLE
            if self._cfg.mode == "simple"
            else TOTAL_STEPS_PER_CYCLE
        )
        return KreuzstossStatus(
            running=self._running,
            cycle_index=self._cycle_index,
            step_index=self._step_index,
            total_steps=total,
            current_step=self._current_step,
            phase=self._phase,
            mode=self._cfg.mode,
            started_at=self._started_at.isoformat() if self._started_at else None,
            cycle_started_at=(
                self._cycle_started_at.isoformat() if self._cycle_started_at else None
            ),
            interval_remaining_s=self._interval_remaining_s,
            save_dir=str(self._cfg.save_dir),
            interval_seconds=self._cfg.interval_seconds,
            free_space_gb=_free_gb(self._cfg.save_dir),
            error=self._error,
            last_artifact=self._last_artifact,
            artifacts_total=self._artifacts_total,
        )

    async def run(self) -> None:
        self._running = True
        self._started_at = datetime.now(timezone.utc)
        try:
            self._set_step(1, "Capturing pre-program settings", "cam1_active")
            self._capture_initial_snapshots()

            consecutive_failures = 0
            while not self._stop_event.is_set():
                self._cycle_index += 1
                self._cycle_started_at = datetime.now(timezone.utc)
                logger.info(
                    "Kreuzstoss[%s]: starting cycle %d",
                    self._cfg.mode, self._cycle_index,
                )
                cycle_fn = (
                    self._run_cycle_simple
                    if self._cfg.mode == "simple"
                    else self._run_cycle
                )
                try:
                    await cycle_fn()
                    consecutive_failures = 0
                    self._error = None
                except KreuzstossError as exc:
                    # A single cycle failed (e.g. a camera missed its first
                    # frame after a rebuild) — log, surface the error in
                    # status, and try the next cycle. The user wants the
                    # loop to keep going indefinitely, only stopping on
                    # explicit stop or disk-full.
                    consecutive_failures += 1
                    self._error = (
                        f"Cycle {self._cycle_index} failed "
                        f"({consecutive_failures} consecutive): {exc}"
                    )
                    logger.warning("Kreuzstoss: %s", self._error)
                    if consecutive_failures >= _MAX_CONSECUTIVE_CYCLE_FAILURES:
                        logger.error(
                            "Kreuzstoss: %d consecutive failures — stopping",
                            consecutive_failures,
                        )
                        self._phase = "error"
                        break
                if self._stop_event.is_set():
                    break

                free = _free_gb(self._cfg.save_dir)
                if free is not None and free < DISK_FREE_FLOOR_GB:
                    self._error = (
                        f"Free disk space {free:.2f} GB below floor "
                        f"{DISK_FREE_FLOOR_GB:.1f} GB — stopping"
                    )
                    logger.warning("Kreuzstoss: %s", self._error)
                    self._phase = "stopped"
                    break

                await self._sleep_interval()

            if self._phase not in ("error", "stopped"):
                self._phase = "done"
        except asyncio.CancelledError:
            logger.info("Kreuzstoss: task cancelled")
            self._phase = "stopped"
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Kreuzstoss: unexpected failure")
            self._error = f"{type(exc).__name__}: {exc}"
            self._phase = "error"
        finally:
            try:
                await self._restore_settings()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Kreuzstoss: restore failed")
                self._error = (self._error or "") + f" | restore failed: {exc}"
            self._running = False
            self._interval_remaining_s = None

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _set_step(self, idx: int, label: str, phase: str) -> None:
        self._step_index = idx
        self._current_step = label
        self._phase = phase
        logger.info("Kreuzstoss step %d: %s", idx, label)

    def _capture_initial_snapshots(self) -> None:
        workers = self._manager.all_workers()
        if len(workers) < 2:
            raise KreuzstossError(
                f"Kreuzstoss requires 2 cameras; found {len(workers)}"
            )
        self._snapshots = []
        for w in workers:
            cam_id = self._manager.get_cam_id(w)
            self._snapshots.append(CameraSnapshot(
                mxid=w.id,
                cam_id=cam_id,
                resolution=w._resolution,
                stream_fps=w._stream_fps,
                mjpeg_quality=w._mjpeg_quality,
            ))
        # Pre-flight: enough disk space?
        free = _free_gb(self._cfg.save_dir)
        if free is not None and free < DISK_FREE_FLOOR_GB:
            raise KreuzstossError(
                f"Free disk space {free:.2f} GB below floor "
                f"{DISK_FREE_FLOOR_GB:.1f} GB — refusing to start"
            )

    def _worker_for(self, cam_id: str) -> CameraWorker:
        w = self._manager.get_worker_by_cam_id(cam_id)
        if w is None:
            raise KreuzstossError(f"Camera {cam_id} not found")
        return w

    async def _apply_and_wait(
        self, worker: CameraWorker, cam_id: str, req: StreamSettingsRequest,
    ) -> None:
        logger.info(
            "Kreuzstoss: %s -> resolution=%s fps=%s quality=%s",
            cam_id, req.resolution, req.fps, req.mjpeg_quality,
        )
        await asyncio.to_thread(worker.update_stream_settings, req)
        ok = await asyncio.to_thread(worker.wait_for_ready, READY_TIMEOUT_S)
        if not ok:
            raise KreuzstossError(
                f"{cam_id} did not produce a frame within {READY_TIMEOUT_S:.0f}s"
            )
        # First-frame readiness is not stability — give the auto loops and
        # the encoder pipeline a real moment to settle so recordings aren't
        # black or under-exposed at the start.
        await self._sleep_or_stop(SETTLE_AFTER_REBUILD_S)

    async def _record_clip(
        self,
        cam_id: str,
        suffix: str,
        duration_s: float,
        fps: int,
    ) -> Path:
        worker = self._worker_for(cam_id)
        filename = (
            f"{self._cfg.prefix}_{cam_id}_{suffix}_{_iso_ms_ts()}.mp4"
        )
        meta_provider: MetadataProvider | None = None
        if self._metadata_factory is not None:
            try:
                meta_provider = self._metadata_factory(worker, cam_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Kreuzstoss: metadata factory failed: %s", exc)

        rec = VideoRecorder(
            camera_id=cam_id,
            fps=fps,
            output_dir=self._cfg.save_dir,
            filename_override=filename,
            metadata_provider=meta_provider,
        )
        path = await asyncio.to_thread(rec.start)
        worker.recording_worker = _RecordingShim(rec)
        worker._recording = True
        worker._recording_mode = RecordingMode.video
        try:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=duration_s
                )
            except asyncio.TimeoutError:
                pass
        finally:
            worker._recording = False
            worker._recording_mode = None
            worker.recording_worker = None
            await asyncio.to_thread(rec.stop)

        # Let the muxer flush and the encoder catch its breath before the
        # next pipeline change. Without this, transient frames around the
        # next rebuild can sneak into a still-finalising file.
        await self._sleep_or_stop(SETTLE_AFTER_RECORD_S)

        self._last_artifact = path.name
        self._artifacts_total += 1
        return path

    async def _take_snapshot(self, cam_id: str, suffix: str) -> Path:
        worker = self._worker_for(cam_id)
        # Make sure the latest frame has actually landed in the buffer.
        await self._sleep_or_stop(SETTLE_BEFORE_SNAPSHOT_S)
        data = await asyncio.to_thread(worker.capture_snapshot)
        filename = (
            f"{self._cfg.prefix}_{cam_id}_{suffix}_{_iso_ms_ts()}.jpg"
        )
        # VideoRecorder uses <save_dir>/<cam_id>/ — match that for snapshots
        # so all artifacts for one camera live together.
        out_dir = self._cfg.save_dir / cam_id
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / filename
        cleaned = _strip_jpeg_exif(data)
        await asyncio.to_thread(path.write_bytes, cleaned)
        await self._sleep_or_stop(SETTLE_AFTER_SNAPSHOT_S)
        self._last_artifact = path.name
        self._artifacts_total += 1
        return path

    async def _run_cycle(self) -> None:
        cam1 = self._worker_for("cam1")
        cam2 = self._worker_for("cam2")

        # MJPEG quality empirically tuned: the OAK-D's MJPEG quality knob is
        # near-flat in q=80..100 (q=85 produces ~7.3 MB per 4K frame, same
        # as q=100), so q=85 still saturates the GbE PoE link at 4K/29 fps.
        # The result is ~30 frames at 34ms (29 fps) interleaved with ~14
        # frames stretched to 200-450ms each — about 80% of playback time
        # the player is holding a still image. q=70 should drop 4K frame
        # size enough to fit 4K + idle-cam 1080p in 1 Gbps with headroom.
        # 1080p clips at q=85 are already smooth (no stuck frames) so they
        # stay where they are.
        target_4k = StreamSettingsRequest(
            fps=29, mjpeg_quality=70, resolution=RES_4K,
        )
        target_1080p_29 = StreamSettingsRequest(
            fps=29, mjpeg_quality=85, resolution=RES_1080P,
        )
        target_1080p_59 = StreamSettingsRequest(
            fps=59, mjpeg_quality=85, resolution=RES_1080P,
        )

        # ---- cam1 active phase ----
        # Step 2: drop cam2 to low bandwidth FIRST.
        self._set_step(2, "cam2 → 1080p / 29 fps (low bandwidth)", "cam1_active")
        await self._apply_and_wait(cam2, "cam2", target_1080p_29)
        if self._stop_event.is_set():
            return

        # Step 3: raise cam1 to 4K.
        self._set_step(3, "cam1 → 4K / 29 fps / q100", "cam1_active")
        await self._apply_and_wait(cam1, "cam1", target_4k)
        if self._stop_event.is_set():
            return

        # Step 4: record cam1 5 s.
        self._set_step(4, "Recording cam1 5 s @ 4K / 29 fps", "cam1_active")
        await self._record_clip("cam1", "4K_29fps", CLIP_4K_SECONDS, fps=29)
        if self._stop_event.is_set():
            return

        # Step 5: snapshot cam1.
        self._set_step(5, "Snapshot cam1 @ 4K / q100", "cam1_active")
        await self._take_snapshot("cam1", "4K_29fps")
        if self._stop_event.is_set():
            return

        # Step 6: cam1 → 1080p / 59 fps.
        self._set_step(6, "cam1 → 1080p / 59 fps / q100", "cam1_active")
        await self._apply_and_wait(cam1, "cam1", target_1080p_59)
        if self._stop_event.is_set():
            return

        # Step 7: record cam1 3 s.
        self._set_step(7, "Recording cam1 3 s @ 1080p / 59 fps", "cam1_active")
        await self._record_clip("cam1", "1080p_59fps", CLIP_1080P_SECONDS, fps=59)
        if self._stop_event.is_set():
            return

        # ---- cam2 active phase ----
        # Step 8: drop cam1 to low bandwidth (currently 1080p/59 → 1080p/29).
        self._set_step(8, "cam1 → 1080p / 29 fps (low bandwidth)", "cam2_active")
        await self._apply_and_wait(cam1, "cam1", target_1080p_29)
        if self._stop_event.is_set():
            return

        # Step 9: raise cam2 to 4K.
        self._set_step(9, "cam2 → 4K / 29 fps / q100", "cam2_active")
        await self._apply_and_wait(cam2, "cam2", target_4k)
        if self._stop_event.is_set():
            return

        # Step 10: record cam2 5 s.
        self._set_step(10, "Recording cam2 5 s @ 4K / 29 fps", "cam2_active")
        await self._record_clip("cam2", "4K_29fps", CLIP_4K_SECONDS, fps=29)
        if self._stop_event.is_set():
            return

        # Step 11: snapshot cam2.
        self._set_step(11, "Snapshot cam2 @ 4K / q100", "cam2_active")
        await self._take_snapshot("cam2", "4K_29fps")
        if self._stop_event.is_set():
            return

        # Step 12: cam2 → 1080p / 59 fps.
        self._set_step(12, "cam2 → 1080p / 59 fps / q100", "cam2_active")
        await self._apply_and_wait(cam2, "cam2", target_1080p_59)
        if self._stop_event.is_set():
            return

        # Step 13: record cam2 3 s.
        self._set_step(13, "Recording cam2 3 s @ 1080p / 59 fps", "cam2_active")
        await self._record_clip("cam2", "1080p_59fps", CLIP_1080P_SECONDS, fps=59)

    async def _run_cycle_simple(self) -> None:
        """Bandwidth-safe alternative: 1080p video + 4K snapshot per camera.

        4K MJPEG video at 29 fps requires ~1.7 Gbps and never fits in the
        single GbE PoE uplink — the encoder stalls and the result is a
        slideshow. Snapshots are a single frame, so 4K is fine for those.
        Each cycle for both cameras: raise to 1080p/59, record 5 s, raise
        to 4K, snapshot, drop back to idle.
        """
        cam1 = self._worker_for("cam1")
        cam2 = self._worker_for("cam2")

        target_4k = StreamSettingsRequest(
            fps=29, mjpeg_quality=85, resolution=RES_4K,
        )
        target_1080p_29 = StreamSettingsRequest(
            fps=29, mjpeg_quality=85, resolution=RES_1080P,
        )
        target_1080p_59 = StreamSettingsRequest(
            fps=59, mjpeg_quality=85, resolution=RES_1080P,
        )

        # ---- cam1 active phase ----
        self._set_step(2, "cam2 → 1080p / 29 fps (low bandwidth)", "cam1_active")
        await self._apply_and_wait(cam2, "cam2", target_1080p_29)
        if self._stop_event.is_set():
            return

        self._set_step(3, "cam1 → 1080p / 59 fps", "cam1_active")
        await self._apply_and_wait(cam1, "cam1", target_1080p_59)
        if self._stop_event.is_set():
            return

        self._set_step(4, "Recording cam1 5 s @ 1080p / 59 fps", "cam1_active")
        await self._record_clip("cam1", "1080p_59fps", CLIP_1080P_SECONDS, fps=59)
        if self._stop_event.is_set():
            return

        self._set_step(5, "cam1 → 4K (for snapshot)", "cam1_active")
        await self._apply_and_wait(cam1, "cam1", target_4k)
        if self._stop_event.is_set():
            return

        self._set_step(6, "Snapshot cam1 @ 4K", "cam1_active")
        await self._take_snapshot("cam1", "4K_29fps")
        if self._stop_event.is_set():
            return

        # ---- cam2 active phase ----
        self._set_step(7, "cam1 → 1080p / 29 fps (low bandwidth)", "cam2_active")
        await self._apply_and_wait(cam1, "cam1", target_1080p_29)
        if self._stop_event.is_set():
            return

        self._set_step(8, "cam2 → 1080p / 59 fps", "cam2_active")
        await self._apply_and_wait(cam2, "cam2", target_1080p_59)
        if self._stop_event.is_set():
            return

        self._set_step(9, "Recording cam2 5 s @ 1080p / 59 fps", "cam2_active")
        await self._record_clip("cam2", "1080p_59fps", CLIP_1080P_SECONDS, fps=59)
        if self._stop_event.is_set():
            return

        self._set_step(10, "cam2 → 4K (for snapshot)", "cam2_active")
        await self._apply_and_wait(cam2, "cam2", target_4k)
        if self._stop_event.is_set():
            return

        self._set_step(11, "Snapshot cam2 @ 4K", "cam2_active")
        await self._take_snapshot("cam2", "4K_29fps")

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep for `seconds`, returning early if stop is requested."""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _sleep_interval(self) -> None:
        wait_step = (
            TOTAL_STEPS_PER_CYCLE_SIMPLE
            if self._cfg.mode == "simple"
            else TOTAL_STEPS_PER_CYCLE
        )
        self._set_step(wait_step, "Waiting for next cycle", "interval")
        deadline = asyncio.get_event_loop().time() + self._cfg.interval_seconds
        try:
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                self._interval_remaining_s = remaining
                tick = min(0.5, remaining)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=tick)
                    return  # stop requested
                except asyncio.TimeoutError:
                    continue
        finally:
            self._interval_remaining_s = None

    async def _restore_settings(self) -> None:
        if not self._snapshots:
            return
        self._phase = "restoring"
        self._current_step = "Restoring pre-program settings"
        # Lower-resolution camera first to keep PoE headroom while restoring.
        ordered = sorted(self._snapshots, key=lambda s: _resolution_rank(s.resolution))
        for snap in ordered:
            try:
                worker = self._manager.get_worker(snap.mxid)
            except KeyError:
                logger.warning(
                    "Kreuzstoss restore: worker %s no longer present", snap.mxid,
                )
                continue
            req = StreamSettingsRequest(
                fps=snap.stream_fps,
                mjpeg_quality=snap.mjpeg_quality,
                resolution=snap.resolution,
            )
            try:
                await asyncio.to_thread(worker.update_stream_settings, req)
                await asyncio.to_thread(worker.wait_for_ready, READY_TIMEOUT_S)
                logger.info(
                    "Kreuzstoss restore: %s -> %s/%dfps/q%d",
                    snap.cam_id, snap.resolution,
                    snap.stream_fps, snap.mjpeg_quality,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Kreuzstoss restore: %s failed: %s", snap.cam_id, exc,
                )


def _resolution_rank(res: str) -> int:
    return {"4k": 4, "1080p": 3, "720p": 2, "480p": 1}.get(res.lower(), 0)

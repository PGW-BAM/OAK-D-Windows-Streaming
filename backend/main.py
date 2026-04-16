"""FastAPI application — multi-camera OAK-D 4 Pro streaming dashboard."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .calibration import (
    CalibrationManager,
    CalibrationPoint,
    CalibrationSettings,
)
from .angle_targets import AngleTargetManager
from .camera_manager import CameraManager
from .config import settings
from .models import (
    AngleTargetResponse,
    ApiResponse,
    BandwidthCheckRequest,
    CalibrationAutoApplyRequest,
    CalibrationInterpolateFocusRequest,
    CalibrationPointResponse,
    CalibrationProfileResponse,
    CameraControlRequest,
    CameraListResponse,
    CaptureAngleTargetRequest,
    Detection,
    InferenceModeRequest,
    RecordingStartRequest,
    SaveCalibrationPointRequest,
    StorageStatus,
    StreamSettingsRequest,
)
from .bandwidth import (
    BandwidthEstimate,
    BandwidthMatrix,
    build_bandwidth_matrix,
    check_feasibility,
)
from .recording import (
    MetadataProvider,
    RecordingMetadata,
    RecordingWorker,
    cleanup_old_recordings,
    get_storage_stats,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

camera_manager = CameraManager()
recording_workers: dict[str, RecordingWorker] = {}

# Calibration store (loaded at startup)
CALIBRATION_PATH = Path(__file__).parent.parent / "config" / "calibration.json"
calibration_manager = CalibrationManager(CALIBRATION_PATH)

# Radial-angle teach targets (loaded at startup)
ANGLE_TARGETS_PATH = Path(__file__).parent.parent / "config" / "angle_targets.json"
angle_target_manager = AngleTargetManager(ANGLE_TARGETS_PATH)

# MQTT service (lazy — only started if broker is configured)
mqtt_service = None

# Background task handle for calibration auto-apply loop
_calibration_task: asyncio.Task | None = None


async def _calibration_auto_apply_loop() -> None:
    """Per-camera auto-apply with continuous focus interpolation.

    - When the IMU angle is within tolerance of a calibration point, the full
      settings bundle from the nearest point is applied once per point entry
      (debounced by point index).
    - Between points, `manual_focus` is continuously interpolated (IDW over
      the 3 nearest points) and sent as a focus-only CameraControl whenever
      the interpolated value changes by >=1 LSB. Exposure/WB/etc. keep
      snap-to-nearest semantics.
    - If the nearest point has auto_focus=True, or interpolation is disabled
      for that camera, behaviour collapses back to the original snap-only mode.

    Exceptions on one camera do not stop the loop.
    """
    last_applied_idx: dict[str, int] = {}
    last_focus_sent: dict[str, int] = {}
    try:
        while True:
            await asyncio.sleep(0.5)
            for worker in camera_manager.all_workers():
                try:
                    cal = calibration_manager.get_camera(worker.id)
                    if not cal.auto_apply:
                        # Reset so re-enabling auto-apply reapplies the nearest point
                        last_applied_idx.pop(worker.id, None)
                        last_focus_sent.pop(worker.id, None)
                        continue
                    angle = worker.get_imu_angle()
                    if angle is None:
                        continue
                    result = calibration_manager.interpolate_focus(
                        worker.id, angle[0], angle[1]
                    )
                    if result is None:
                        continue
                    idx, point, focus = result

                    bundle_changed = last_applied_idx.get(worker.id) != idx
                    if bundle_changed:
                        # Apply full settings bundle once on entry to this point.
                        # If the camera is using interpolated focus (i.e. the
                        # user disabled auto_focus and `interpolate_focus` is on
                        # with >=2 points), override the bundle's focus fields
                        # with the interpolated value so autofocus doesn't
                        # re-engage on every point crossing.
                        settings_dump = point.settings.model_dump()
                        if not point.settings.auto_focus and cal.interpolate_focus and len(cal.points) >= 2:
                            settings_dump["auto_focus"] = False
                            settings_dump["manual_focus"] = focus
                        ctrl = CameraControlRequest(**settings_dump)
                        worker.apply_control(ctrl)
                        last_applied_idx[worker.id] = idx
                        last_focus_sent[worker.id] = focus
                        logger.info(
                            "Auto-applied calibration point #%d (%s) to camera %s",
                            idx, point.label or "unlabeled", worker.id[:8],
                        )
                        continue

                    # Same bundle — check if interpolated focus moved
                    prev_focus = last_focus_sent.get(worker.id)
                    if prev_focus is None or abs(focus - prev_focus) >= 1:
                        ctrl = CameraControlRequest(
                            auto_focus=False,
                            manual_focus=focus,
                        )
                        worker.apply_control(ctrl)
                        last_focus_sent[worker.id] = focus
                        logger.debug(
                            "focus interp: cam=%s roll=%.2f pitch=%.2f -> focus=%d (near #%d)",
                            worker.id[:8], angle[0], angle[1], focus, idx,
                        )
                except Exception as exc:
                    logger.debug("calibration auto-apply error on %s: %s", worker.id[:8], exc)
    except asyncio.CancelledError:
        return


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global mqtt_service, _calibration_task

    logger.info("Starting camera discovery …")
    ids = camera_manager.discover()
    logger.info("Found %d camera(s) at startup: %s", len(ids), ids)

    # Load persisted calibration profiles
    calibration_manager.load()
    angle_target_manager.load()

    # Start MQTT service (non-blocking, auto-reconnects in background)
    try:
        from .mqtt.service import MqttService
        mqtt_service = MqttService(camera_manager, angle_target_manager)
        await mqtt_service.start()
    except Exception as exc:
        logger.warning("MQTT service failed to start (standalone mode): %s", exc)
        mqtt_service = None

    # Start calibration auto-apply background loop
    _calibration_task = asyncio.create_task(_calibration_auto_apply_loop())

    yield

    # Stop calibration loop
    if _calibration_task:
        _calibration_task.cancel()
        try:
            await _calibration_task
        except asyncio.CancelledError:
            pass

    # Shutdown MQTT
    if mqtt_service:
        try:
            await mqtt_service.stop()
        except Exception as exc:
            logger.warning("MQTT shutdown error: %s", exc)

    logger.info("Shutting down camera manager …")
    camera_manager.shutdown()


app = FastAPI(
    title="OAK-D Streaming Dashboard",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Serve React frontend (production build)
# ---------------------------------------------------------------------------

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


@app.get("/", include_in_schema=False)
async def root() -> HTMLResponse:
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse(
        "<h1>OAK-D Dashboard backend running</h1>"
        "<p>No frontend build found. Run <code>npm run build</code> in ./frontend</p>"
    )


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


# ---------------------------------------------------------------------------
# Camera list
# ---------------------------------------------------------------------------

@app.get("/api/cameras", response_model=CameraListResponse)
async def list_cameras() -> CameraListResponse:
    statuses = camera_manager.all_statuses()
    return CameraListResponse(cameras=statuses, total=len(statuses))


@app.post("/api/cameras/discover", response_model=ApiResponse)
async def discover_cameras() -> ApiResponse:
    ids = camera_manager.discover()
    return ApiResponse(ok=True, message=f"Found {len(ids)} camera(s)", data=ids)


# ---------------------------------------------------------------------------
# Camera enable / disable (free bandwidth)
# ---------------------------------------------------------------------------

@app.post("/api/camera/{camera_id}/enable", response_model=ApiResponse)
async def enable_camera(camera_id: str) -> ApiResponse:
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")

    # Wait for any other camera that is currently in the process of stopping
    # (disabled but whose worker thread hasn't exited yet) to fully release its
    # PoE device before we open a new pipeline.  Without this, switching from a
    # high-bandwidth camera (e.g. 4K+60fps ≈ 87% PoE) to another while the
    # first is still winding down pushes combined bandwidth over 100%.
    stopping = [
        w for w in camera_manager.all_workers()
        if w.id != camera_id and not w._enabled
        and w._thread is not None and w._thread.is_alive()
    ]
    if stopping:
        def _drain() -> None:
            for w in stopping:
                if w._thread:
                    w._thread.join(timeout=30)
        await asyncio.to_thread(_drain)

    worker.set_enabled(True)
    return ApiResponse(ok=True, message="Camera enabled")


@app.post("/api/camera/{camera_id}/disable", response_model=ApiResponse)
async def disable_camera(camera_id: str) -> ApiResponse:
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    # set_enabled(False) calls thread.join() — offload so the event loop stays live
    await asyncio.to_thread(worker.set_enabled, False)
    return ApiResponse(ok=True, message="Camera disabled — bandwidth freed")


# ---------------------------------------------------------------------------
# MJPEG stream
# ---------------------------------------------------------------------------

BOUNDARY = b"--frame"
CRLF = b"\r\n"


async def _mjpeg_generator(camera_id: str) -> AsyncGenerator[bytes, None]:
    worker = camera_manager.get_worker(camera_id)
    last_count = -1
    while True:
        frame_bytes, _ = worker.frame_buffer.get()
        count = worker.frame_buffer.frame_count
        if frame_bytes and count != last_count:
            last_count = count
            yield (
                BOUNDARY + CRLF
                + b"Content-Type: image/jpeg" + CRLF
                + b"Content-Length: " + str(len(frame_bytes)).encode() + CRLF
                + CRLF
                + frame_bytes + CRLF
            )
        await asyncio.sleep(1 / worker._stream_fps)


@app.get("/api/camera/{camera_id}/stream")
async def camera_stream(camera_id: str) -> StreamingResponse:
    try:
        camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    return StreamingResponse(
        _mjpeg_generator(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

@app.get("/api/camera/{camera_id}/snapshot")
async def camera_snapshot(camera_id: str) -> Response:
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    try:
        data = worker.capture_snapshot()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    return Response(content=data, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Stereo snapshot
# ---------------------------------------------------------------------------

@app.get("/api/camera/{camera_id}/stereo/{side}/snapshot")
async def stereo_snapshot(camera_id: str, side: str) -> Response:
    if side not in ("left", "right"):
        raise HTTPException(422, "side must be 'left' or 'right'")
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    try:
        data = worker.capture_stereo_snapshot(side)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    return Response(content=data, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Camera control (single + bulk)
# ---------------------------------------------------------------------------

@app.post("/api/camera/{camera_id}/control", response_model=ApiResponse)
async def camera_control(camera_id: str, req: CameraControlRequest) -> ApiResponse:
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    try:
        worker.apply_control(req)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return ApiResponse(ok=True, message="Control applied")


@app.post("/api/cameras/control", response_model=ApiResponse)
async def camera_control_all(req: CameraControlRequest) -> ApiResponse:
    """Apply camera control settings to ALL cameras."""
    errors = []
    for worker in camera_manager.all_workers():
        try:
            worker.apply_control(req)
        except Exception as exc:
            errors.append(f"{worker.id[:8]}: {exc}")
    if errors:
        return ApiResponse(ok=False, message=f"Partial failure: {'; '.join(errors)}")
    return ApiResponse(ok=True, message="Control applied to all cameras")


# ---------------------------------------------------------------------------
# Stream settings (single + bulk)
# ---------------------------------------------------------------------------

@app.post("/api/camera/{camera_id}/stream-settings", response_model=ApiResponse)
async def update_stream_settings(camera_id: str, req: StreamSettingsRequest) -> ApiResponse:
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    try:
        # update_stream_settings calls stop() → thread.join() — offload so the
        # event loop keeps serving the concurrent camera's MJPEG stream
        await asyncio.to_thread(worker.update_stream_settings, req)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return ApiResponse(ok=True, message="Stream settings updated")


@app.post("/api/cameras/stream-settings", response_model=ApiResponse)
async def update_stream_settings_all(req: StreamSettingsRequest) -> ApiResponse:
    """Apply stream settings to ALL cameras (triggers pipeline rebuild)."""
    errors = []
    for worker in camera_manager.all_workers():
        try:
            await asyncio.to_thread(worker.update_stream_settings, req)
        except Exception as exc:
            errors.append(f"{worker.id[:8]}: {exc}")
    if errors:
        return ApiResponse(ok=False, message=f"Partial failure: {'; '.join(errors)}")
    return ApiResponse(ok=True, message="Stream settings applied to all cameras")


# ---------------------------------------------------------------------------
# Recording (single + bulk)
# ---------------------------------------------------------------------------

def _get_or_create_recording_worker(camera_id: str) -> RecordingWorker:
    if camera_id not in recording_workers:
        recording_workers[camera_id] = RecordingWorker(camera_id)
    return recording_workers[camera_id]


def _cam_id_for_worker(worker) -> str:
    """Map a CameraWorker to its logical cam_id (cam1, cam2, ...) by discovery order."""
    workers = camera_manager.all_workers()
    idx = next((i for i, w in enumerate(workers) if w.id == worker.id), 0)
    return f"cam{idx + 1}"


def _make_metadata_provider(worker, cam_id: str) -> MetadataProvider:
    """Return a zero-argument callable that snapshots IMU angles + drive positions."""
    def provider() -> RecordingMetadata:
        imu = worker.get_imu_angle()
        drives: dict[str, float | None] = {}
        if mqtt_service is not None:
            try:
                drives = mqtt_service.get_drive_positions(cam_id)
            except Exception:
                pass
        return RecordingMetadata(
            cam_id=cam_id,
            timestamp=datetime.now(timezone.utc),
            roll_deg=imu[0] if imu else None,
            pitch_deg=imu[1] if imu else None,
            drive_a=drives.get("drive_a"),
            drive_b=drives.get("drive_b"),
        )
    return provider


@app.post("/api/camera/{camera_id}/recording/start", response_model=ApiResponse)
async def start_recording(camera_id: str, req: RecordingStartRequest) -> ApiResponse:
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")

    rec_worker = _get_or_create_recording_worker(camera_id)
    if rec_worker.active:
        raise HTTPException(409, "Recording already active")

    # Resolve custom output directory
    custom_dir = Path(req.output_dir) if req.output_dir else None
    if custom_dir and not custom_dir.is_absolute():
        custom_dir = Path.cwd() / custom_dir

    # Determine if stereo capture is needed
    from .models import StereoMode
    stereo_capture = worker._stereo_mode in (StereoMode.stereo_only, StereoMode.both)

    cam_id = _cam_id_for_worker(worker)
    metadata_provider = _make_metadata_provider(worker, cam_id)

    # Attach recording worker to camera worker
    worker.recording_worker = rec_worker
    worker._recording = True
    worker._recording_mode = req.mode

    try:
        # rec_worker.start() calls stop() first (potentially closing a container)
        # then opens a new AV container — offload so the event loop stays live
        output_path = await asyncio.to_thread(
            rec_worker.start,
            req.mode,
            req.interval_seconds,
            custom_dir,
            req.filename_prefix,
            stereo_capture,
            worker._stream_fps,
            metadata_provider,
            req.clip_duration_seconds,
            req.clip_interval_seconds,
        )
    except Exception as exc:
        # Roll back recording state so the camera worker doesn't feed a broken recorder
        worker._recording = False
        worker._recording_mode = None
        worker.recording_worker = None
        raise HTTPException(500, f"Failed to start recording: {exc}") from exc
    return ApiResponse(ok=True, message="Recording started", data=str(output_path))


@app.post("/api/camera/{camera_id}/recording/stop", response_model=ApiResponse)
async def stop_recording(camera_id: str) -> ApiResponse:
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")

    rec_worker = recording_workers.get(camera_id)
    if not rec_worker or not rec_worker.active:
        raise HTTPException(409, "No active recording")

    # Stop feeding new frames into the recorder immediately
    worker._recording = False
    worker._recording_mode = None
    worker.recording_worker = None
    # Drain the mux queue and close the container — can be slow at high fps,
    # offload so the event loop keeps serving the live MJPEG stream
    await asyncio.to_thread(rec_worker.stop)
    # Disk scan — also synchronous, keep off the event loop
    await asyncio.to_thread(cleanup_old_recordings)
    return ApiResponse(ok=True, message="Recording stopped")


@app.post("/api/cameras/recording/start", response_model=ApiResponse)
async def start_recording_all(req: RecordingStartRequest) -> ApiResponse:
    """Start recording on ALL cameras."""
    from .models import StereoMode
    started = []
    errors = []
    for worker in camera_manager.all_workers():
        try:
            rec_worker = _get_or_create_recording_worker(worker.id)
            if rec_worker.active:
                continue  # skip already recording

            custom_dir = Path(req.output_dir) if req.output_dir else None
            if custom_dir and not custom_dir.is_absolute():
                custom_dir = Path.cwd() / custom_dir

            stereo_capture = worker._stereo_mode in (StereoMode.stereo_only, StereoMode.both)

            cam_id = _cam_id_for_worker(worker)
            metadata_provider = _make_metadata_provider(worker, cam_id)

            worker.recording_worker = rec_worker
            worker._recording = True
            worker._recording_mode = req.mode

            try:
                await asyncio.to_thread(
                    rec_worker.start,
                    req.mode,
                    req.interval_seconds,
                    custom_dir,
                    req.filename_prefix,
                    stereo_capture,
                    worker._stream_fps,
                    metadata_provider,
                    req.clip_duration_seconds,
                    req.clip_interval_seconds,
                )
            except Exception as exc:
                worker._recording = False
                worker._recording_mode = None
                worker.recording_worker = None
                raise
            started.append(worker.id[:8])
        except Exception as exc:
            errors.append(f"{worker.id[:8]}: {exc}")

    msg = f"Recording started on {len(started)} camera(s)"
    if errors:
        msg += f"; errors: {'; '.join(errors)}"
    return ApiResponse(ok=len(errors) == 0, message=msg, data=started)


@app.post("/api/cameras/recording/stop", response_model=ApiResponse)
async def stop_recording_all() -> ApiResponse:
    """Stop recording on ALL cameras."""
    stopped = []
    for worker in camera_manager.all_workers():
        rec_worker = recording_workers.get(worker.id)
        if rec_worker and rec_worker.active:
            worker._recording = False
            worker._recording_mode = None
            worker.recording_worker = None
            await asyncio.to_thread(rec_worker.stop)
            stopped.append(worker.id[:8])

    await asyncio.to_thread(cleanup_old_recordings)
    return ApiResponse(ok=True, message=f"Stopped {len(stopped)} recording(s)", data=stopped)


@app.get("/api/camera/{camera_id}/recording/status", response_model=ApiResponse)
async def recording_status(camera_id: str) -> ApiResponse:
    rec_worker = recording_workers.get(camera_id)
    active = rec_worker.active if rec_worker else False
    mode = rec_worker.mode if rec_worker else None
    return ApiResponse(ok=True, message="ok", data={"active": active, "mode": mode})


# ---------------------------------------------------------------------------
# Inference mode
# ---------------------------------------------------------------------------

@app.post("/api/camera/{camera_id}/inference/mode", response_model=ApiResponse)
async def set_inference_mode(camera_id: str, req: InferenceModeRequest) -> ApiResponse:
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    try:
        # on_camera mode triggers a pipeline rebuild (stop+start) — offload to thread
        await asyncio.to_thread(worker.set_inference_mode, req.mode, req.model_path)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    return ApiResponse(ok=True, message=f"Inference mode set to {req.mode}")


@app.get("/api/camera/{camera_id}/detections", response_model=Detection | None)
async def get_detections(camera_id: str) -> Detection | None:
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    return worker.detection_buffer.get()


@app.get("/api/camera/{camera_id}/imu")
async def get_imu(camera_id: str) -> dict:
    """Return the latest IMU angles (roll + pitch in degrees) for the given camera."""
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    angle = worker.get_imu_angle()
    if angle is None:
        return {"has_data": False, "roll_deg": 0.0, "pitch_deg": 0.0}
    return {"has_data": True, "roll_deg": round(angle[0], 2), "pitch_deg": round(angle[1], 2)}


# ---------------------------------------------------------------------------
# Calibration (IMU angle → camera settings)
# ---------------------------------------------------------------------------

def _profile_response(camera_id: str) -> CalibrationProfileResponse:
    cal = calibration_manager.get_camera(camera_id)
    points = [
        CalibrationPointResponse(
            index=i,
            label=p.label,
            roll_deg=p.roll_deg,
            pitch_deg=p.pitch_deg,
            settings=p.settings.model_dump(),
            created_at=p.created_at.isoformat(),
        )
        for i, p in enumerate(cal.points)
    ]
    return CalibrationProfileResponse(
        camera_id=camera_id,
        auto_apply=cal.auto_apply,
        tolerance_deg=cal.tolerance_deg,
        interpolate_focus=cal.interpolate_focus,
        points=points,
    )


@app.get("/api/camera/{camera_id}/calibration", response_model=CalibrationProfileResponse)
async def get_calibration(camera_id: str) -> CalibrationProfileResponse:
    try:
        camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    return _profile_response(camera_id)


@app.post("/api/camera/{camera_id}/calibration/point", response_model=ApiResponse)
async def save_calibration_point(
    camera_id: str, req: SaveCalibrationPointRequest
) -> ApiResponse:
    """Capture current IMU angle + provided settings as a new calibration point."""
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")

    angle = worker.get_imu_angle()
    if angle is None:
        raise HTTPException(503, "No IMU data available — cannot capture calibration point")

    # Drop None fields from the control request so unset values don't overwrite defaults
    settings_dict = req.settings.model_dump(exclude_none=True)
    cal_settings = CalibrationSettings(**settings_dict)

    point = CalibrationPoint(
        label=req.label,
        roll_deg=round(angle[0], 2),
        pitch_deg=round(angle[1], 2),
        settings=cal_settings,
    )
    idx = calibration_manager.add_point(camera_id, point)
    try:
        calibration_manager.save()
    except Exception as exc:
        raise HTTPException(500, f"Failed to persist calibration: {exc}")
    return ApiResponse(
        ok=True,
        message=f"Saved calibration point {idx} at roll={point.roll_deg}° pitch={point.pitch_deg}°",
        data={"index": idx, "roll_deg": point.roll_deg, "pitch_deg": point.pitch_deg},
    )


@app.delete("/api/camera/{camera_id}/calibration/point/{index}", response_model=ApiResponse)
async def delete_calibration_point(camera_id: str, index: int) -> ApiResponse:
    try:
        camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    try:
        calibration_manager.delete_point(camera_id, index)
    except IndexError as exc:
        raise HTTPException(404, str(exc))
    calibration_manager.save()
    return ApiResponse(ok=True, message=f"Deleted calibration point {index}")


@app.post("/api/camera/{camera_id}/calibration/auto-apply", response_model=ApiResponse)
async def set_calibration_auto_apply(
    camera_id: str, req: CalibrationAutoApplyRequest
) -> ApiResponse:
    try:
        camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    calibration_manager.set_auto_apply(camera_id, req.enabled, req.tolerance_deg)
    calibration_manager.save()
    return ApiResponse(
        ok=True,
        message=f"Auto-apply {'enabled' if req.enabled else 'disabled'}",
    )


@app.post(
    "/api/camera/{camera_id}/calibration/interpolate-focus",
    response_model=ApiResponse,
)
async def set_calibration_interpolate_focus(
    camera_id: str, req: CalibrationInterpolateFocusRequest
) -> ApiResponse:
    try:
        camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    calibration_manager.set_interpolate_focus(camera_id, req.enabled)
    calibration_manager.save()
    return ApiResponse(
        ok=True,
        message=f"Focus interpolation {'enabled' if req.enabled else 'disabled'}",
    )


@app.post("/api/camera/{camera_id}/calibration/apply-nearest", response_model=ApiResponse)
async def apply_nearest_calibration(camera_id: str) -> ApiResponse:
    """One-shot: read current IMU, find the nearest saved point, apply its settings."""
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {camera_id!r} not found")
    angle = worker.get_imu_angle()
    if angle is None:
        raise HTTPException(503, "No IMU data available")
    match = calibration_manager.find_nearest(camera_id, angle[0], angle[1])
    if match is None:
        return ApiResponse(
            ok=False,
            message="No calibration point within tolerance of current angle",
        )
    idx, point = match
    ctrl = CameraControlRequest(**point.settings.model_dump())
    try:
        worker.apply_control(ctrl)
    except Exception as exc:
        raise HTTPException(500, f"Failed to apply control: {exc}")
    return ApiResponse(
        ok=True,
        message=f"Applied calibration point #{idx}"
                + (f" ({point.label})" if point.label else ""),
        data={"index": idx, "roll_deg": point.roll_deg, "pitch_deg": point.pitch_deg},
    )


# ---------------------------------------------------------------------------
# Radial-angle teach targets (closed-loop drive correction)
# ---------------------------------------------------------------------------

def _angle_target_to_response(name: str, target: Any) -> AngleTargetResponse:
    return AngleTargetResponse(
        checkpoint_name=name,
        axis=target.axis,
        active_angle=target.active_angle,
        target_angle_deg=target.target_angle_deg,
        motor_position=target.motor_position,
        label=target.label,
        created_at=target.created_at.isoformat(),
    )


@app.get("/api/angle_targets")
async def list_angle_targets() -> dict[str, list[AngleTargetResponse]]:
    """All stored radial-angle teach targets grouped by cam_id."""
    store = angle_target_manager.list_all()
    return {
        cam_id: [_angle_target_to_response(name, t) for name, t in targets.items()]
        for cam_id, targets in store.items()
    }


@app.get("/api/angle_targets/{cam_id}")
async def list_angle_targets_for_camera(cam_id: str) -> list[AngleTargetResponse]:
    targets = angle_target_manager.list_camera(cam_id)
    return [_angle_target_to_response(name, t) for name, t in targets.items()]


@app.post("/api/angle_targets/capture", response_model=AngleTargetResponse)
async def capture_angle_target(req: CaptureAngleTargetRequest) -> AngleTargetResponse:
    """Snapshot current IMU angle + axis-b motor position for a checkpoint."""
    try:
        worker = camera_manager.get_worker(req.camera_id)
    except KeyError:
        raise HTTPException(404, f"Camera {req.camera_id!r} not found")

    angle = worker.get_imu_angle()
    if angle is None:
        raise HTTPException(503, "No IMU data available — cannot teach angle target")

    if mqtt_service is None:
        raise HTTPException(503, "MQTT service not running — cannot read drive position")

    positions = mqtt_service.get_drive_positions(req.cam_id)
    motor_position = positions.get("b")
    if motor_position is None:
        raise HTTPException(
            503,
            f"No axis-b drive position known for {req.cam_id} yet — move the drive once first",
        )

    target = angle_target_manager.capture(
        req.cam_id,
        req.checkpoint_name,
        active_angle=req.active_angle,
        current_imu_roll_deg=angle[0],
        current_imu_pitch_deg=angle[1],
        motor_position=float(motor_position),
        axis="b",
        label=req.label,
    )
    try:
        angle_target_manager.save()
    except Exception as exc:
        raise HTTPException(500, f"Failed to persist angle targets: {exc}")

    return _angle_target_to_response(req.checkpoint_name, target)


@app.delete("/api/angle_targets/{cam_id}/{checkpoint_name}", response_model=ApiResponse)
async def delete_angle_target(cam_id: str, checkpoint_name: str) -> ApiResponse:
    if not angle_target_manager.delete(cam_id, checkpoint_name):
        raise HTTPException(404, f"No angle target {checkpoint_name!r} for {cam_id!r}")
    angle_target_manager.save()
    return ApiResponse(ok=True, message=f"Deleted angle target {checkpoint_name!r}")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

@app.get("/api/storage", response_model=StorageStatus)
async def storage_status() -> StorageStatus:
    return StorageStatus(**get_storage_stats())


@app.post("/api/storage/cleanup", response_model=ApiResponse)
async def trigger_cleanup() -> ApiResponse:
    deleted = cleanup_old_recordings()
    return ApiResponse(ok=True, message=f"Deleted {deleted} file(s)")


@app.get("/api/settings/recordings-dir", response_model=ApiResponse)
async def get_recordings_dir() -> ApiResponse:
    return ApiResponse(
        ok=True,
        message="ok",
        data=str(settings.recordings_dir.resolve()),
    )


@app.post("/api/settings/recordings-dir", response_model=ApiResponse)
async def set_recordings_dir(body: dict) -> ApiResponse:
    new_dir = body.get("path", "").strip()
    if not new_dir:
        raise HTTPException(422, "path is required")
    p = Path(new_dir)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"Cannot create directory: {exc}")
    settings.recordings_dir = p
    return ApiResponse(ok=True, message=f"Recordings directory set to {p}")


# ---------------------------------------------------------------------------
# Last session (persist last-used UI/camera settings across restarts)
# ---------------------------------------------------------------------------

from .session_store import load_last_session, save_last_session


@app.get("/api/session/last", response_model=ApiResponse)
async def get_last_session() -> ApiResponse:
    data = load_last_session()
    return ApiResponse(
        ok=True,
        message="Last session loaded" if data else "No previous session",
        data=data,
    )


@app.post("/api/session/save", response_model=ApiResponse)
async def save_session(payload: dict) -> ApiResponse:
    try:
        save_last_session(payload)
    except OSError as exc:
        raise HTTPException(500, f"Cannot save session: {exc}")
    return ApiResponse(ok=True, message="Session saved")


# ---------------------------------------------------------------------------
# Bandwidth estimation
# ---------------------------------------------------------------------------

@app.get("/api/bandwidth/matrix", response_model=BandwidthMatrix)
async def bandwidth_matrix() -> BandwidthMatrix:
    """Return the full bandwidth matrix (max FPS for each combination)."""
    return build_bandwidth_matrix()


@app.post("/api/bandwidth/check", response_model=BandwidthEstimate)
async def bandwidth_check(req: BandwidthCheckRequest) -> BandwidthEstimate:
    """Check if a specific configuration fits within PoE bandwidth."""
    return check_feasibility(
        req.resolution, req.quality, req.fps, req.num_cameras, req.stereo_mode
    )


# ---------------------------------------------------------------------------
# WebSocket binary stream
# ---------------------------------------------------------------------------

@app.websocket("/ws/camera/{camera_id}")
async def ws_camera(websocket: WebSocket, camera_id: str) -> None:
    try:
        worker = camera_manager.get_worker(camera_id)
    except KeyError:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    logger.info("WebSocket connected for camera %s", camera_id)
    last_count = -1
    try:
        while True:
            frame_bytes, _ = worker.frame_buffer.get()
            count = worker.frame_buffer.frame_count
            if frame_bytes and count != last_count:
                last_count = count
                await websocket.send_bytes(frame_bytes)
            await asyncio.sleep(1 / worker._stream_fps)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for camera %s", camera_id)
    except Exception as exc:
        logger.warning("WebSocket error for camera %s: %s", camera_id, exc)


# ---------------------------------------------------------------------------
# MQTT status & control
# ---------------------------------------------------------------------------

@app.get("/api/mqtt/status", response_model=ApiResponse)
async def mqtt_status() -> ApiResponse:
    """Return MQTT connection state and component health."""
    if not mqtt_service:
        return ApiResponse(ok=True, message="MQTT not active (standalone mode)", data={
            "connected": False,
            "mode": "standalone",
        })
    state = mqtt_service.monitor.get_state()
    orch = mqtt_service.orchestrator
    return ApiResponse(ok=True, message="ok", data={
        "connected": mqtt_service.is_connected,
        "mode": "mqtt",
        "pi_online": state.pi_online,
        "broker_connected": state.broker_connected,
        "cameras": state.cameras,
        "drives": state.drives,
        "orchestrator_state": orch.state.value,
        "active_sequence": orch.active_sequence.name if orch.active_sequence else None,
        "sequence_progress": orch.progress,
        "total_captures": orch.total_captures,
    })


@app.get("/api/mqtt/connectivity", response_model=ApiResponse)
async def mqtt_connectivity() -> ApiResponse:
    """Return detailed connectivity state for all components."""
    if not mqtt_service:
        return ApiResponse(ok=False, message="MQTT not active")
    state = mqtt_service.monitor.get_state()
    return ApiResponse(ok=True, message="ok", data=state.model_dump(mode="json"))


@app.post("/api/mqtt/sequence/start", response_model=ApiResponse)
async def start_sequence(body: dict) -> ApiResponse:
    """Start a capture sequence from a YAML file or inline definition."""
    if not mqtt_service:
        raise HTTPException(503, "MQTT service not active")

    file_path = body.get("file")
    if file_path:
        try:
            seq = await mqtt_service.orchestrator.load_sequence_file(file_path)
        except FileNotFoundError:
            raise HTTPException(404, f"Sequence file not found: {file_path}")
        except Exception as exc:
            raise HTTPException(422, f"Invalid sequence file: {exc}")
    else:
        from .mqtt.models import CaptureSequence
        try:
            seq = CaptureSequence(**body)
        except Exception as exc:
            raise HTTPException(422, f"Invalid sequence definition: {exc}")

    try:
        await mqtt_service.orchestrator.start_sequence(seq)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return ApiResponse(ok=True, message=f"Sequence '{seq.name}' started", data={
        "sequence_id": seq.sequence_id,
        "steps": len(seq.steps),
        "repeats": seq.repeat_count,
    })


@app.post("/api/mqtt/sequence/stop", response_model=ApiResponse)
async def stop_sequence() -> ApiResponse:
    if not mqtt_service:
        raise HTTPException(503, "MQTT service not active")
    await mqtt_service.orchestrator.stop_sequence()
    return ApiResponse(ok=True, message="Sequence stopped")


@app.get("/api/mqtt/sequences", response_model=ApiResponse)
async def list_sequences() -> ApiResponse:
    """List available sequence YAML files from config/sequences/."""
    seq_dir = Path(__file__).parent.parent / "config" / "sequences"
    if not seq_dir.exists():
        return ApiResponse(ok=True, message="ok", data=[])
    files = sorted(str(f.name) for f in seq_dir.glob("*.yaml"))
    return ApiResponse(ok=True, message="ok", data=files)


@app.get("/api/mqtt/history/connectivity", response_model=ApiResponse)
async def connectivity_history(component: str | None = None, limit: int = 100) -> ApiResponse:
    if not mqtt_service:
        raise HTTPException(503, "MQTT service not active")
    records = await mqtt_service.history.get_recent_connectivity(component, limit)
    return ApiResponse(ok=True, message="ok", data=records)


@app.get("/api/mqtt/history/alerts", response_model=ApiResponse)
async def alert_history(limit: int = 50) -> ApiResponse:
    if not mqtt_service:
        raise HTTPException(503, "MQTT service not active")
    records = await mqtt_service.history.get_recent_alerts(limit)
    return ApiResponse(ok=True, message="ok", data=records)

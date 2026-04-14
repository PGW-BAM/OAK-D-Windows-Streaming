"""FastAPI application — multi-camera OAK-D 4 Pro streaming dashboard."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .camera_manager import CameraManager
from .config import settings
from .models import (
    ApiResponse,
    BandwidthCheckRequest,
    CameraControlRequest,
    CameraListResponse,
    Detection,
    InferenceModeRequest,
    RecordingStartRequest,
    StorageStatus,
    StreamSettingsRequest,
)
from .bandwidth import (
    BandwidthEstimate,
    BandwidthMatrix,
    build_bandwidth_matrix,
    check_feasibility,
)
from .recording import RecordingWorker, cleanup_old_recordings, get_storage_stats

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

# MQTT service (lazy — only started if broker is configured)
mqtt_service = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global mqtt_service

    logger.info("Starting camera discovery …")
    ids = camera_manager.discover()
    logger.info("Found %d camera(s) at startup: %s", len(ids), ids)

    # Start MQTT service (non-blocking, auto-reconnects in background)
    try:
        from .mqtt.service import MqttService
        mqtt_service = MqttService(camera_manager)
        await mqtt_service.start()
    except Exception as exc:
        logger.warning("MQTT service failed to start (standalone mode): %s", exc)
        mqtt_service = None

    yield

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

    # Attach recording worker to camera worker
    worker.recording_worker = rec_worker
    worker._recording = True
    worker._recording_mode = req.mode

    try:
        output_path = rec_worker.start(
            req.mode, req.interval_seconds,
            output_dir=custom_dir,
            filename_prefix=req.filename_prefix,
            stereo_capture=stereo_capture,
            fps=worker._stream_fps,
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

    rec_worker.stop()
    worker._recording = False
    worker._recording_mode = None
    worker.recording_worker = None

    # Check storage after recording
    cleanup_old_recordings()
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

            worker.recording_worker = rec_worker
            worker._recording = True
            worker._recording_mode = req.mode

            try:
                rec_worker.start(
                    req.mode, req.interval_seconds,
                    output_dir=custom_dir,
                    filename_prefix=req.filename_prefix,
                    stereo_capture=stereo_capture,
                    fps=worker._stream_fps,
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
            rec_worker.stop()
            worker._recording = False
            worker._recording_mode = None
            worker.recording_worker = None
            stopped.append(worker.id[:8])

    cleanup_old_recordings()
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

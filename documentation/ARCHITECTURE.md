# Architecture Overview

## Backend

### Startup (lifespan)
`backend/main.py` uses FastAPI's `lifespan` context manager:
1. Creates a global `CameraManager` instance
2. Calls `discover()` — finds all OAK devices, starts a `CameraWorker` thread per device
3. On shutdown: calls `CameraManager.shutdown()` which stops all worker threads

### CameraManager (`camera_manager.py`)
Owns a `dict[mxId, CameraWorker]` and a threading lock. Provides:
- `discover()` — calls `dai.Device.getAllAvailableDevices()`, starts new workers for new devices
- `get_worker(id)` — returns worker or raises `KeyError`
- `all_statuses()` — returns list of `CameraStatus` snapshots

### CameraWorker (`camera_manager.py`)
One thread per physical camera. Lifecycle:
```
start() → _thread → _run() → dai.Device context → poll loop → stop()
```

**Pipeline built in `_build_pipeline()`:**
- `dai.node.Camera` on `CAM_A` (RGB) → 1280×720 @ configurable FPS
- `dai.node.VideoEncoder` MJPEG at `settings.mjpeg_quality`
- `XLinkOut("mjpeg")` — main frame output
- `XLinkIn("control")` — accepts `dai.CameraControl` messages
- Optionally: `DetectionNetwork` → `XLinkOut("detections")` when `inference_mode == on_camera`

**Frame loop:**
1. `tryGet()` on mjpeg queue (non-blocking, `maxSize=2`)
2. `FrameBuffer.put(jpeg_bytes)` — thread-safe, single latest frame
3. FPS and latency calculated from packet timestamps
4. If `inference_mode == host`: decodes JPEG → OpenCV → Ultralytics YOLO → `DetectionBuffer.put()`
5. If `recording_worker` set: calls `recording_worker.feed(jpeg_bytes)`

**Key design choices:**
- Non-blocking `tryGet()` with `maxSize=2` → always latest frame, no queue buildup
- `FrameBuffer` holds only ONE frame — monitoring needs current, not history
- Threading over multiprocessing — DepthAI XLink is I/O-bound, GIL not a bottleneck
- On-camera inference mode switch triggers worker restart (pipeline rebuild required)

### RecordingWorker (`recording.py`)
Attached to `CameraWorker.recording_worker` when recording is active.

**Video mode (`VideoRecorder`):**
- Uses PyAV to open an MP4 container
- Each `feed(jpeg_bytes)` call wraps raw JPEG bytes in an `av.Packet` and muxes it
- File naming: `{CAM_ID}_{ISO8601}.mp4`
- Storage: ~1–2.3 GB/hour per camera at 1080p (hardware H.265 not used here — MJPEG frames muxed as-is)

**Interval mode (`IntervalRecorder`):**
- Background thread wakes every `interval_seconds`
- Saves latest frame from `_latest_frame` buffer as JPEG
- Output: `recordings/{cam_id}/interval/{CAM_ID}_{ISO8601}.jpg`

**Storage cleanup** (`cleanup_old_recordings()`):
- Triggered after each recording stop
- Deletes oldest files when `shutil.disk_usage` > `settings.storage_threshold_pct` (default 85%)

### MJPEG Streaming (`main.py`)
```python
async def _mjpeg_generator(camera_id):
    while True:
        frame, _ = worker.frame_buffer.get()
        if frame and count != last_count:
            yield BOUNDARY + headers + frame
        await asyncio.sleep(1 / settings.stream_fps)
```
Returns a `StreamingResponse` with `multipart/x-mixed-replace; boundary=frame`.
The browser `<img src="/api/camera/{id}/stream">` renders it natively — no JS video decoder needed.

---

## Frontend

### Data Flow
```
useCameraList (poll 3s) → GET /api/cameras → CameraStatus[]
useDetections (poll 200ms) → GET /api/camera/{id}/detections → Detection | null
<img src="/api/camera/{id}/stream"> → MJPEG push
```

### Component Tree
```
App
└── CameraGrid (react-grid-layout v2)
    └── CameraCard (per camera)
        ├── <img> MJPEG stream
        └── DetectionOverlay (canvas, absolute-positioned)
    └── ControlPanel (conditionally rendered right drawer)
```

### react-grid-layout v2 — IMPORTANT API CHANGE
This project uses **v2.2.2** which has a completely different prop API from v1:

```tsx
// v1 (OLD — does NOT work in this project)
<GridLayout cols={12} rowHeight={180} isDraggable isResizable margin={[6,6]} />

// v2 (CORRECT)
<GridLayout
  gridConfig={{ cols: 12, rowHeight: 180, margin: [6, 6] as const }}
  dragConfig={{ enabled: true, bounded: false, threshold: 3 }}
  resizeConfig={{ enabled: true, handles: ['se'] as const }}
/>
```

Types: `LayoutItem` = single item `{i,x,y,w,h}`, `Layout = readonly LayoutItem[]`
`onLayoutChange` receives `Layout` (not `LayoutItem`).

### Detection Overlay
`DetectionOverlay.tsx` draws bounding boxes on a `<canvas>` absolutely positioned over the `<img>`. Coordinates are normalized 0–1 (from backend) and multiplied by the rendered image dimensions from `img.offsetWidth/Height`.

---

## Configuration (`backend/config.py`)

All settings can be overridden via `.env` file or environment variables:

| Key | Default | Description |
|-----|---------|-------------|
| `RECORDINGS_DIR` | `recordings` | Output path for all recordings |
| `STORAGE_THRESHOLD_PCT` | `85.0` | Disk % that triggers cleanup |
| `STORAGE_MAX_AGE_DAYS` | `7` | Max age for rolling deletion |
| `MJPEG_QUALITY` | `85` | JPEG quality 1–100 |
| `STREAM_FPS` | `20` | Target stream FPS |
| `HOST_YOLO_MODEL` | `yolov8n.pt` | Default Ultralytics model |
| `POE_SUBNET` | `169.254.0.0/16` | PoE link-local subnet |
| `CORS_ORIGINS` | `["*"]` | Allowed CORS origins |

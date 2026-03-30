# API Reference

Base URL: `http://localhost:8000`

---

## Camera List

### `GET /api/cameras`
Returns all known cameras and their current status.

**Response:**
```json
{
  "cameras": [
    {
      "id": "14442C108144F1D000",
      "name": "OAK-14442C10",
      "connected": true,
      "ip": "169.254.1.222",
      "fps": 20.0,
      "latency_ms": 12.3,
      "recording": false,
      "recording_mode": null,
      "inference_mode": "none",
      "width": 1280,
      "height": 720
    }
  ],
  "total": 1
}
```

### `POST /api/cameras/discover`
Triggers a rescan for new OAK devices. Starts workers for newly found cameras.

**Response:** `{ "ok": true, "message": "Found 2 camera(s)", "data": ["id1", "id2"] }`

---

## Video Streaming

### `GET /api/camera/{camera_id}/stream`
MJPEG streaming response. Use directly as `<img src="...">` in browser.

- Content-Type: `multipart/x-mixed-replace; boundary=frame`
- Each part: `Content-Type: image/jpeg`
- Frame rate: controlled by `settings.stream_fps` (default 20 FPS)

### `GET /api/camera/{camera_id}/snapshot`
Returns a single JPEG frame as `image/jpeg`.

### `WS /ws/camera/{camera_id}`
WebSocket binary stream — sends raw JPEG bytes at stream FPS.
Alternative to MJPEG for clients that prefer WebSocket.

---

## Camera Control

### `POST /api/camera/{camera_id}/control`
Sends a `CameraControl` message to the device. All fields optional.

**Request body:**
```json
{
  "auto_exposure": true,
  "exposure_us": null,
  "iso": null,
  "auto_focus": true,
  "manual_focus": null,
  "auto_white_balance": true,
  "white_balance_k": null,
  "brightness": 0,
  "contrast": 0,
  "saturation": 0,
  "sharpness": 0,
  "luma_denoise": 0,
  "chroma_denoise": 0
}
```

**Field ranges:**
| Field | Range | Notes |
|-------|-------|-------|
| `exposure_us` | 1–33000 | µs; only used when `auto_exposure: false` |
| `iso` | 100–1600 | only used when `auto_exposure: false` |
| `manual_focus` | 0–255 | only used when `auto_focus: false` |
| `white_balance_k` | 1000–12000 | Kelvin; only when `auto_white_balance: false` |
| `brightness` | -10–10 | |
| `contrast` | -10–10 | |
| `saturation` | -10–10 | |
| `sharpness` | 0–4 | |
| `luma_denoise` | 0–4 | |
| `chroma_denoise` | 0–4 | |

**Response:** `{ "ok": true, "message": "Control applied" }`

---

## Recording

### `POST /api/camera/{camera_id}/recording/start`
Starts recording for a camera.

**Request body:**
```json
{
  "mode": "video",
  "interval_seconds": 5.0
}
```

- `mode`: `"video"` — continuous MP4 recording (MJPEG muxed into container)
- `mode`: `"interval"` — saves one JPEG every `interval_seconds`

**Response:** `{ "ok": true, "message": "Recording started", "data": "recordings/cam_id/..." }`

Returns 409 if recording is already active.

### `POST /api/camera/{camera_id}/recording/stop`
Stops any active recording. Triggers storage cleanup check.

**Response:** `{ "ok": true, "message": "Recording stopped" }`

### `GET /api/camera/{camera_id}/recording/status`
**Response:** `{ "ok": true, "message": "ok", "data": { "active": true, "mode": "video" } }`

---

## AI Inference

### `POST /api/camera/{camera_id}/inference/mode`
Switches the inference mode for a camera.

**Request body:**
```json
{
  "mode": "none",
  "model_path": null
}
```

- `mode: "none"` — disable inference
- `mode: "on_camera"` — use on-device SNPE (RVC4). **Triggers pipeline rebuild/worker restart.**
- `mode: "host"` — use Ultralytics on host GPU. Requires `host-inference` extras installed.
- `model_path` — optional override (e.g., `"yolov8s.pt"` or path to `.dlc`). Defaults to `settings.host_yolo_model`.

**Response:** `{ "ok": true, "message": "Inference mode set to host" }`

### `GET /api/camera/{camera_id}/detections`
Returns the latest detection result, or `null` if none available.

**Response:**
```json
{
  "camera_id": "14442C108144F1D000",
  "timestamp": 1741478400.123,
  "boxes": [
    {
      "x1": 0.12, "y1": 0.34, "x2": 0.56, "y2": 0.78,
      "confidence": 0.91,
      "class_id": 0,
      "label": "person"
    }
  ],
  "inference_mode": "host"
}
```
Coordinates are **normalized 0–1** relative to the full frame.

---

## Storage

### `GET /api/storage`
**Response:**
```json
{
  "total_gb": 500.1,
  "used_gb": 120.3,
  "free_gb": 379.8,
  "usage_pct": 24.1,
  "recordings_gb": 3.4
}
```

### `POST /api/storage/cleanup`
Manually triggers rolling deletion of oldest recordings until below threshold.

**Response:** `{ "ok": true, "message": "Deleted 12 file(s)" }`

---

## Frontend

### `GET /`
Serves `frontend/dist/index.html` (React SPA). Falls back to a plain HTML message if not built yet.

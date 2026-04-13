# Known Issues & Next Steps

## Known Issues / Not Yet Implemented

### 1. On-camera inference model not verified on RVC4
`_build_pipeline()` in `camera_manager.py` tries `dai.NNModelDescription("yolov6-nano")` for on-camera mode.
This uses DepthAI's model zoo auto-download. If the model isn't in the zoo or network is unavailable, it falls back gracefully with a warning. For custom `.dlc` models, the `model_path` field in `InferenceModeRequest` can be passed but the pipeline rebuild path needs to read it — **currently the model path is loaded into the worker but not passed into `_build_pipeline()`**.

**Fix needed in `camera_manager.py`:**
```python
# In CameraWorker, store model path before rebuild:
self._on_camera_model_path = model_path  # set in set_inference_mode()

# In _build_pipeline(), use it:
if self._inference_mode == InferenceMode.on_camera:
    nn = pipeline.create(dai.node.NeuralNetwork)
    nn.setModelPath(self._on_camera_model_path or 'yolov6-nano')
    nn.setBackend("snpe")
    nn.setBackendProperties({"runtime": "dsp", "performance_profile": "default"})
```

### 2. Frame dimensions not populated
`CameraStatus.width` and `height` are always 0. They should be set from the first decoded frame.
In `_process_frame()` in `camera_manager.py`, after decoding the JPEG, set:
```python
import cv2, numpy as np
arr = np.frombuffer(data, np.uint8)
img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
if img is not None:
    self._frame_height, self._frame_width = img.shape[:2]
```
(Do this only once, check `self._frame_width == 0` to avoid per-frame decode overhead.)

### 3. ControlPanel doesn't re-sync from live camera state
The ControlPanel initialises all sliders at hardcoded defaults (e.g., auto_exposure=True). It does not read back the current camera state. When the panel is closed and reopened, sliders reset.
**Fix:** Fetch current settings from the device or cache the last-sent values server-side.

### 4. Layout doesn't persist across page refreshes
The react-grid-layout state is in-memory only.
**Fix:** Persist layout to `localStorage` in `CameraGrid.tsx`:
```ts
const [layout, setLayout] = useState<LayoutItem[]>(() => {
  const saved = localStorage.getItem('camera-layout')
  return saved ? JSON.parse(saved) : defaultLayout(cameras)
})
const handleLayoutChange = (l: Layout) => {
  const arr = [...l]
  setLayout(arr)
  localStorage.setItem('camera-layout', JSON.stringify(arr))
}
```

### 5. No graceful reconnect for disconnected cameras
If a camera disconnects, the worker thread exits. `CameraStatus.connected` becomes `false` but there is no automatic reconnect loop. The user must click "Discover cameras" to restart the worker.
**Fix:** Add a reconnect loop in `_run()`:
```python
while not self._stop_event.is_set():
    try:
        self._connect_and_poll()
    except Exception:
        self._connected = False
        time.sleep(5)  # wait before retry
```

### 6. Host YOLO runs synchronously in the frame thread
In `_process_frame()`, host inference calls `self._run_host_inference(data)` inline. This blocks the frame thread on GPU inference latency (~10–100ms), reducing effective streaming FPS.
**Fix:** Move inference to a separate thread with its own frame buffer:
```python
self._inference_queue = queue.Queue(maxsize=1)
self._inference_thread = threading.Thread(target=self._inference_loop)
```

### 7. PyAV MJPEG-in-MP4 container may not be seekable
The VideoRecorder muxes raw MJPEG packets directly. Some players may not support this. For better compatibility, consider H.265 encoding via DepthAI's `VideoEncoder` node and piping the bitstream through PyAV.

### 8. Windows Firewall not automatically configured
Windows Firewall may block UDP broadcast (device discovery) and TCP data. The user must manually add a Python exception or temporarily disable the firewall for the PoE adapter.

---

## MQTT Integration Issues

### 9. Windows asyncio event loop compatibility
Windows' default `ProactorEventLoop` doesn't support `add_reader`/`add_writer` required by paho-mqtt. **Fixed:** The MQTT client runs in a dedicated thread with `asyncio.SelectorEventLoop`. Edge case: if the MQTT thread crashes, it won't auto-restart until the app is restarted.

### 10. MQTT sequences vs. existing interval recording
When an MQTT capture sequence is running, the orchestrator calls `capture_snapshot()` at specific drive positions. If interval recording is also active, both systems capture independently. There is no coordination between them — the operator should stop interval recording before starting a sequence.

### 11. Email alert delivery depends on SMTP configuration
If `config/mqtt.yaml` has no SMTP settings, alerts are only logged to SQLite and `logs/unsent_alerts.jsonl`. The first-run email prompt described in the PRD is not yet implemented — the operator must edit `config/mqtt.yaml` manually.

### 12. cam_id mapping is positional
The orchestrator maps `cam1` → first discovered camera, `cam2` → second. If cameras are discovered in a different order across restarts, the mapping may change. A future fix should map by serial number or IP in config.

---

## Suggested Next Steps (Priority Order)

1. **Implement Pi drive controller** — GPIO stepper control + MQTT client (see `docs/RASPBERRY_PI_IMPLEMENTATION.md`)
2. **Fix on-camera inference model path** (Issue #1 above)
3. **Add auto-reconnect for cameras** (Issue #5)
4. **Add cam_id mapping by serial number** (Issue #12) — stable camera identification
5. **Move host inference off frame thread** (Issue #6)
6. **Persist layout to localStorage** (Issue #4)
7. **Add streaming overlay** — render connectivity status onto camera preview (Phase 2)
8. **Add first-run email prompt** (Issue #11)
9. **Add snapshot download button** in `CameraCard`
10. **Add recordings browser** — list files in `recordings/` via new API endpoint
11. **Consider WebRTC upgrade path** — for >4 cameras at 1080p where MJPEG bandwidth becomes limiting

---

## Environment Notes

- `uv` is installed as a pip package, invoked as `python -m uv` (not on PATH)
- The `.venv` is at project root, activated by `start.bat` via `.venv\Scripts\python.exe`
- `react-grid-layout` v2 API is incompatible with v1 — see `ARCHITECTURE.md`
- `host-inference` extra (Ultralytics + PyTorch) is NOT installed by default — run `python -m uv sync --extra host-inference` separately
- Frontend production build lives at `frontend/dist/` and is committed to `.gitignore` — rebuild with `cd frontend && npm run build` after any frontend changes

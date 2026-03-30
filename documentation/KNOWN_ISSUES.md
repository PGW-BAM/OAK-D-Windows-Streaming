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

## Suggested Next Steps (Priority Order)

1. **Test with real hardware** — connect a PoE OAK-D 4 Pro, run `start.bat`, click "Discover cameras"
2. **Fix on-camera inference model path** (Issue #1 above)
3. **Fix frame dimensions** (Issue #2) — needed for correct detection overlay scaling
4. **Add auto-reconnect** (Issue #5)
5. **Move host inference off frame thread** (Issue #6) — important for multi-camera + GPU
6. **Persist layout to localStorage** (Issue #4)
7. **Add depth stream support** — second pipeline output for `StereoDepth` node, serve as 16-bit PNG
8. **Add stereo cameras** — `CAM_B` and `CAM_C` mono cameras for depth
9. **Add snapshot download button** in `CameraCard`
10. **Add recordings browser** — list files in `recordings/` via new API endpoint
11. **Add per-camera name editing** — store in a config file, display in UI
12. **Consider WebRTC upgrade path** — for >4 cameras at 1080p where MJPEG bandwidth becomes limiting

---

## Environment Notes

- `uv` is installed as a pip package, invoked as `python -m uv` (not on PATH)
- The `.venv` is at project root, activated by `start.bat` via `.venv\Scripts\python.exe`
- `react-grid-layout` v2 API is incompatible with v1 — see `ARCHITECTURE.md`
- `host-inference` extra (Ultralytics + PyTorch) is NOT installed by default — run `python -m uv sync --extra host-inference` separately
- Frontend production build lives at `frontend/dist/` and is committed to `.gitignore` — rebuild with `cd frontend && npm run build` after any frontend changes

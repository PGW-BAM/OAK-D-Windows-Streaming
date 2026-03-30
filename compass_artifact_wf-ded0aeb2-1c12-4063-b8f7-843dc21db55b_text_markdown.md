# Multi-Camera OAK-D 4 Pro browser streaming app: PRD research foundations

The OAK-D 4 Pro represents a generational leap in embedded vision — its **Qualcomm QCS8550 SoC delivers 48 TOPS** of AI inference, **48MP RGB sensing**, and standalone Linux compute in an IP67 industrial package. Building a browser-based multi-camera streaming application around this hardware is technically feasible using DepthAI v3's unified pipeline API, FastAPI for async backend streaming, and React for the dashboard frontend. No existing open-source project fills this exact niche, making this a novel and valuable product opportunity. This document compiles all technical research needed to write a complete PRD.

---

## 1. OAK-D 4 Pro hardware delivers 48 TOPS in an IP67 enclosure

The OAK-D 4 Pro runs on the **RVC4 platform** (Robotics Vision Core 4), a fundamental departure from the Intel Myriad X–based RVC2 generation. The RVC4 uses a **Qualcomm QCS8550 SoC** with a 6-core ARM CPU, **8 GB RAM**, and **128 GB onboard storage**, enabling standalone operation via Luxonis OS (Yocto Linux, kernel 5.15). AI performance reaches **48 TOPS (INT8) on the DSP** plus **4 TOPS (FP16) on the GPU**, totaling approximately 52 TOPS — a **10–40× improvement** over the previous generation's 4 TOPS.

### Sensor specifications

| Component | Sensor | Resolution | Max FPS | FOV | Shutter | Notes |
|-----------|--------|-----------|---------|-----|---------|-------|
| RGB (main) | IMX586 | **48 MP** (8000×6000) | 480 FPS (lower res) | 82.4° DFOV | Rolling | AF or FF variants |
| Stereo pair (×2) | OV9282 | 1 MP (1280×800) | 60 FPS | 84.5° DFOV | **Global** | 940nm IR capable |

**Stereo depth** operates at up to **800P@60FPS** with 4-bit subpixel precision, 64 disparity search, and an 8-bit confidence map. The 75mm baseline delivers depth accuracy under 1.5% error below 4 meters. The "Pro" variant includes an **IR dot projector** (Himax, Class 1 Laser) and IR illumination LED for active stereo and night vision. The **9-axis IMU** combines an ICM-42688-P (6-axis accelerometer + gyroscope) with an AK09919C magnetometer.

### Connectivity and encoding

The device supports **USB 3.2 Gen2 (10 Gbps)** and **802.3at PoE with 2.5GBASE-T** (2.5 Gbps) simultaneously via M12 connector. The hardware encoder handles **4K@120FPS** encoding (H.264/H.265) and **4K@240FPS** decoding (H.264/H.265/VP9/AV1). The MJPEG encoder supports up to 16384×8192 at 450 MPix/sec. Physical specs: **143.5 × 42.5 × 67.3 mm**, 674g, IP67 rated, -20°C to 50°C operating range. Price is **$949 USD** (AF/FF variants).

---

## 2. DepthAI v3 SDK provides a unified pipeline API for RVC4

**DepthAI v3** is the current SDK supporting both RVC2 and RVC4 devices. It uses a **Pipeline → Node → Message** architecture where nodes represent sensors, hardware accelerators, or compute functions, linked together to form processing graphs deployed to the device. The latest release is **v3.3.0** (January 2026), installable via `pip install depthai`.

### Camera control API

The unified `dai.node.Camera` node replaces the legacy `ColorCamera`/`MonoCamera` split. It supports flexible output requests at any resolution with automatic sensor mode selection:

```python
cam = pipeline.create(dai.node.Camera)
cam.build(dai.CameraBoardSocket.CAM_A)
nn_input = cam.requestOutput(size=(640, 640), type=dai.ImgFrame.Type.NV12, fps=30)
hd_stream = cam.requestOutput(size=(1280, 720), type=dai.ImgFrame.Type.BGR888p, fps=20)
full_res = cam.requestFullResolutionOutput(type=dai.ImgFrame.Type.BGR888p, fps=1)
```

Runtime camera control uses `dai.CameraControl` messages sent to `cam.inputControl`:

- **Exposure**: `setManualExposure(timeUs, isoSensitivity)` (1–33000 µs, ISO 100–1600), `setAutoExposureEnable()`, `setAutoExposureLimit()`, `setAutoExposureRegion()`
- **Focus**: `setManualFocus(0–255)`, `setAutoFocusMode(CONTINUOUS_VIDEO)`, `setAutoFocusRegion()`
- **White balance**: `setManualWhiteBalance(1000–12000K)`, `setAutoWhiteBalanceMode(AUTO)`, `setAutoWhiteBalanceLock()`
- **Image quality**: `setBrightness()`, `setContrast()`, `setSaturation()`, `setSharpness()`, `setLumaDenoise(0–4)`, `setChromaDenoise(0–4)`
- **Still capture**: `setCaptureStill(True)` for on-demand high-res frame grab

### StereoDepth configuration

The `dai.node.StereoDepth` node supports preset modes (`DEFAULT`, `FACE`, `HIGH_DETAIL`, `ROBOTICS`) and exposes granular controls: extended disparity (0–190 range), subpixel precision, left-right check, depth alignment to RGB, confidence threshold, and median filtering. Post-processing filters include speckle, temporal, threshold (min/max range in mm), decimation, brightness, and hole-filling. RVC4 adds **neural depth modes** (`NEURAL_DEPTH_LARGE/MEDIUM/SMALL/NANO`) and neural-assisted stereo fusion.

### Key v3 nodes relevant to this application

| Node | Purpose |
|------|---------|
| `Camera` | Unified camera sensor access |
| `StereoDepth` | Classical stereo depth computation |
| `NeuralNetwork` | AI inference (SNPE on RVC4, OpenVINO on RVC2) |
| `DetectionNetwork` | NN with built-in detection parsing |
| `SpatialDetectionNetwork` | Detection + 3D spatial location |
| `VideoEncoder` | H.264/H.265/MJPEG hardware encoding |
| `ImageManip` | Resize, crop, warp, type conversion |
| `RGBD` | Synchronized aligned color + depth, point clouds |
| `Sync` | Cross-stream message synchronization |
| `ObjectTracker` | 2D/3D tracking (OC-Sort on RVC4) |
| `Script` | Custom on-device scripts |
| `IMU` | IMU data acquisition |

---

## 3. Multi-camera PoE discovery and simultaneous streaming

### Device discovery and connection

DepthAI discovers cameras via **UDP broadcast** on the local network. PoE cameras behave identically to USB cameras from the API perspective:

```python
import depthai as dai

# Discover all connected devices (USB + PoE)
for info in dai.Device.getAllAvailableDevices():
    print(f"MxID: {info.getMxId()}, State: {info.state}")

# Connect by IP address (PoE cameras on 169.254.x.x)
device_info = dai.DeviceInfo("169.254.1.222")

# Connect by unique hardware identifier
device_info = dai.DeviceInfo("14442C108144F1D000")
```

### PoE link-local networking

OAK PoE cameras attempt **DHCP first**; if unavailable, they fall back to a static IP of **`169.254.1.222`**. For multiple cameras without DHCP, each camera must be assigned unique static IPs using the `poe_set_ip` example or `oakctl`. The host adapter must be configured in the same subnet (e.g., `169.254.1.1` with mask `255.255.0.0`). Stable bandwidth reaches approximately **700 Mbps** per camera after initial pipeline upload, with the 2.5GBASE-T link providing significant headroom.

### Multi-camera architecture pattern

The recommended pattern uses **`contextlib.ExitStack`** with a thread-per-camera model:

```python
import depthai as dai
import contextlib

with contextlib.ExitStack() as stack:
    device_infos = dai.Device.getAllAvailableDevices()
    devices, queues = [], []
    for info in device_infos:
        pipeline = create_pipeline()  # Creates camera + encoder nodes
        device = stack.enter_context(dai.Device(pipeline, info))
        devices.append(device)
        queues.append(device.getOutputQueue("mjpeg", maxSize=2, blocking=False))
```

There is **no documented hard limit** on simultaneous cameras. Luxonis documentation states "use as many OAK cameras as you need," with previous-generation setups running 10–20 devices from a single host. Timestamp synchronization achieves **<500 µs accuracy for PoE** and <200 µs for USB devices. Hardware sync via FSYNC Y-adapter is available for frame-level synchronization across devices.

---

## 4. On-camera YOLO inference uses SNPE on the RVC4

### The model format paradigm shift

The RVC4's Qualcomm SoC uses **SNPE (Snapdragon Neural Processing Engine)** instead of OpenVINO. Models must be in **`.dlc` format** (or the cross-platform NN Archive `.tar.xz`), not `.blob`. This changes the conversion pipeline fundamentally:

- **RVC2 (legacy)**: YOLO `.pt` → ONNX → OpenVINO IR → `.blob`
- **RVC4 (current)**: YOLO `.pt` → ONNX → `.dlc` (via SNPE tools or HubAI)

### Recommended conversion tool: HubAI

The **Luxonis HubAI** platform (https://hub.luxonis.com) is the recommended conversion path, supporting YOLOv5 through YOLOv12 including detection, segmentation, pose estimation, and OBB variants:

```python
from modelconverter.hub import convert
converted = convert.RVC4("yolov8n.pt", yolo_version="yolov8", api_key=api_key)
```

For offline conversion, the **ModelConverter** tool (`pip install modelconverter`) uses Docker containers with SNPE tooling. Legacy tools include `tools.luxonis.com` (web-based, RVC2 only) and `blobconverter` (PyPI package, RVC2 only).

### DepthAI v3 inference pipeline

The v3 API simplifies on-device inference with auto-download from the model zoo:

```python
with dai.Pipeline() as pipeline:
    cam = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
    det = pipeline.create(dai.node.DetectionNetwork).build(
        cam, dai.NNModelDescription("yolov6-nano")
    )
```

For custom models on RVC4, explicitly set the SNPE backend:

```python
nn = pipeline.create(dai.node.NeuralNetwork)
nn.setModelPath('model.dlc')
nn.setBackend("snpe")
nn.setBackendProperties({"runtime": "dsp", "performance_profile": "default"})
```

### RVC4 inference performance benchmarks

| Model | Input Size | Peak FPS | Power | Notes |
|-------|-----------|----------|-------|-------|
| **YOLOv6n** | 512×288 | **2,340** | 7.5W | Configurable: 190 FPS @ 0.7W → 2340 FPS @ 7.5W |
| **YOLOv5m** | 640×640 | **280** | 8.4W | |
| **YOLOv7-W6** | 640×640 | **162** | 7.85W | |
| YOLOv3 Tiny | — | **1,342** | 9.9W | |

The RVC4 supports configurable power/performance profiles, allowing the application to balance inference speed against thermal constraints. INT8 quantization (unavailable on RVC2) provides the highest throughput.

---

## 5. Browser streaming architecture: FastAPI + MJPEG + React

### Streaming protocol recommendation

For a LAN-based monitoring application with 2–8 cameras, **MJPEG over HTTP** provides the optimal simplicity-to-performance ratio. The OAK-D 4 Pro's hardware MJPEG encoder eliminates host CPU encoding costs entirely — JPEG-compressed frames arrive pre-encoded from the device and are forwarded directly to the browser as byte streams.

| Protocol | Latency | Bandwidth per 720p stream | Complexity | Browser Support |
|----------|---------|--------------------------|------------|----------------|
| **MJPEG/HTTP** | 25–50ms | 2–10 Mbps | **Lowest** | Universal (`<img>` tag) |
| WebSocket + H.264/WebCodecs | 50–150ms | 1–3 Mbps | Medium | Chrome only |
| WebRTC (via aiortc/MediaMTX) | 60–150ms | 1–3 Mbps | Highest | All modern browsers |
| HLS/DASH | 2–30 seconds | 1–3 Mbps | Medium | Universal |

MJPEG's 25–50ms total latency (5–10ms hardware encode + 5–10ms XLink transfer + 5–10ms HTTP delivery + 5–16ms browser render) is excellent for inspection use cases. If bandwidth becomes constraining with 8+ cameras at 1080p, the architecture can upgrade to WebSocket + H.264/WebCodecs or WebRTC without restructuring the backend.

### Backend: FastAPI with thread-per-camera manager

**FastAPI + Uvicorn** is the recommended backend, providing native async/await support, first-class WebSocket handling, and high concurrent connection performance. The architecture uses a `CameraManager` class running one daemon thread per camera, each operating its own DepthAI pipeline and writing JPEG frames into a thread-safe shared buffer:

```python
# Core API endpoints
GET  /api/cameras                → List cameras + status
GET  /api/camera/{id}/stream     → MJPEG StreamingResponse
GET  /api/camera/{id}/snapshot   → Single JPEG frame
WS   /ws/camera/{id}            → WebSocket binary stream
POST /api/camera/{id}/control    → Camera control (exposure, focus, WB)
GET  /api/camera/{id}/detections → NN inference results
GET  /                           → Serve React frontend
```

Key design decisions: **threading over multiprocessing** (DepthAI's XLink is I/O-bound, so Python's GIL doesn't bottleneck it), **non-blocking queues** (`maxSize=2, blocking=False` to always get the latest frame), and **latest-frame-only buffers** (monitoring needs current frames, not queued history). Luxonis confirms: "Since DepthAI does all the heavy lifting, you can usually use quite a few of them with very little burden to the host."

### Frontend: React with react-grid-layout

**React** paired with **react-grid-layout** provides a draggable, resizable camera grid dashboard used by production tools like Grafana and Kibana. MJPEG streams display in simple `<img>` tags pointing to `/api/camera/{id}/stream` endpoints — no complex video decoding logic needed. Essential UI components:

- Layout presets (1×1, 2×2, 3×3, 4×2) with quick-switch buttons
- Per-camera status overlays (FPS, latency, connection status, camera name)
- Click-to-maximize for individual camera inspection
- Camera control panel (exposure, focus, white balance sliders)
- Recording controls (start/stop recording, interval capture toggle)
- Detection overlay rendering (bounding boxes drawn on `<canvas>` overlaid on stream)

The official **`@luxonis/depthai-viewer-common`** npm package (v1.1.29) provides React components for building custom DepthAI frontends, though building custom components offers more flexibility for this specific use case.

### DepthAI v3 RemoteConnection (alternative quick path)

For rapid prototyping, DepthAI v3 includes a built-in `dai.RemoteConnection` that serves a web UI with WebSocket-based streaming at `localhost:8080` with zero custom frontend code. However, it targets single-device usage and lacks multi-camera dashboard features.

---

## 6. Data recording spans video, images, depth, and point clouds

### Video recording

The optimal approach uses **on-device H.265 hardware encoding** piped directly to `.mp4` containers via the PyAv library (`pip install av`), achieving maximum compression with zero host encoding overhead. Storage estimates: **H.265 @ 1080p/30fps ≈ 1–2.3 GB/hour per stream**; H.264 ≈ 1.5–3.6 GB/hour; MJPEG ≈ 4.5–9 GB/hour.

The DepthAI SDK's `OakCamera.record()` provides high-level recording with automatic timestamp synchronization:

```python
from depthai_sdk import OakCamera, RecordType
with OakCamera() as oak:
    color = oak.create_camera('color', resolution='1080P', fps=20, encode='H265')
    oak.record([color.out.encoded], './recordings/', RecordType.VIDEO)
```

For timestamp-embedded filenames, use the convention: **`{CAMERA_ID}_{ISO8601_TIMESTAMP}.{ext}`** (e.g., `CAM01_20260224T143022_123456.mp4`).

### Interval image capture

Implement a timer-based capture loop comparing `time.time()` against a configurable interval. Use **PNG for lossless inspection data** (critical quality preservation) or **JPEG at quality 95** for high-volume monitoring where storage efficiency matters. Frame capture can be triggered via the `setCaptureStill(True)` control message for on-demand high-resolution grabs independent of the streaming resolution.

### Depth data formats

- **16-bit PNG**: Best balance of compatibility and size (~250 KB/frame at 640×400). Values represent depth in millimeters; 0 indicates invalid/unknown.
- **NumPy `.npy`**: Fastest for programmatic access. Use `np.save()` / `np.load()` for raw uint16 depth arrays.
- **PLY point clouds**: Universal format via Open3D, supporting color + normals (~5–15 MB/frame at 640×400 points). The DepthAI `RGBD` node produces synchronized aligned color + depth suitable for direct point cloud generation.

### Storage management

Implement **rolling deletion** triggered when disk usage exceeds a configurable threshold (e.g., 85%). Use `shutil.disk_usage()` for monitoring and age-based cleanup (delete files older than N days) or space-based cleanup (delete oldest files until below threshold). For a system with 4 cameras recording H.265 at 1080p/20fps, budget approximately **4–9 GB/hour** total, or **96–216 GB/day**.

---

## 7. Host-side YOLO inference complements on-camera processing

### When to use host GPU vs on-camera VPU

On-camera inference (RVC4 VPU) is ideal for always-on detection with small-to-medium models, offloading the host entirely. Host GPU inference suits **larger models** (YOLOv8l, YOLO11x), **custom post-processing**, or scenarios where the same frames need multiple model passes. The application should support both modes, selectable per camera.

### Ultralytics setup on Windows

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
pip install ultralytics
```

Requirements: **Python 3.9–3.13**, NVIDIA GPU with CUDA Compute Capability 6.0+ (Pascal or newer), **8 GB+ VRAM recommended**. Approximate inference speeds: YOLOv8s at ~24ms/image on GTX 1650, ~108ms on CPU.

### Multi-stream GPU inference strategies

For processing 2–8 camera streams simultaneously on host GPU:

- **Thread-per-camera** with separate YOLO model instances (thread-safe, most flexible): ~500 MB base + ~200 MB VRAM per stream for nano models
- **Batched inference** via Ultralytics `.streams` file (best GPU utilization): collects frames from all cameras and processes as a single batch
- **Round-robin** with single model instance (simplest, lowest total throughput)

**Critical thread safety note**: Ultralytics requires **separate model objects per thread** to avoid race conditions. GPU memory estimates for 4 simultaneous streams: YOLOv8n ≈ 1.3 GB VRAM, YOLOv8s ≈ 2.0 GB, YOLOv8m ≈ 3.5 GB. For 8+ streams, use nano/small models or export to TensorRT for 2–3× speedup.

```python
# Integration pattern: DepthAI camera → host YOLO
frame = q.get().getCvFrame()  # Get frame from OAK camera
results = model(frame, device=0, verbose=False)  # Run YOLO on host GPU
detections = results[0].boxes  # Extract bounding boxes
```

---

## 8. No existing multi-camera OAK web dashboard exists

### Luxonis official resources

The **oak-examples** repository (939 stars) contains individual streaming examples — MJPEG, WebRTC (via aiortc), RTSP (via GStreamer), and PoE TCP streaming — but none combine these into a multi-camera dashboard. The **OAK Viewer** (DepthAI Viewer) provides a web-based single-device visualization tool using WebSocket + WebCodecs, serving as the closest reference implementation. The **`@luxonis/depthai-viewer-common`** npm package enables custom React frontends for DepthAI applications.

### Reference architectures from adjacent projects

**Frigate NVR** offers the most directly applicable architecture: multi-camera web dashboard with ML inference, using **go2rtc** for WebRTC/MSE/HLS streaming, camera groups with customizable layouts, 24/7 recording with retention policies, and MQTT for event communication. **go2rtc** itself is a zero-dependency streaming server supporting RTSP → WebRTC conversion with near-zero delay, and could serve as streaming infrastructure if OAK cameras output RTSP. The **Luxonis Hub** cloud platform demonstrates fleet management patterns (OTA updates, live streaming, containerized "OAK Apps") that could be replicated locally.

### Key gap identified

**No existing open-source project provides a complete multi-camera web streaming dashboard specifically for OAK devices.** This represents an underserved niche. The closest building blocks are Luxonis's individual streaming examples, the OAK Viewer (single-device), and Frigate's architecture patterns (not OAK-specific).

---

## 9. Windows 10/11 setup requires network and driver configuration

### DepthAI installation

```bash
pip install depthai  # v3.3.0, supports Python 3.9–3.14 on Windows x86-64
```

Alternatively, the Windows installer from GitHub releases installs OAK Viewer plus all dependencies. No C++ build tools are needed for pip installation. For USB cameras, the **WinUSB driver** installs automatically; PoE cameras require no special drivers.

### PoE network configuration for 169.254.x.x

For direct connection without a DHCP server, configure the Windows Ethernet adapter manually: **IP `169.254.1.1`**, subnet **`255.255.0.0`**, gateway blank. The OAK camera defaults to `169.254.1.222`. For multiple cameras, either deploy a lightweight DHCP server or assign static IPs to each camera via `poe_set_ip`. **Windows Firewall must allow UDP broadcast** for device discovery and TCP for data transfer — add an exception for the Python executable on the PoE network adapter.

### Common Windows issues

- **"No available devices"**: Check USB cable quality (must be USB3 with blue interior), verify PoE subnet configuration, check firewall rules
- **Device disconnects / X_LINK errors**: Caused by poor USB3 cables; use short (<1m) high-quality cables or force USB2 mode via `maxUsbSpeed=dai.UsbSpeed.HIGH`
- **Multiple USB devices**: Each camera needs its own USB controller (not just port); PoE avoids this limitation entirely
- **WSL 2**: USB OAK requires `usbipd-win` for forwarding; PoE OAK requires `--network=host` Docker flag

---

## Conclusion: a recommended technology stack emerges

This research identifies a clear, well-supported technology stack for the application. **DepthAI v3** provides the unified API for managing RVC4-based OAK-D 4 Pro cameras with comprehensive hardware control. **On-camera YOLO via SNPE** delivers inference at hundreds to thousands of FPS while consuming under 10W, complemented by **host-side Ultralytics YOLO** for larger models or custom processing. **MJPEG over HTTP** via **FastAPI/Uvicorn** achieves 25–50ms streaming latency with zero host encoding overhead thanks to on-device hardware JPEG compression. **React + react-grid-layout** provides the proven dashboard UI pattern. The PoE-connected cameras on **169.254.x.x** addresses work reliably on Windows with straightforward network adapter configuration.

The most significant finding is the **absence of any existing multi-camera OAK web dashboard**, making this a genuinely novel product. The architecture should be designed for extensibility: MJPEG streaming first (simplest, sufficient for LAN), with a clear upgrade path to H.264/WebCodecs or WebRTC if bandwidth constraints emerge at scale. The thread-per-camera model scales to 8+ devices with minimal host burden, since DepthAI offloads all encoding and inference to the camera hardware.
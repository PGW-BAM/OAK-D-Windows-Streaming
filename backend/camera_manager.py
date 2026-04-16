"""Thread-per-camera manager for OAK-D 4 Pro devices."""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import depthai as dai
import numpy as np

from .config import settings
from .models import (
    BoundingBox,
    CameraControlRequest,
    CameraStatus,
    Detection,
    InferenceMode,
    RecordingMode,
    RecordingStartRequest,
    StereoMode,
    StreamSettingsRequest,
)

logger = logging.getLogger(__name__)

RESOLUTION_PRESETS: dict[str, tuple[int, int]] = {
    "4k": (3840, 2160),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "480p": (640, 480),
}

STEREO_RESOLUTION: tuple[int, int] = (1280, 800)  # native OAK-D mono sensor


@dataclass
class FrameBuffer:
    """Lock-protected single-frame buffer; always holds the latest JPEG bytes."""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _data: bytes = b""
    _timestamp: float = 0.0
    _frame_count: int = 0

    def put(self, data: bytes) -> None:
        with self._lock:
            self._data = data
            self._timestamp = time.monotonic()
            self._frame_count += 1

    def get(self) -> tuple[bytes, float]:
        with self._lock:
            return self._data, self._timestamp

    @property
    def frame_count(self) -> int:
        with self._lock:
            return self._frame_count


@dataclass
class ImuBuffer:
    """Lock-protected buffer for the latest IMU angle reading."""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _roll_deg: float = 0.0
    _pitch_deg: float = 0.0
    _timestamp: float = 0.0
    _has_data: bool = False

    def put(self, roll_deg: float, pitch_deg: float) -> None:
        with self._lock:
            self._roll_deg = roll_deg
            self._pitch_deg = pitch_deg
            self._timestamp = time.monotonic()
            self._has_data = True

    def get(self) -> tuple[float, float, bool]:
        """Returns (roll_deg, pitch_deg, has_data)."""
        with self._lock:
            return self._roll_deg, self._pitch_deg, self._has_data


@dataclass
class DetectionBuffer:
    """Lock-protected latest detection result buffer."""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _detection: Optional[Detection] = None

    def put(self, detection: Detection) -> None:
        with self._lock:
            self._detection = detection

    def get(self) -> Optional[Detection]:
        with self._lock:
            return self._detection


def _compute_imu_angles(ax: float, ay: float, az: float) -> tuple[float, float]:
    """Compute roll and pitch in degrees from raw accelerometer data."""
    roll_rad = math.atan2(ay, az)
    pitch_rad = math.atan2(-ax, math.sqrt(ay ** 2 + az ** 2))
    return math.degrees(roll_rad), math.degrees(pitch_rad)


class CameraWorker:
    """Manages one OAK device: pipeline, frame acquisition, control, recording."""

    def __init__(self, device_info: dai.DeviceInfo) -> None:
        self.device_info = device_info
        self.id: str = device_info.getDeviceId()
        self.ip: str | None = None

        # Frame buffers
        self.frame_buffer = FrameBuffer()
        self.left_frame_buffer = FrameBuffer()
        self.right_frame_buffer = FrameBuffer()
        self.detection_buffer = DetectionBuffer()
        self.imu_buffer = ImuBuffer()

        self._device: Optional[dai.Device] = None
        self._pipeline: Optional[dai.Pipeline] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = False

        # Camera state
        self._inference_mode = InferenceMode.none
        self._recording = False
        self._recording_mode: Optional[RecordingMode] = None

        # Enabled flag — when False the pipeline is stopped to free bandwidth
        self._enabled: bool = True

        # Per-camera stream settings (defaults from global config)
        self._stream_fps: int = settings.stream_fps
        self._mjpeg_quality: int = settings.mjpeg_quality
        self._resolution: str = "720p"
        self._stereo_mode: StereoMode = StereoMode.main_only
        self._flip_180: bool = False  # Rotate 180° for ceiling-mounted cameras

        # FPS tracking
        self._fps: float = 0.0
        self._latency_ms: float = 0.0
        self._fps_counter = 0
        self._fps_last_time = time.monotonic()
        self._frame_width = 0
        self._frame_height = 0

        # Queues (set during pipeline build)
        self._mjpeg_queue: Optional[dai.MessageQueue] = None
        self._left_mjpeg_queue: Optional[dai.MessageQueue] = None
        self._right_mjpeg_queue: Optional[dai.MessageQueue] = None
        self._det_queue: Optional[dai.MessageQueue] = None
        self._imu_queue: Optional[dai.MessageQueue] = None
        self._control_input: Optional[dai.InputQueue] = None

        # Remember the last control the user/calibration applied so we can
        # re-assert focus/exposure/WB after pipeline rebuilds — otherwise the
        # OAK-D defaults back to continuous autofocus on every restart.
        self._last_control: Optional[CameraControlRequest] = None

        # Host-side YOLO (lazy import)
        self._host_model = None
        self._host_model_path: Optional[str] = None

        # Recording helpers (set by recording.py)
        self.recording_worker = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"cam-{self.id[:8]}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            # High-bandwidth pipelines (4K+60fps) can take several seconds to
            # wind down cleanly via pipeline.stop() / device.close().  5 s was
            # too short and left the device open when the next camera tried to
            # start.  30 s is a safe upper bound.
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                logger.warning(
                    "Camera %s worker thread did not stop within 30 s "
                    "— device may still be open",
                    self.id,
                )
        self._connected = False

    def _build_pipeline(self, device: dai.Device) -> dai.Pipeline:
        pipeline = dai.Pipeline(device)
        res = RESOLUTION_PRESETS.get(self._resolution, (1280, 720))
        fps = self._stream_fps
        quality = self._mjpeg_quality

        # --- RGB camera (main) ---
        need_main = self._stereo_mode in (StereoMode.main_only, StereoMode.both)
        if need_main:
            cam = pipeline.create(dai.node.Camera)
            cam.build(dai.CameraBoardSocket.CAM_A)

            encoder = pipeline.create(dai.node.VideoEncoder)
            encoder.setDefaultProfilePreset(
                fps, dai.VideoEncoderProperties.Profile.MJPEG
            )
            encoder.setQuality(quality)
            cam_output = cam.requestOutput(
                size=res,
                type=dai.ImgFrame.Type.NV12,
                fps=fps,
            )
            cam_output.link(encoder.input)

            self._mjpeg_queue = encoder.bitstream.createOutputQueue()
            self._mjpeg_queue.setMaxSize(2)
            self._mjpeg_queue.setBlocking(False)

            self._control_input = cam.inputControl.createInputQueue()

            # Optional: on-camera detection output placeholder
            if self._inference_mode == InferenceMode.on_camera:
                try:
                    det = pipeline.create(dai.node.DetectionNetwork)
                    det.build(cam, dai.NNModelDescription("yolov6-nano"))
                    self._det_queue = det.out.createOutputQueue()
                    self._det_queue.setMaxSize(2)
                    self._det_queue.setBlocking(False)
                except Exception as exc:
                    logger.warning("Could not add on-camera detection node: %s", exc)
                    self._det_queue = None
            else:
                self._det_queue = None
        else:
            self._mjpeg_queue = None
            self._control_input = None
            self._det_queue = None

        # --- Stereo cameras (left/right) ---
        need_stereo = self._stereo_mode in (StereoMode.stereo_only, StereoMode.both)
        if need_stereo:
            try:
                # Left camera (CAM_B)
                left_cam = pipeline.create(dai.node.Camera)
                left_cam.build(dai.CameraBoardSocket.CAM_B)
                left_encoder = pipeline.create(dai.node.VideoEncoder)
                left_encoder.setDefaultProfilePreset(
                    fps, dai.VideoEncoderProperties.Profile.MJPEG
                )
                left_encoder.setQuality(quality)
                left_output = left_cam.requestOutput(
                    size=STEREO_RESOLUTION,
                    type=dai.ImgFrame.Type.NV12,
                    fps=fps,
                )
                left_output.link(left_encoder.input)
                self._left_mjpeg_queue = left_encoder.bitstream.createOutputQueue()
                self._left_mjpeg_queue.setMaxSize(2)
                self._left_mjpeg_queue.setBlocking(False)

                # Right camera (CAM_C)
                right_cam = pipeline.create(dai.node.Camera)
                right_cam.build(dai.CameraBoardSocket.CAM_C)
                right_encoder = pipeline.create(dai.node.VideoEncoder)
                right_encoder.setDefaultProfilePreset(
                    fps, dai.VideoEncoderProperties.Profile.MJPEG
                )
                right_encoder.setQuality(quality)
                right_output = right_cam.requestOutput(
                    size=STEREO_RESOLUTION,
                    type=dai.ImgFrame.Type.NV12,
                    fps=fps,
                )
                right_output.link(right_encoder.input)
                self._right_mjpeg_queue = right_encoder.bitstream.createOutputQueue()
                self._right_mjpeg_queue.setMaxSize(2)
                self._right_mjpeg_queue.setBlocking(False)

                logger.info("Stereo cameras enabled for %s", self.id)
            except Exception as exc:
                logger.warning("Could not initialise stereo cameras on %s: %s", self.id, exc)
                self._left_mjpeg_queue = None
                self._right_mjpeg_queue = None
        else:
            self._left_mjpeg_queue = None
            self._right_mjpeg_queue = None

        # --- IMU (accelerometer for drift detection) ---
        try:
            imu = pipeline.create(dai.node.IMU)
            imu.enableIMUSensor(dai.IMUSensor.ACCELEROMETER_RAW, 10)  # 10 Hz — sufficient for angle estimation
            imu.setBatchReportThreshold(5)   # report every 5 samples → 2 Hz host delivery
            imu.setMaxBatchReports(10)
            self._imu_queue = imu.out.createOutputQueue()
            self._imu_queue.setMaxSize(4)
            self._imu_queue.setBlocking(False)
            logger.debug("IMU node enabled for %s", self.id)
        except Exception as exc:
            logger.warning("Could not enable IMU on %s: %s", self.id, exc)
            self._imu_queue = None

        return pipeline

    def _run(self) -> None:
        logger.info("Camera worker starting: %s", self.id)
        _MAX_RETRIES = 10
        _RETRY_DELAYS = [2, 4, 8, 16, 30, 30, 30, 30, 30, 30]  # seconds, one per retry

        for attempt in range(_MAX_RETRIES + 1):
            if self._stop_event.is_set():
                break

            device: Optional[dai.Device] = None
            pipeline: Optional[dai.Pipeline] = None

            # Reset per-pipeline state so stale queue handles are never used
            self._mjpeg_queue = None
            self._left_mjpeg_queue = None
            self._right_mjpeg_queue = None
            self._det_queue = None
            self._imu_queue = None
            self._control_input = None
            self._frame_width = 0
            self._frame_height = 0
            self._fps_counter = 0

            try:
                device = dai.Device(self.device_info)
                self._device = device
                self._connected = True

                try:
                    self.ip = device.getDeviceInfo().name
                except Exception:
                    pass

                pipeline = self._build_pipeline(device)
                pipeline.start()

                logger.info(
                    "Camera %s connected (IP: %s)%s",
                    self.id, self.ip,
                    f" [retry {attempt}]" if attempt else "",
                )
                self._fps_last_time = time.monotonic()

                # Re-assert the last-applied control so focus/exposure/WB survive
                # pipeline rebuilds. Without this, the OAK-D defaults back to
                # continuous autofocus after every stream-settings change.
                if self._last_control is not None and self._control_input is not None:
                    try:
                        self.apply_control(self._last_control)
                    except Exception as exc:
                        logger.debug(
                            "Camera %s: failed to re-assert last control after restart: %s",
                            self.id[:8], exc,
                        )

                while not self._stop_event.is_set():
                    self._process_frame()
                    self._process_stereo_frames()
                    self._process_detections()
                    self._process_imu()

                # Clean exit — stop_event was set explicitly (disable/shutdown)
                break

            except Exception as exc:
                if self._stop_event.is_set():
                    break
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning(
                        "Camera %s pipeline error (attempt %d/%d, retrying in %ds): %s",
                        self.id, attempt + 1, _MAX_RETRIES + 1, delay, exc,
                    )
                else:
                    logger.error(
                        "Camera %s pipeline error — max retries reached, giving up: %s",
                        self.id, exc, exc_info=True,
                    )
            finally:
                self._connected = False
                if pipeline:
                    try:
                        pipeline.stop()
                    except Exception:
                        pass
                if device:
                    try:
                        device.close()
                    except Exception:
                        pass
                self._device = None

            # Interruptible delay before next attempt
            if attempt < _MAX_RETRIES and not self._stop_event.is_set():
                self._stop_event.wait(timeout=_RETRY_DELAYS[attempt])

        logger.info("Camera worker stopped: %s", self.id)

    def _process_frame(self) -> None:
        if self._mjpeg_queue is None:
            return
        try:
            pkt = self._mjpeg_queue.tryGet()
            if pkt is None:
                time.sleep(0.001)
                return
            data = bytes(pkt.getData())

            # Rotate 180° for ceiling-mounted cameras
            if self._flip_180:
                try:
                    import cv2
                    arr = np.frombuffer(data, dtype=np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if img is not None:
                        img = cv2.rotate(img, cv2.ROTATE_180)
                        _, buf = cv2.imencode(
                            '.jpg', img,
                            [cv2.IMWRITE_JPEG_QUALITY, self._mjpeg_quality],
                        )
                        data = buf.tobytes()
                except Exception as exc:
                    logger.debug("Rotation error on %s: %s", self.id, exc)

            self.frame_buffer.put(data)

            # Detect frame dimensions once from JPEG header
            if self._frame_width == 0 and len(data) > 4:
                try:
                    import cv2
                    arr = np.frombuffer(data, dtype=np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if img is not None:
                        self._frame_height, self._frame_width = img.shape[:2]
                except Exception:
                    pass

            # FPS tracking
            self._fps_counter += 1
            now = time.monotonic()
            elapsed = now - self._fps_last_time
            if elapsed >= 1.0:
                self._fps = self._fps_counter / elapsed
                self._fps_counter = 0
                self._fps_last_time = now

            # Latency: host-synced packet timestamp vs current monotonic time
            try:
                pkt_ts = pkt.getTimestamp().total_seconds()
                latency = (time.monotonic() - pkt_ts) * 1000
                # Only use if the clocks are reasonably synced (< 10s)
                if 0 <= latency < 10_000:
                    self._latency_ms = latency
            except Exception:
                pass

            # Host-side inference
            if self._inference_mode == InferenceMode.host and self._host_model:
                self._run_host_inference(data)

            # Feed recording worker (main camera)
            if self.recording_worker and self._recording:
                self.recording_worker.feed(data)

        except Exception as exc:
            logger.debug("Frame processing error on %s: %s", self.id, exc)

    def _process_stereo_frames(self) -> None:
        """Pull frames from left/right stereo queues and feed to buffers + recording."""
        for queue, buf, side in [
            (self._left_mjpeg_queue, self.left_frame_buffer, "left"),
            (self._right_mjpeg_queue, self.right_frame_buffer, "right"),
        ]:
            if queue is None:
                continue
            try:
                pkt = queue.tryGet()
                if pkt is None:
                    continue
                data = bytes(pkt.getData())

                # Rotate 180° for ceiling-mounted cameras
                if self._flip_180:
                    try:
                        import cv2
                        arr = np.frombuffer(data, dtype=np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if img is not None:
                            img = cv2.rotate(img, cv2.ROTATE_180)
                            _, enc = cv2.imencode(
                                '.jpg', img,
                                [cv2.IMWRITE_JPEG_QUALITY, self._mjpeg_quality],
                            )
                            data = enc.tobytes()
                    except Exception as exc:
                        logger.debug("Stereo rotation error on %s/%s: %s", self.id, side, exc)

                buf.put(data)
                # Feed stereo to recording worker
                if self.recording_worker and self._recording:
                    if side == "left":
                        self.recording_worker.feed_left(data)
                    else:
                        self.recording_worker.feed_right(data)
            except Exception as exc:
                logger.debug("Stereo %s frame error on %s: %s", side, self.id, exc)

    def _process_detections(self) -> None:
        if self._det_queue is None:
            return
        try:
            det_msg = self._det_queue.tryGet()
            if det_msg is None:
                return
            boxes = []
            for d in det_msg.detections:
                boxes.append(
                    BoundingBox(
                        x1=d.xmin,
                        y1=d.ymin,
                        x2=d.xmax,
                        y2=d.ymax,
                        confidence=d.confidence,
                        class_id=d.label,
                        label=str(d.label),
                    )
                )
            detection = Detection(
                camera_id=self.id,
                timestamp=time.time(),
                boxes=boxes,
                inference_mode=InferenceMode.on_camera,
            )
            self.detection_buffer.put(detection)
        except Exception as exc:
            logger.debug("Detection processing error on %s: %s", self.id, exc)

    def _process_imu(self) -> None:
        """Drain the IMU queue and update imu_buffer with the latest angles."""
        if self._imu_queue is None:
            return
        try:
            imu_data = self._imu_queue.tryGet()
            if imu_data is None:
                return
            for packet in imu_data.packets:
                accel = packet.acceleroMeter
                roll_deg, pitch_deg = _compute_imu_angles(accel.x, accel.y, accel.z)
                self.imu_buffer.put(roll_deg, pitch_deg)
        except Exception as exc:
            logger.debug("IMU processing error on %s: %s", self.id, exc)

    def get_imu_angle(self) -> tuple[float, float] | None:
        """Return (roll_deg, pitch_deg) from the latest IMU reading, or None if no data."""
        roll, pitch, has_data = self.imu_buffer.get()
        return (roll, pitch) if has_data else None

    def _run_host_inference(self, jpeg_bytes: bytes) -> None:
        try:
            import cv2
            arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return
            results = self._host_model(frame, verbose=False)
            boxes = []
            for box in results[0].boxes:
                cls_id = int(box.cls[0])
                label = results[0].names.get(cls_id, str(cls_id))
                xyxy = box.xyxy[0].tolist()
                # normalize to 0-1
                h, w = frame.shape[:2]
                boxes.append(
                    BoundingBox(
                        x1=xyxy[0] / w,
                        y1=xyxy[1] / h,
                        x2=xyxy[2] / w,
                        y2=xyxy[3] / h,
                        confidence=float(box.conf[0]),
                        class_id=cls_id,
                        label=label,
                    )
                )
            detection = Detection(
                camera_id=self.id,
                timestamp=time.time(),
                boxes=boxes,
                inference_mode=InferenceMode.host,
            )
            self.detection_buffer.put(detection)
        except Exception as exc:
            logger.debug("Host inference error on %s: %s", self.id, exc)

    # ------------------------------------------------------------------
    # Camera control
    # ------------------------------------------------------------------

    def apply_control(self, req: CameraControlRequest) -> None:
        if not self._control_input:
            raise RuntimeError("Camera not connected or main camera not active")
        ctrl = dai.CameraControl()

        if req.auto_exposure is True:
            ctrl.setAutoExposureEnable()
        elif req.auto_exposure is False and req.exposure_us and req.iso:
            ctrl.setManualExposure(req.exposure_us, req.iso)

        if req.auto_focus is True:
            ctrl.setAutoFocusMode(dai.CameraControl.AutoFocusMode.CONTINUOUS_VIDEO)
        elif req.auto_focus is False and req.manual_focus is not None:
            ctrl.setManualFocus(req.manual_focus)

        if req.auto_white_balance is True:
            ctrl.setAutoWhiteBalanceMode(dai.CameraControl.AutoWhiteBalanceMode.AUTO)
        elif req.auto_white_balance is False and req.white_balance_k:
            ctrl.setManualWhiteBalance(req.white_balance_k)

        if req.brightness is not None:
            ctrl.setBrightness(req.brightness)
        if req.contrast is not None:
            ctrl.setContrast(req.contrast)
        if req.saturation is not None:
            ctrl.setSaturation(req.saturation)
        if req.sharpness is not None:
            ctrl.setSharpness(req.sharpness)
        if req.luma_denoise is not None:
            ctrl.setLumaDenoise(req.luma_denoise)
        if req.chroma_denoise is not None:
            ctrl.setChromaDenoise(req.chroma_denoise)

        self._control_input.send(ctrl)
        self._last_control = req

    def capture_snapshot(self) -> bytes:
        """Return the latest JPEG frame bytes."""
        data, _ = self.frame_buffer.get()
        if not data:
            raise RuntimeError("No frame available yet")
        return data

    def capture_stereo_snapshot(self, side: str) -> bytes:
        """Return the latest stereo JPEG frame bytes ('left' or 'right')."""
        buf = self.left_frame_buffer if side == "left" else self.right_frame_buffer
        data, _ = buf.get()
        if not data:
            raise RuntimeError(f"No {side} stereo frame available")
        return data

    # ------------------------------------------------------------------
    # Stream settings (requires pipeline rebuild)
    # ------------------------------------------------------------------

    def update_stream_settings(self, req: StreamSettingsRequest) -> None:
        """Update per-camera stream settings and restart the pipeline if needed."""
        changed = False
        if req.fps is not None and req.fps != self._stream_fps:
            self._stream_fps = max(1, min(60, req.fps))
            changed = True
        if req.mjpeg_quality is not None and req.mjpeg_quality != self._mjpeg_quality:
            self._mjpeg_quality = max(1, min(100, req.mjpeg_quality))
            changed = True
        if req.resolution is not None and req.resolution != self._resolution:
            if req.resolution in RESOLUTION_PRESETS:
                self._resolution = req.resolution
                changed = True
        if req.stereo_mode is not None and req.stereo_mode != self._stereo_mode:
            self._stereo_mode = req.stereo_mode
            changed = True

        # flip_180 is post-processing only — no pipeline restart needed
        if req.flip_180 is not None and req.flip_180 != self._flip_180:
            self._flip_180 = req.flip_180
            logger.info(
                "Camera %s flip_180 set to %s",
                self.id, self._flip_180,
            )

        if changed:
            logger.info(
                "Stream settings changed for %s: fps=%d quality=%d res=%s stereo=%s — restarting",
                self.id, self._stream_fps, self._mjpeg_quality,
                self._resolution, self._stereo_mode.value,
            )
            # Reset frame dimensions so they get re-detected
            self._frame_width = 0
            self._frame_height = 0
            self.stop()
            self.start()

    # ------------------------------------------------------------------
    # Enable / disable (stop pipeline to free PoE bandwidth)
    # ------------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable this camera's stream.

        Disabling stops the pipeline and frees PoE bandwidth.
        Enabling restarts it.
        """
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            logger.info("Enabling camera %s — starting pipeline", self.id)
            self.start()
        else:
            logger.info("Disabling camera %s — stopping pipeline to free bandwidth", self.id)
            self.stop()

    # ------------------------------------------------------------------
    # Inference mode switching
    # ------------------------------------------------------------------

    def set_inference_mode(self, mode: InferenceMode, model_path: str | None = None) -> None:
        self._inference_mode = mode

        if mode == InferenceMode.host:
            self._load_host_model(model_path or settings.host_yolo_model)
        elif mode == InferenceMode.none:
            self._host_model = None
        # on_camera requires pipeline rebuild — restart worker
        elif mode == InferenceMode.on_camera:
            self._host_model = None
            self.stop()
            self.start()

    def _load_host_model(self, model_path: str) -> None:
        if self._host_model_path == model_path:
            return
        try:
            from ultralytics import YOLO
            self._host_model = YOLO(model_path)
            self._host_model_path = model_path
            logger.info("Loaded host model %s for camera %s", model_path, self.id)
        except ImportError:
            raise RuntimeError(
                "ultralytics not installed. Install with: "
                "uv pip install 'oak-d-streaming[host-inference]'"
            )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def status(self) -> CameraStatus:
        return CameraStatus(
            id=self.id,
            name=f"OAK-{self.id[:8]}",
            connected=self._connected,
            enabled=self._enabled,
            ip=self.ip,
            fps=round(self._fps, 1),
            latency_ms=round(self._latency_ms, 1),
            recording=self._recording,
            recording_mode=self._recording_mode,
            inference_mode=self._inference_mode,
            width=self._frame_width,
            height=self._frame_height,
            stereo_mode=self._stereo_mode,
            stream_fps=self._stream_fps,
            mjpeg_quality=self._mjpeg_quality,
            resolution=self._resolution,
            flip_180=self._flip_180,
        )


class CameraManager:
    """Discovers OAK devices and owns a CameraWorker per device."""

    def __init__(self) -> None:
        self._workers: dict[str, CameraWorker] = {}
        self._lock = threading.Lock()

    def discover(self) -> list[str]:
        """Scan for available OAK devices and start workers for new ones."""
        found_ids: list[str] = []
        try:
            device_infos = dai.Device.getAllAvailableDevices()
        except Exception as exc:
            logger.error("Device discovery failed: %s", exc)
            return []

        with self._lock:
            for info in device_infos:
                mx_id = info.getDeviceId()
                found_ids.append(mx_id)
                if mx_id not in self._workers:
                    worker = CameraWorker(info)
                    self._workers[mx_id] = worker
                    worker.start()
                    logger.info("Started worker for camera %s", mx_id)

        logger.info("Discovered %d device(s): %s", len(found_ids), found_ids)
        return found_ids

    def get_worker(self, camera_id: str) -> CameraWorker:
        with self._lock:
            if camera_id not in self._workers:
                raise KeyError(f"Camera {camera_id!r} not found")
            return self._workers[camera_id]

    def all_workers(self) -> list[CameraWorker]:
        with self._lock:
            return list(self._workers.values())

    def all_statuses(self) -> list[CameraStatus]:
        return [w.status for w in self.all_workers()]

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.stop()
        with self._lock:
            self._workers.clear()

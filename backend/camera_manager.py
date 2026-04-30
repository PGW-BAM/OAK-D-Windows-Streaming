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

        # Set the first time a frame reaches frame_buffer after each pipeline
        # (re)build. Lets external orchestrators wait for "pipeline rebuilt and
        # producing frames" instead of guessing with sleeps.
        self._first_frame_event = threading.Event()

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

        # Live auto-resolved values reported on each frame's metadata. Lets the
        # UI snapshot the auto-converged values when the user toggles auto off.
        self._live_lens_position: Optional[int] = None
        self._live_exposure_us: Optional[int] = None
        self._live_iso: Optional[int] = None
        self._live_color_temp_k: Optional[int] = None

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
        """Block until the worker thread has fully released the device.

        Returning while the thread is still alive lets ``start()`` open a
        second ``dai.Device`` handle to the same physical OAK-D. The two
        sessions race inside DeviceGate; the loser triggers
        ``dai::DeviceBase::~DeviceBase`` → ``terminate()`` → ``abort()``,
        which crashes the entire backend process. So we wait — however
        long it takes — for the worker to finish ``pipeline.stop()`` and
        ``device.close()``. Progress is logged in 10 s chunks so a stuck
        teardown is visible in the log.
        """
        self._stop_event.set()
        if self._thread:
            waited = 0
            while self._thread.is_alive():
                self._thread.join(timeout=10)
                waited += 10
                if self._thread.is_alive():
                    logger.warning(
                        "Camera %s worker still tearing down after %ds — "
                        "waiting (do not interrupt)",
                        self.id[:8], waited,
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
            if self._flip_180:
                # On-device 180° rotation. Doing this on the host
                # (cv2.imdecode → rotate → imencode) costs ~100-150 ms per
                # 4K frame, which collapses recording fps to ~7 and produces
                # 1-2 s clips out of a 5 s window. Pushing it to the ISP is
                # essentially free.
                try:
                    cam.setImageOrientation(dai.CameraImageOrientation.ROTATE_180_DEG)
                except Exception as exc:
                    logger.warning(
                        "Camera %s: setImageOrientation failed (%s) — "
                        "falling back to host-side rotation",
                        self.id[:8], exc,
                    )

            encoder = pipeline.create(dai.node.VideoEncoder)
            encoder.setDefaultProfilePreset(
                fps, dai.VideoEncoderProperties.Profile.MJPEG
            )
            encoder.setQuality(quality)
            # Bump the device-side frame pool so the encoder always has a
            # free buffer ready when a new camera frame arrives. With the
            # default (4) at 4K/29 or 1080p/59 the encoder can stall briefly,
            # producing duplicate output packets that show up as "stuck"
            # frames in the recording.
            try:
                encoder.setNumFramesPool(8)
            except AttributeError:
                pass  # older DepthAI versions
            cam_output = cam.requestOutput(
                size=res,
                type=dai.ImgFrame.Type.NV12,
                fps=fps,
            )
            # On-device link only — the NV12 raw frame is consumed by the
            # encoder inside the camera; the host receives only the MJPEG
            # bitstream so PoE bandwidth scales with quality, not raw size.
            cam_output.link(encoder.input)

            self._mjpeg_queue = encoder.bitstream.createOutputQueue()
            # Larger host queue + non-blocking: lets the encoder keep
            # producing without back-pressuring the device pipeline. If the
            # host falls behind, oldest frames are dropped on the host side
            # rather than stalling the camera/encoder.
            self._mjpeg_queue.setMaxSize(30)
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
                if self._flip_180:
                    try:
                        left_cam.setImageOrientation(dai.CameraImageOrientation.ROTATE_180_DEG)
                    except Exception:
                        pass
                left_encoder = pipeline.create(dai.node.VideoEncoder)
                left_encoder.setDefaultProfilePreset(
                    fps, dai.VideoEncoderProperties.Profile.MJPEG
                )
                left_encoder.setQuality(quality)
                try:
                    left_encoder.setNumFramesPool(8)
                except AttributeError:
                    pass
                left_output = left_cam.requestOutput(
                    size=STEREO_RESOLUTION,
                    type=dai.ImgFrame.Type.NV12,
                    fps=fps,
                )
                left_output.link(left_encoder.input)
                self._left_mjpeg_queue = left_encoder.bitstream.createOutputQueue()
                self._left_mjpeg_queue.setMaxSize(30)
                self._left_mjpeg_queue.setBlocking(False)

                # Right camera (CAM_C)
                right_cam = pipeline.create(dai.node.Camera)
                right_cam.build(dai.CameraBoardSocket.CAM_C)
                if self._flip_180:
                    try:
                        right_cam.setImageOrientation(dai.CameraImageOrientation.ROTATE_180_DEG)
                    except Exception:
                        pass
                right_encoder = pipeline.create(dai.node.VideoEncoder)
                right_encoder.setDefaultProfilePreset(
                    fps, dai.VideoEncoderProperties.Profile.MJPEG
                )
                right_encoder.setQuality(quality)
                try:
                    right_encoder.setNumFramesPool(8)
                except AttributeError:
                    pass
                right_output = right_cam.requestOutput(
                    size=STEREO_RESOLUTION,
                    type=dai.ImgFrame.Type.NV12,
                    fps=fps,
                )
                right_output.link(right_encoder.input)
                self._right_mjpeg_queue = right_encoder.bitstream.createOutputQueue()
                self._right_mjpeg_queue.setMaxSize(30)
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
            self._first_frame_event.clear()

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
                    t0 = time.monotonic()
                    try:
                        pipeline.stop()
                    except Exception as exc:
                        logger.warning(
                            "Camera %s pipeline.stop() raised: %s",
                            self.id[:8], exc,
                        )
                    dt = time.monotonic() - t0
                    logger.info(
                        "Camera %s pipeline.stop() took %.2fs",
                        self.id[:8], dt,
                    )
                if device:
                    t0 = time.monotonic()
                    try:
                        device.close()
                    except Exception as exc:
                        logger.warning(
                            "Camera %s device.close() raised: %s",
                            self.id[:8], exc,
                        )
                    dt = time.monotonic() - t0
                    logger.info(
                        "Camera %s device.close() took %.2fs",
                        self.id[:8], dt,
                    )
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

            # Capture sensor metadata so the UI can snapshot the
            # auto-converged values when the user toggles an auto mode off.
            try:
                lens = pkt.getLensPosition()
                if isinstance(lens, int) and lens >= 0:
                    self._live_lens_position = lens
                exp = pkt.getExposureTime()
                exp_us = int(exp.total_seconds() * 1_000_000) if exp else 0
                if exp_us > 0:
                    self._live_exposure_us = exp_us
                iso = pkt.getSensitivity()
                if isinstance(iso, int) and iso > 0:
                    self._live_iso = iso
                ct = pkt.getColorTemperature()
                if isinstance(ct, int) and ct > 0:
                    self._live_color_temp_k = ct
            except Exception:
                pass

            # Rotation for ceiling-mounted cameras is now performed on the
            # device's ISP (see _build_pipeline / setImageOrientation), so
            # the host receives already-oriented frames at full fps.

            self.frame_buffer.put(data)
            if not self._first_frame_event.is_set():
                self._first_frame_event.set()

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
                # Pass through device timestamp + sequence number so the muxer
                # can assign accurate, monotonically-increasing PTS based on
                # capture time — not host dequeue time. Avoids "stuck" videos
                # caused by burst dequeues collapsing PTS into the same ms.
                ts_s: float | None = None
                seq: int | None = None
                try:
                    ts = pkt.getTimestampDevice()
                    if ts is not None:
                        ts_s = ts.total_seconds()
                except Exception:
                    pass
                try:
                    seq = int(pkt.getSequenceNum())
                except Exception:
                    pass
                self.recording_worker.feed(data, capture_ts_s=ts_s, seq_num=seq)

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

                # Stereo rotation is also handled on-device via
                # setImageOrientation; see _build_pipeline.

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

    def get_live_auto_values(self) -> dict[str, int | None]:
        """Return the most recent sensor-reported auto values.

        These are populated from each incoming frame's camera metadata, so
        they reflect what the auto modes (AE/AF/AWB) have converged to.
        Useful for snapshotting the values when the user toggles auto off.
        """
        return {
            "manual_focus": self._live_lens_position,
            "exposure_us": self._live_exposure_us,
            "iso": self._live_iso,
            "white_balance_k": self._live_color_temp_k,
        }

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

    def wait_for_ready(self, timeout_s: float = 15.0) -> bool:
        """Block until the worker produces its first frame after a (re)build.

        Returns True on success, False on timeout.
        """
        return self._first_frame_event.wait(timeout_s)

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

        # flip_180 is now handled on-device (cam.setImageOrientation), so a
        # change requires a pipeline rebuild to apply.
        if req.flip_180 is not None and req.flip_180 != self._flip_180:
            self._flip_180 = req.flip_180
            changed = True
            logger.info(
                "Camera %s flip_180 set to %s — pipeline restart required",
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
        # Physical cam_id mapping: mx_id -> "cam1"/"cam2".
        # Resolved once per session from IMU roll sign (cam1 = upside-down /
        # negative roll, cam2 = right-side-up / positive roll) so Dashboard
        # widgets keyed on cam_id stay tied to the correct physical camera
        # across restarts regardless of DepthAI enumeration order.
        self._cam_id_by_mxid: dict[str, str] = {}
        self._mapping_resolved: bool = False

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

    # ------------------------------------------------------------------
    # Physical cam_id resolution (roll-sign based)
    # ------------------------------------------------------------------

    def resolve_cam_ids_by_roll(
        self,
        timeout_s: float = 8.0,
        samples_per_worker: int = 5,
        sample_interval_s: float = 0.3,
    ) -> dict[str, str]:
        """Assign cam1/cam2 based on physical IMU roll sign.

        Cam1 is mounted upside-down so its raw accelerometer-derived roll is
        negative; cam2 is right-side-up so its raw roll is positive. Discovery
        order from `dai.Device.getAllAvailableDevices()` is non-deterministic,
        so we use this physical signal instead to make cam_ids stable across
        restarts.

        Falls back to discovery order if IMU data is unavailable or both
        workers report the same sign (e.g. one camera running, ambiguous mount).
        The resolved mapping is cached for the lifetime of the manager.
        """
        workers = self.all_workers()
        mapping: dict[str, str] = {}

        # Wait for IMU data from every connected worker (bounded by timeout).
        deadline = time.monotonic() + timeout_s
        ready: list[CameraWorker] = []
        while time.monotonic() < deadline:
            ready = [w for w in workers if w._connected and w.get_imu_angle() is not None]
            if len(ready) == len(workers) and ready:
                break
            time.sleep(0.2)

        # Collect a small window of raw roll samples per worker and average.
        roll_by_mxid: dict[str, float] = {}
        for _ in range(samples_per_worker):
            for w in ready:
                angle = w.get_imu_angle()
                if angle is None:
                    continue
                roll_by_mxid.setdefault(w.id, 0.0)
                roll_by_mxid[w.id] += angle[0]
            time.sleep(sample_interval_s)
        for mxid in list(roll_by_mxid.keys()):
            roll_by_mxid[mxid] /= samples_per_worker

        def _fallback_mapping() -> dict[str, str]:
            return {w.id: f"cam{i + 1}" for i, w in enumerate(workers)}

        if not roll_by_mxid:
            logger.warning(
                "cam_id auto-detection: no IMU data within %.1fs — falling back to discovery order",
                timeout_s,
            )
            mapping = _fallback_mapping()
        elif len(roll_by_mxid) == 1:
            mxid, roll = next(iter(roll_by_mxid.items()))
            cam_id = "cam1" if roll < 0 else "cam2"
            mapping[mxid] = cam_id
            # Remaining workers (if any joined late) get discovery-order fallback.
            for i, w in enumerate(workers):
                mapping.setdefault(w.id, f"cam{i + 1}")
            logger.info(
                "cam_id auto-detection: single camera — %s (mean roll=%.2f°) -> %s",
                mxid[:8], roll, cam_id,
            )
        elif len(roll_by_mxid) >= 2:
            items = sorted(roll_by_mxid.items(), key=lambda kv: kv[1])
            neg_mxid, neg_roll = items[0]
            pos_mxid, pos_roll = items[-1]
            if neg_roll < 0 < pos_roll:
                mapping[neg_mxid] = "cam1"
                mapping[pos_mxid] = "cam2"
                # Any extra cameras beyond the two extremes keep discovery order.
                leftover_idx = 3
                for w in workers:
                    if w.id not in mapping:
                        mapping[w.id] = f"cam{leftover_idx}"
                        leftover_idx += 1
                logger.info(
                    "cam_id auto-detection: %s (roll=%.2f°) -> cam1, %s (roll=%.2f°) -> cam2",
                    neg_mxid[:8], neg_roll, pos_mxid[:8], pos_roll,
                )
            else:
                logger.warning(
                    "cam_id auto-detection: rolls have same sign "
                    "(%s=%.2f°, %s=%.2f°) — falling back to discovery order",
                    neg_mxid[:8], neg_roll, pos_mxid[:8], pos_roll,
                )
                mapping = _fallback_mapping()

        with self._lock:
            self._cam_id_by_mxid = mapping
            self._mapping_resolved = True
        return dict(mapping)

    def get_cam_id(self, worker: CameraWorker) -> str:
        """Return the logical cam_id for a worker.

        Uses the roll-sign mapping resolved at startup. Falls back to
        discovery-order positioning for workers not in the mapping (e.g. a
        camera that reconnected after initial resolution).
        """
        with self._lock:
            cam_id = self._cam_id_by_mxid.get(worker.id)
            if cam_id is not None:
                return cam_id
            workers = list(self._workers.values())
        idx = next((i for i, w in enumerate(workers) if w.id == worker.id), 0)
        return f"cam{idx + 1}"

    def get_worker_by_cam_id(self, cam_id: str) -> CameraWorker | None:
        """Reverse lookup: logical cam_id -> CameraWorker (or None)."""
        with self._lock:
            for mxid, cid in self._cam_id_by_mxid.items():
                if cid == cam_id and mxid in self._workers:
                    return self._workers[mxid]
            # Fallback: parse numeric index from cam_id for unmapped workers.
            try:
                idx = int(cam_id.replace("cam", "")) - 1
            except ValueError:
                return None
            workers = list(self._workers.values())
        if 0 <= idx < len(workers):
            return workers[idx]
        return None

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.stop()
        with self._lock:
            self._workers.clear()
            self._cam_id_by_mxid.clear()
            self._mapping_resolved = False

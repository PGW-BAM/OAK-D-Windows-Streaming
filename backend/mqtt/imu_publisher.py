"""IMU publisher — reads OAK-D camera IMU data and publishes angles over MQTT.

Two publishing modes:
  1. Background loop (~2 Hz): publishes IMUAngle for all connected cameras with
     request_id=None. Used by the Pi GUI for live roll/pitch display.

  2. Request/response: listens for IMUCheckRequest on cmd/cam/+/imu_check.
     Responds immediately with a fresh IMUAngle echoing the request_id so
     the Pi's DriftDetector can resolve its pending Future.

cam_id → worker mapping is resolved by CameraManager from the IMU roll
sign at startup (cam1 = upside-down / negative roll, cam2 = right-side-up
/ positive roll). This keeps the Dashboard angle widgets tied to the
correct physical camera across restarts regardless of DepthAI
enumeration order.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .models import IMUAngle, IMUCheckRequest
from .topics import Topics

if TYPE_CHECKING:
    from backend.camera_manager import CameraManager, CameraWorker
    from .client import MqttClient

logger = logging.getLogger(__name__)

# Background publish interval in seconds
_PUBLISH_INTERVAL_S = 0.5   # 2 Hz


class ImuPublisher:
    """Reads IMU angles from camera workers and publishes them to MQTT.

    Lifecycle:
        publisher = ImuPublisher(mqtt_client, camera_manager)
        publisher.register_handlers(mqtt_client)   # before client.start()
        await publisher.start()                     # after client.start()
        ...
        await publisher.stop()
    """

    def __init__(self, mqtt: MqttClient, camera_manager: CameraManager) -> None:
        self._mqtt = mqtt
        self._camera_manager = camera_manager
        self._running = False
        self._bg_task: asyncio.Task | None = None

    def register_handlers(self, mqtt: MqttClient) -> None:
        """Register the imu_check subscription handler. Call before client.start()."""
        mqtt.on(Topics.CMD_IMU_CHECK_ALL, self._handle_imu_check)

    async def start(self) -> None:
        """Start the background 2 Hz publish loop."""
        self._running = True
        self._bg_task = asyncio.create_task(
            self._publish_loop(), name="imu-background-publisher"
        )
        logger.info("ImuPublisher started (interval=%.1fs)", _PUBLISH_INTERVAL_S)

    async def stop(self) -> None:
        """Stop the background loop."""
        self._running = False
        if self._bg_task:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
        logger.info("ImuPublisher stopped")

    # ──────────────────────────────────────────────
    # Background publish loop
    # ──────────────────────────────────────────────

    async def _publish_loop(self) -> None:
        """Publish IMUAngle for every connected camera at ~2 Hz."""
        while self._running:
            try:
                for worker in self._camera_manager.all_workers():
                    if not worker._connected:
                        continue
                    cam_id = self._camera_manager.get_cam_id(worker)
                    await self._publish_angle(worker, cam_id, request_id=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("IMU background publish error: %s", exc)

            await asyncio.sleep(_PUBLISH_INTERVAL_S)

    # ──────────────────────────────────────────────
    # Request/response handler
    # ──────────────────────────────────────────────

    async def _handle_imu_check(self, topic_str: str, payload: dict[str, Any]) -> None:
        """Handle cmd/cam/{cam_id}/imu_check — publish a fresh IMUAngle immediately."""
        try:
            req = IMUCheckRequest.model_validate(payload)
        except Exception as exc:
            logger.warning("Invalid IMUCheckRequest payload: %s — %s", payload, exc)
            return

        cam_id = req.cam_id
        worker = self._worker_for_cam_id(cam_id)

        if worker is None:
            logger.warning(
                "IMU check request for %s — no worker found (connected cameras: %d)",
                cam_id,
                len(self._camera_manager.all_workers()),
            )
            return

        if not worker._connected:
            logger.warning(
                "IMU check request for %s — camera not connected", cam_id
            )
            return

        await self._publish_angle(worker, cam_id, request_id=req.request_id)
        logger.debug(
            "IMU check response sent for %s (checkpoint=%s, request_id=%s)",
            cam_id,
            req.checkpoint_name,
            req.request_id[:8],
        )

    # ──────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────

    async def _publish_angle(
        self,
        worker: CameraWorker,
        cam_id: str,
        request_id: str | None,
    ) -> None:
        """Read the latest IMU angle from a worker and publish it."""
        angle = worker.get_imu_angle()
        if angle is None:
            logger.debug("No IMU data yet for %s — skipping publish", cam_id)
            return

        roll_deg, pitch_deg = angle
        msg = IMUAngle(
            cam_id=cam_id,
            roll_deg=round(roll_deg, 3),
            pitch_deg=round(pitch_deg, 3),
            request_id=request_id,
        )
        await self._mqtt.publish(
            Topics.telemetry_imu(cam_id),
            msg,
            qos=1 if request_id else 0,  # QoS 1 for correlated responses, 0 for background
        )

    def _worker_for_cam_id(self, cam_id: str) -> CameraWorker | None:
        """Return the CameraWorker for the given cam_id, or None if not found."""
        return self._camera_manager.get_worker_by_cam_id(cam_id)

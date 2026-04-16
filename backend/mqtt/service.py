"""MQTT service — top-level coordinator that wires client, monitor, orchestrator, and alerts.

Usage from FastAPI lifespan:
    mqtt_service = MqttService(camera_manager)
    await mqtt_service.start()
    ...
    await mqtt_service.stop()
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .alerts import EmailAlertSender
from .client import MqttClient
from .config import mqtt_settings
from .history import HistoryDB
from .imu_publisher import ImuPublisher
from .models import CameraHealth, WinControllerHealth
from .monitor import ConnectivityMonitor
from .orchestrator import SequenceRunner
from .topics import Topics

if TYPE_CHECKING:
    from backend.angle_targets import AngleTargetManager
    from backend.camera_manager import CameraManager

logger = logging.getLogger(__name__)


class MqttService:
    """Facade that owns the MQTT client and all subsystems."""

    def __init__(
        self,
        camera_manager: CameraManager,
        angle_target_manager: AngleTargetManager | None = None,
    ) -> None:
        self._camera_manager = camera_manager
        self._angle_target_manager = angle_target_manager

        # Core components
        self.client = MqttClient()
        self.history = HistoryDB()
        self.alerts = EmailAlertSender(self.history)
        self.monitor = ConnectivityMonitor(
            self.client,
            self.history,
            on_alert=self._on_alert,
        )
        self.orchestrator = SequenceRunner(
            self.client, camera_manager, angle_target_manager
        )
        self.imu_publisher = ImuPublisher(self.client, camera_manager)

        self._health_task: asyncio.Task | None = None
        self._running = False

    @property
    def is_connected(self) -> bool:
        return self.client.is_connected

    def get_drive_positions(self, cam_id: str) -> dict[str, float | None]:
        """Return the latest cached drive positions for a camera."""
        return self.orchestrator.get_drive_positions(cam_id)

    async def start(self) -> None:
        """Start all MQTT subsystems. Non-blocking — runs in background."""
        logger.info("Starting MQTT service (broker: %s:%d)",
                     mqtt_settings.broker.host, mqtt_settings.broker.port)

        # Open history DB
        await self.history.open()

        # Register all handlers on the MQTT client (before client.start())
        self.monitor.register_handlers(self.client)
        self.client.on(
            Topics.STATUS_DRIVES_ALL,
            self.orchestrator.handle_drive_position,
        )
        self.imu_publisher.register_handlers(self.client)

        # Start client (connects in background with auto-reconnect)
        await self.client.start()

        # Start monitor
        await self.monitor.start()

        # Start IMU background publisher
        await self.imu_publisher.start()

        # Start health beacon publisher
        self._running = True
        self._health_task = asyncio.create_task(
            self._health_loop(), name="win-health-beacon"
        )

        logger.info("MQTT service started")

    async def stop(self) -> None:
        """Gracefully shut down all MQTT subsystems."""
        self._running = False

        # Stop orchestrator if running
        if self.orchestrator.active_sequence:
            await self.orchestrator.stop_sequence()

        # Stop health beacon
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Publish offline health before disconnecting
        try:
            await self.client.publish(
                Topics.health_win(),
                WinControllerHealth(online=False),
            )
        except Exception:
            pass

        await self.imu_publisher.stop()
        await self.monitor.stop()
        await self.client.stop()
        await self.history.close()
        logger.info("MQTT service stopped")

    async def _health_loop(self) -> None:
        """Publish Win controller health beacon at configured interval."""
        interval = mqtt_settings.health_interval_s
        while self._running:
            try:
                workers = self._camera_manager.all_workers()
                connected_ids = [w.id for w in workers if w._connected]

                health = WinControllerHealth(
                    online=True,
                    cameras_connected=connected_ids,
                    active_sequence=(
                        self.orchestrator.active_sequence.sequence_id
                        if self.orchestrator.active_sequence
                        else None
                    ),
                )
                await self.client.publish(Topics.health_win(), health)

                # Also publish per-camera health
                for worker in workers:
                    cam_health = CameraHealth(
                        cam_id=worker.id,
                        online=worker._connected,
                        ip_address=worker.ip,
                        mx_id=worker.id,
                    )
                    await self.client.publish(
                        Topics.health_camera(worker.id), cam_health
                    )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Health beacon error: %s", exc)

            await asyncio.sleep(interval)

    async def _on_alert(self, alert_type: str, component: str, message: str) -> None:
        """Callback from ConnectivityMonitor when an alert fires."""
        state = self.monitor.get_state()
        await self.alerts.send_alert(alert_type, component, message, state)

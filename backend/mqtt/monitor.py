"""Connectivity monitor — tracks health of all components via MQTT heartbeats."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .client import MqttClient
from .config import mqtt_settings
from .history import HistoryDB
from .models import (
    CameraHealth,
    CameraStatusMqtt,
    ConnectivityState,
    DrivePosition,
    PiHealth,
    WinControllerHealth,
)
from .topics import Topics

logger = logging.getLogger(__name__)


class ComponentState:
    """Track last-seen time and state for a single component."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.state: str = "unknown"
        self.last_seen: float = 0.0
        self.details: dict[str, Any] = {}

    @property
    def age_s(self) -> float:
        if self.last_seen == 0:
            return float("inf")
        return time.monotonic() - self.last_seen

    def update(self, state: str, **details: Any) -> None:
        self.state = state
        self.last_seen = time.monotonic()
        self.details.update(details)


class ConnectivityMonitor:
    """Subscribes to health/status topics and maintains a real-time connectivity state.

    Publishes aggregated ConnectivityState to monitoring/connectivity.
    Calls alert callbacks when thresholds are exceeded.
    """

    def __init__(
        self,
        mqtt: MqttClient,
        history: HistoryDB,
        on_alert: Any = None,
    ) -> None:
        self._mqtt = mqtt
        self._history = history
        self._on_alert = on_alert  # async callable(alert_type, component, message)
        self._cfg = mqtt_settings.alerts.thresholds

        # Component states
        self.pi = ComponentState("pi")
        self.broker = ComponentState("broker")
        self.cameras: dict[str, ComponentState] = {}
        self.drives: dict[str, ComponentState] = {}

        # Alert dedup: (alert_type, component) -> last_fired_monotonic
        self._alert_fired: dict[tuple[str, str], float] = {}
        self._alert_count_hour: int = 0
        self._alert_hour_start: float = time.monotonic()

        # Background task
        self._task: asyncio.Task | None = None
        self._stopping = False

    def register_handlers(self, mqtt: MqttClient) -> None:
        """Register all MQTT subscription handlers."""
        mqtt.on(Topics.health_pi(), self._handle_pi_health)
        mqtt.on(Topics.HEALTH_ALL, self._handle_health_all)
        mqtt.on(Topics.STATUS_DRIVES_ALL, self._handle_drive_position)
        mqtt.on(Topics.ERROR_ALL, self._handle_error)

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._monitor_loop(), name="connectivity-monitor")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get_state(self) -> ConnectivityState:
        """Return the current aggregated connectivity state."""
        return ConnectivityState(
            pi_online=self.pi.state == "online" and self.pi.age_s < self._cfg.pi_offline_s,
            broker_connected=self._mqtt.is_connected,
            cameras={
                name: cs.state for name, cs in self.cameras.items()
            },
            drives={
                name: cs.state for name, cs in self.drives.items()
            },
        )

    # ------------------------------------------------------------------
    # MQTT Handlers
    # ------------------------------------------------------------------

    async def _handle_pi_health(self, topic: str, data: dict[str, Any]) -> None:
        try:
            health = PiHealth(**data)
        except Exception:
            return

        prev_state = self.pi.state
        if health.online:
            self.pi.update("online", cpu_temp=health.cpu_temp_c, uptime=health.uptime_s)
            # Update drive states from Pi health beacon
            for key, state in health.drive_states.items():
                if key not in self.drives:
                    self.drives[key] = ComponentState(key)
                self.drives[key].update(state)
        else:
            self.pi.update("offline")

        if prev_state == "online" and self.pi.state == "offline":
            await self._history.log_connectivity("pi", "offline")

    async def _handle_health_all(self, topic: str, data: dict[str, Any]) -> None:
        """Handle health/cameras/{cam_id} messages."""
        parts = topic.split("/")
        if len(parts) >= 3 and parts[1] == "cameras":
            cam_id = parts[2]
            if cam_id not in self.cameras:
                self.cameras[cam_id] = ComponentState(cam_id)
            online = data.get("online", True)
            self.cameras[cam_id].update("online" if online else "offline")

    async def _handle_drive_position(self, topic: str, data: dict[str, Any]) -> None:
        parts = topic.split("/")
        if len(parts) >= 3:
            cam_id = parts[2]
            axis = data.get("drive_axis", "?")
            key = f"{cam_id}:{axis}"
            state = data.get("state", "unknown")
            if key not in self.drives:
                self.drives[key] = ComponentState(key)
            self.drives[key].update(state)

            if state == "fault":
                await self._fire_alert("drive_fault", key, f"Drive {key} fault")

    async def _handle_error(self, topic: str, data: dict[str, Any]) -> None:
        component = data.get("cam_id") or data.get("drive_axis") or "unknown"
        message = data.get("message", "")
        await self._history.log_connectivity(component, "error")
        logger.warning("MQTT error on %s: %s", topic, message)

    # ------------------------------------------------------------------
    # Monitor loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """Periodic check for threshold violations and state publishing."""
        while not self._stopping:
            try:
                await asyncio.sleep(1.0)

                # Check Pi heartbeat
                if self.pi.last_seen > 0 and self.pi.age_s > self._cfg.pi_offline_s:
                    if self.pi.state != "offline":
                        self.pi.state = "offline"
                        await self._history.log_connectivity("pi", "offline")
                    await self._fire_alert(
                        "pi_offline", "pi",
                        f"Pi heartbeat lost for {self.pi.age_s:.0f}s",
                    )

                # Check camera heartbeats
                for cam_id, cs in self.cameras.items():
                    if cs.last_seen > 0 and cs.age_s > self._cfg.camera_offline_s:
                        if cs.state != "offline":
                            cs.state = "offline"
                            await self._history.log_connectivity(cam_id, "offline")
                        await self._fire_alert(
                            "camera_offline", cam_id,
                            f"Camera {cam_id} offline for {cs.age_s:.0f}s",
                        )

                # Check broker
                if not self._mqtt.is_connected:
                    if self.broker.state != "offline":
                        self.broker.update("offline")
                        await self._history.log_connectivity("broker", "offline")
                    await self._fire_alert(
                        "broker_offline", "broker", "MQTT broker connection lost"
                    )
                else:
                    self.broker.update("online")

                # Publish aggregated state
                state = self.get_state()
                await self._mqtt.publish(
                    Topics.monitoring_connectivity(),
                    state,
                    retain=True,
                )

                # Periodic DB cleanup (every ~60 checks = ~1 min)
                if int(time.monotonic()) % 3600 == 0:
                    await self._history.cleanup_old(24)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Monitor loop error: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Alerting
    # ------------------------------------------------------------------

    async def _fire_alert(self, alert_type: str, component: str, message: str) -> None:
        """Fire an alert with dedup and rate limiting."""
        now = time.monotonic()
        key = (alert_type, component)
        dedup_window = mqtt_settings.alerts.dedup_window_s

        # Deduplication
        last_fired = self._alert_fired.get(key, 0)
        if now - last_fired < dedup_window:
            return

        # Hourly rate limit
        if now - self._alert_hour_start > 3600:
            self._alert_count_hour = 0
            self._alert_hour_start = now
        if self._alert_count_hour >= mqtt_settings.alerts.max_alerts_per_hour:
            return

        self._alert_fired[key] = now
        self._alert_count_hour += 1

        logger.warning("ALERT [%s] %s: %s", alert_type, component, message)
        await self._history.log_alert(alert_type, component, message)

        if self._on_alert:
            try:
                await self._on_alert(alert_type, component, message)
            except Exception as exc:
                logger.error("Alert callback error: %s", exc)

"""MQTT topic constants — single source of truth for the topic tree.

Usage:
    from backend.mqtt.topics import Topics
    Topics.cmd_move("cam1")       # "cmd/drives/cam1/move"
    Topics.health_pi()            # "health/pi"
"""
from __future__ import annotations


class Topics:
    """All MQTT topics used in the OAK-D drive sync protocol."""

    # ── Commands (Windows -> Pi) ──────────────────────────────────────

    @staticmethod
    def cmd_move(cam_id: str) -> str:
        return f"cmd/drives/{cam_id}/move"

    @staticmethod
    def cmd_home(cam_id: str) -> str:
        return f"cmd/drives/{cam_id}/home"

    @staticmethod
    def cmd_stop(cam_id: str) -> str:
        return f"cmd/drives/{cam_id}/stop"

    # ── Drive Status (Pi -> Windows) ──────────────────────────────────

    @staticmethod
    def status_drive_position(cam_id: str) -> str:
        return f"status/drives/{cam_id}/position"

    # ── Camera Status (Windows -> broker) ─────────────────────────────

    @staticmethod
    def status_camera(cam_id: str) -> str:
        return f"status/cameras/{cam_id}/state"

    # ── Health Beacons ────────────────────────────────────────────────

    @staticmethod
    def health_pi() -> str:
        return "health/pi"

    @staticmethod
    def health_win() -> str:
        return "health/win_controller"

    @staticmethod
    def health_camera(cam_id: str) -> str:
        return f"health/cameras/{cam_id}"

    # ── Errors ────────────────────────────────────────────────────────

    @staticmethod
    def error_drive(cam_id: str) -> str:
        return f"error/drives/{cam_id}"

    @staticmethod
    def error_camera(cam_id: str) -> str:
        return f"error/cameras/{cam_id}"

    @staticmethod
    def error_orchestration(event: str) -> str:
        return f"error/orchestration/{event}"

    # ── Monitoring ────────────────────────────────────────────────────

    @staticmethod
    def monitoring_connectivity() -> str:
        return "monitoring/connectivity"

    # ── Configuration ─────────────────────────────────────────────────

    @staticmethod
    def config_sequence_active() -> str:
        return "config/sequence/active"

    # ── IMU / Drift Detection ─────────────────────────────────────────

    @staticmethod
    def telemetry_imu(cam_id: str) -> str:
        return f"telemetry/cam/{cam_id}/imu"

    @staticmethod
    def cmd_imu_check(cam_id: str) -> str:
        return f"cmd/cam/{cam_id}/imu_check"

    @staticmethod
    def event_drift(cam_id: str, axis: str = "b") -> str:
        return f"event/drift/{cam_id}/{axis}"

    # ── Wildcard subscriptions ────────────────────────────────────────

    CMD_DRIVES_ALL = "cmd/drives/+/#"
    STATUS_DRIVES_ALL = "status/drives/+/position"
    HEALTH_ALL = "health/#"
    ERROR_ALL = "error/#"
    CMD_IMU_CHECK_ALL = "cmd/cam/+/imu_check"

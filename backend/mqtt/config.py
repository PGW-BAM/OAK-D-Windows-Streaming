"""MQTT configuration loaded from config/mqtt.yaml with env-var overrides."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_MQTT_YAML = _CONFIG_DIR / "mqtt.yaml"


class BrokerConfig(BaseModel):
    host: str = "192.168.1.100"
    port: int = 1883
    keepalive: int = 30
    clean_session: bool = False
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0


class SmtpConfig(BaseModel):
    host: str = ""
    port: int = 587
    use_tls: bool = True
    username: str = ""
    password: str = ""


class AlertThresholds(BaseModel):
    pi_offline_s: float = 10.0
    camera_offline_s: float = 15.0
    broker_offline_s: float = 10.0


class AlertsConfig(BaseModel):
    enabled: bool = True
    email: str = ""
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    thresholds: AlertThresholds = Field(default_factory=AlertThresholds)
    dedup_window_s: int = 300
    max_alerts_per_hour: int = 20


class OrchestrationConfig(BaseModel):
    move_timeout_s: float = 30.0
    capture_timeout_s: float = 10.0
    default_settling_ms: int = 150
    puback_timeout_s: float = 5.0


class MqttSettings(BaseModel):
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    health_interval_s: float = 2.0
    cam_ids: list[str] = Field(default_factory=lambda: ["cam1", "cam2"])


def load_mqtt_settings() -> MqttSettings:
    """Load MQTT settings from YAML, with environment variable overrides."""
    data: dict = {}
    if _MQTT_YAML.exists():
        with open(_MQTT_YAML) as f:
            data = yaml.safe_load(f) or {}

    settings = MqttSettings(**data)

    # Env-var overrides for the most common fields
    if v := os.environ.get("MQTT_BROKER_HOST"):
        settings.broker.host = v
    if v := os.environ.get("MQTT_BROKER_PORT"):
        settings.broker.port = int(v)
    if v := os.environ.get("OAK_ALERT_EMAIL"):
        settings.alerts.email = v
    if v := os.environ.get("OAK_SMTP_HOST"):
        settings.alerts.smtp.host = v
    if v := os.environ.get("OAK_SMTP_PASSWORD"):
        settings.alerts.smtp.password = v

    return settings


mqtt_settings = load_mqtt_settings()

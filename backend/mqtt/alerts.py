"""Email alert system with SMTP, deduplication, and Jinja2 templates."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import mqtt_settings
from .history import HistoryDB
from .models import AlertEvent, ConnectivityState

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "email_templates"
_UNSENT_LOG = Path(__file__).resolve().parent.parent.parent / "logs" / "unsent_alerts.jsonl"


class EmailAlertSender:
    """Sends email alerts via async SMTP with Jinja2 templates.

    Falls back to writing unsent alerts to a JSONL file when SMTP fails.
    """

    def __init__(self, history: HistoryDB) -> None:
        self._cfg = mqtt_settings.alerts
        self._history = history
        self._template = self._load_template()

    def _load_template(self) -> str | None:
        template_path = _TEMPLATE_DIR / "alert.txt.j2"
        if template_path.exists():
            return template_path.read_text()
        return None

    async def send_alert(
        self,
        alert_type: str,
        component: str,
        message: str,
        system_state: ConnectivityState | None = None,
    ) -> None:
        """Send an alert email. Falls back to file logging on SMTP failure."""
        if not self._cfg.enabled:
            return
        if not self._cfg.email:
            logger.debug("No alert email configured — skipping")
            return

        severity = _severity_for(alert_type)
        alert = AlertEvent(
            alert_type=alert_type,
            severity=severity,
            component=component,
            message=message,
            system_state=system_state,
        )

        subject = f"[OAK-Drive-Sync] {severity.upper()}: {alert_type} — {component}"
        body = self._render_body(alert)

        email_sent = False
        try:
            await self._send_smtp(subject, body)
            email_sent = True
            logger.info("Alert email sent: %s", subject)
        except Exception as exc:
            logger.error("SMTP send failed: %s — writing to fallback log", exc)
            self._write_fallback(alert)

        await self._history.log_alert(alert_type, component, message, email_sent)

    def _render_body(self, alert: AlertEvent) -> str:
        """Render the alert body using Jinja2 template or fallback plaintext."""
        if self._template:
            try:
                from jinja2 import Template
                tmpl = Template(self._template)
                return tmpl.render(
                    alert=alert,
                    remediation=_remediation_for(alert.alert_type),
                )
            except Exception as exc:
                logger.warning("Template render error: %s — using plaintext", exc)

        # Fallback plaintext
        lines = [
            f"Alert: {alert.alert_type}",
            f"Severity: {alert.severity}",
            f"Component: {alert.component}",
            f"Message: {alert.message}",
            f"Time: {alert.timestamp.isoformat()}",
            "",
        ]
        if alert.system_state:
            lines.append("System State:")
            lines.append(f"  Pi Online: {alert.system_state.pi_online}")
            lines.append(f"  Broker Connected: {alert.system_state.broker_connected}")
            lines.append(f"  Cameras: {alert.system_state.cameras}")
            lines.append(f"  Drives: {alert.system_state.drives}")
        lines.append("")
        lines.append(f"Remediation: {_remediation_for(alert.alert_type)}")
        return "\n".join(lines)

    async def _send_smtp(self, subject: str, body: str) -> None:
        """Send email via aiosmtplib."""
        import aiosmtplib
        from email.message import EmailMessage

        smtp_cfg = self._cfg.smtp
        if not smtp_cfg.host:
            raise RuntimeError("SMTP host not configured")

        msg = EmailMessage()
        msg["From"] = smtp_cfg.username or f"oak-alerts@{smtp_cfg.host}"
        msg["To"] = self._cfg.email
        msg["Subject"] = subject
        msg.set_content(body)

        await aiosmtplib.send(
            msg,
            hostname=smtp_cfg.host,
            port=smtp_cfg.port,
            username=smtp_cfg.username or None,
            password=smtp_cfg.password or None,
            use_tls=smtp_cfg.use_tls,
        )

    def _write_fallback(self, alert: AlertEvent) -> None:
        """Append alert to the unsent alerts JSONL file."""
        _UNSENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_UNSENT_LOG, "a") as f:
            f.write(alert.model_dump_json() + "\n")


def _severity_for(alert_type: str) -> str:
    return {
        "pi_offline": "critical",
        "broker_offline": "critical",
        "camera_offline": "high",
        "drive_fault": "high",
        "sequence_aborted": "medium",
        "capture_failure": "medium",
    }.get(alert_type, "low")


def _remediation_for(alert_type: str) -> str:
    return {
        "pi_offline": "Check Pi power supply and network connection. SSH into the Pi and verify the drive controller service is running.",
        "broker_offline": "Verify Mosquitto is running on the Pi: 'sudo systemctl status mosquitto'. Check network connectivity.",
        "camera_offline": "Check PoE cable and switch port LED. Try power-cycling the camera by disconnecting/reconnecting the PoE cable.",
        "drive_fault": "Check drive hardware: motor connections, limit switches, driver board power. The drive has been disabled for safety.",
        "sequence_aborted": "Review the sequence log and correct any drive positioning issues before restarting the sequence.",
        "capture_failure": "Check camera connection and ensure the camera pipeline is running. Review backend logs for details.",
    }.get(alert_type, "Review system logs for details.")

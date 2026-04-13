# Raspberry Pi 5 — Drive Controller Implementation Guide

This document provides everything needed to implement the Pi side of the OAK-D Drive Sync system. The Windows side is already implemented and expects the Pi to follow this protocol exactly.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Hardware Setup](#2-hardware-setup)
3. [Software Prerequisites](#3-software-prerequisites)
4. [Mosquitto MQTT Broker Setup](#4-mosquitto-mqtt-broker-setup)
5. [MQTT Topic Schema & Message Payloads](#5-mqtt-topic-schema--message-payloads)
6. [Drive Controller Implementation](#6-drive-controller-implementation)
7. [Health Beacon](#7-health-beacon)
8. [Error Handling & Safety](#8-error-handling--safety)
9. [Configuration Files](#9-configuration-files)
10. [Systemd Services](#10-systemd-services)
11. [Testing & Validation](#11-testing--validation)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. System Overview

```
┌──────────────────────────────────────────────────┐
│  Raspberry Pi 5                                  │
│                                                  │
│  ┌────────────────┐  ┌─────────────────────────┐ │
│  │ Mosquitto      │  │ Drive Controller         │ │
│  │ Broker         │  │  (your implementation)   │ │
│  │ (port 1883)    │  │                          │ │
│  │                │  │ • GPIO step/dir/enable   │ │
│  │ Receives cmds  │  │ • MQTT command listener  │ │
│  │ from Windows   │  │ • Position publisher     │ │
│  │ Routes msgs    │  │ • Health beacon (2s)     │ │
│  └────────────────┘  │ • Error detection        │ │
│                      └──────────┬──────────────┘ │
│                                 │ GPIO           │
│                      ┌──────────▼──────────────┐ │
│                      │  4× Stepper Drives       │ │
│                      │  cam1:a  cam1:b          │ │
│                      │  cam2:a  cam2:b          │ │
│                      └─────────────────────────┘ │
└──────────────────────────────────────────────────┘
         │
         │  MQTT over TCP (port 1883)
         │  PoE LAN
         ▼
┌──────────────────────┐
│  Windows 11 PC       │
│  (already implemented)│
│  - Sends move/home/  │
│    stop commands      │
│  - Listens for drive  │
│    position updates   │
│  - Monitors health    │
└──────────────────────┘
```

### Communication Flow

1. **Windows sends commands** → `cmd/drives/{cam_id}/move|home|stop`
2. **Pi executes GPIO drive movements** and publishes position updates → `status/drives/{cam_id}/position`
3. **Pi publishes health beacons** every 2 seconds → `health/pi`
4. **Pi reports errors** → `error/drives/{cam_id}`
5. **Windows waits for `state="reached"`** before capturing images

---

## 2. Hardware Setup

### 2.1 GPIO Pin Mapping

Each drive requires 3-4 GPIO pins. With 4 drives (2 per camera), you need 12-16 GPIO pins.

**Per drive:**
| Signal | Description | Direction |
|--------|-------------|-----------|
| `step_pin` | Pulse output to stepper driver | Output |
| `dir_pin` | Direction (HIGH=CW, LOW=CCW) | Output |
| `enable_pin` | Driver enable (active LOW) | Output |
| `limit_min_pin` | Minimum limit switch (NC, pulled HIGH) | Input |
| `limit_max_pin` | Maximum limit switch (NC, pulled HIGH) | Input |

**Suggested pin assignment (adjust for your wiring):**

```yaml
# config/drive_pinmap.yaml
drives:
  cam1:
    a:
      step_pin: 17
      dir_pin: 27
      enable_pin: 22
      limit_min_pin: 5
      limit_max_pin: 6
      steps_per_unit: 200       # steps per degree/mm (depends on your gearing)
      max_speed: 1000           # max steps/second
      home_direction: -1        # -1 = toward min limit
    b:
      step_pin: 23
      dir_pin: 24
      enable_pin: 25
      limit_min_pin: 12
      limit_max_pin: 16
      steps_per_unit: 200
      max_speed: 1000
      home_direction: -1

  cam2:
    a:
      step_pin: 20
      dir_pin: 21
      enable_pin: 26
      limit_min_pin: 19
      limit_max_pin: 13
      steps_per_unit: 200
      max_speed: 1000
      home_direction: -1
    b:
      step_pin: 18
      dir_pin: 15
      enable_pin: 14
      limit_min_pin: 9
      limit_max_pin: 11
      steps_per_unit: 200
      max_speed: 1000
      home_direction: -1
```

### 2.2 Network Configuration

The Pi uses **dual networking**:
- **`eth0`** — Static IP `169.254.10.10/16` on the PoE switch LAN (cameras + Windows PC)
- **`wlan0`** — DHCP via WiFi router (internet access)

```bash
# Set static IP on eth0 (no gateway — internet stays on WiFi)
sudo nmcli con mod "Wired connection 1" \
  ipv4.addresses 169.254.10.10/16 \
  ipv4.method manual \
  ipv4.gateway "" \
  ipv4.dns ""
sudo nmcli con up "Wired connection 1"

# Verify
ip addr show eth0          # should show 169.254.10.10/16
ping 169.254.236.75        # should reach camera
ping google.com            # should still work via WiFi
```

### 2.3 Stepper Driver Wiring

Typical A4988 or DRV8825 stepper driver wiring:

```
Pi GPIO ─── Step  ──→ [Driver STEP]  ──→ [Motor Coil A]
Pi GPIO ─── Dir   ──→ [Driver DIR ]  ──→ [Motor Coil B]
Pi GPIO ─── Enable──→ [Driver EN  ]
GND     ─── GND   ──→ [Driver GND ]
                       [Driver VMOT] ←── Motor Power Supply (12-24V)
```

### 2.3 Limit Switches

Wire limit switches as **normally closed (NC)** with internal pull-up:

```
Pi GPIO (INPUT, pull-up) ──── [NC Switch] ──── GND
```

- Normal state: GPIO reads LOW (switch closed, pulling to GND)
- Triggered state: GPIO reads HIGH (switch open, pull-up wins)
- Use `gpiozero.Button(pin, pull_up=True, active_state=None)` to detect

---

## 3. Software Prerequisites

### 3.1 System Setup

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11+ (should already be on Pi OS Bookworm)
python3 --version

# Install pip and venv
sudo apt install -y python3-pip python3-venv

# Install Mosquitto broker
sudo apt install -y mosquitto mosquitto-clients

# Create project directory
mkdir -p ~/oak-drive-controller
cd ~/oak-drive-controller
python3 -m venv .venv
source .venv/bin/activate
```

### 3.2 Python Dependencies

Create `requirements.txt`:

```
aiomqtt>=2.0.0
pydantic>=2.5.0
structlog>=24.0.0
pyyaml>=6.0
gpiozero>=2.0
lgpio>=0.2.0
```

```bash
pip install -r requirements.txt
```

### 3.3 GPIO Permissions

On Pi 5 with Bookworm, `lgpio` is the required backend:

```bash
# Ensure your user is in the gpio group
sudo usermod -aG gpio $USER

# lgpio should work out of the box on Pi 5
# Test with:
python3 -c "import lgpio; h = lgpio.gpiochip_open(0); print('GPIO OK'); lgpio.gpiochip_close(h)"
```

---

## 4. Mosquitto MQTT Broker Setup

### 4.1 Broker Configuration

Create `/etc/mosquitto/conf.d/oak-drive-sync.conf`:

```conf
# OAK-D Drive Sync — Mosquitto Configuration
# Listens on all interfaces (LAN only — no internet exposure)

listener 1883 0.0.0.0
allow_anonymous true

# Persistence — retain messages survive broker restarts
persistence true
persistence_location /var/lib/mosquitto/

# Logging
log_dest file /var/log/mosquitto/mosquitto.log
log_type warning
log_type error
log_type notice
log_timestamp true
log_timestamp_format %Y-%m-%dT%H:%M:%S

# Performance tuning for small LAN
max_inflight_messages 20
max_queued_messages 1000
message_size_limit 262144

# Keepalive
max_keepalive 60
```

### 4.2 Enable and Start

```bash
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto
sudo systemctl status mosquitto

# Verify it's listening
mosquitto_sub -h localhost -t '#' -v &
mosquitto_pub -h localhost -t 'test' -m 'hello'
# Should see: test hello
```

### 4.3 Verify from Windows

From the Windows machine (with `mosquitto-clients` or any MQTT tool):

```bash
# Replace PI_IP with your Pi's IP address
mosquitto_sub -h PI_IP -t '#' -v
```

---

## 5. MQTT Topic Schema & Message Payloads

### 5.1 Topics the Pi SUBSCRIBES to (receives commands)

| Topic | Payload Model | QoS | Description |
|-------|---------------|-----|-------------|
| `cmd/drives/cam1/move` | MoveCommand | 1 | Move cam1 drive to position |
| `cmd/drives/cam1/home` | HomeCommand | 1 | Home cam1 drive |
| `cmd/drives/cam1/stop` | StopCommand | 1 | Stop cam1 drive |
| `cmd/drives/cam2/move` | MoveCommand | 1 | Move cam2 drive to position |
| `cmd/drives/cam2/home` | HomeCommand | 1 | Home cam2 drive |
| `cmd/drives/cam2/stop` | StopCommand | 1 | Stop cam2 drive |

**Subscribe wildcard:** `cmd/drives/+/+` (covers all cameras and commands)

### 5.2 Topics the Pi PUBLISHES to

| Topic | Payload Model | QoS | Retained | Description |
|-------|---------------|-----|----------|-------------|
| `status/drives/cam1/position` | DrivePosition | 1 | Yes | Current position of cam1 drives |
| `status/drives/cam2/position` | DrivePosition | 1 | Yes | Current position of cam2 drives |
| `health/pi` | PiHealth | 0 | No | Health beacon every 2s |
| `error/drives/cam1` | DriveError | 1 | No | Error on cam1 drive |
| `error/drives/cam2` | DriveError | 1 | No | Error on cam2 drive |

### 5.3 Message Payload Schemas

All payloads are JSON. Timestamps are ISO 8601 UTC.

#### MoveCommand (Windows -> Pi)
```json
{
    "sequence_id": "grid-scan-001",
    "drive_axis": "a",
    "target_position": 45.0,
    "speed": 1.0,
    "timestamp": "2026-03-30T10:00:00Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `sequence_id` | string | UUID for correlation with capture sequence |
| `drive_axis` | `"a"` or `"b"` | Which axis on the camera |
| `target_position` | float | Target in drive-native units (degrees, mm, steps) |
| `speed` | float (0.0-1.0) | Normalized speed (1.0 = max) |
| `timestamp` | ISO datetime | When the command was issued |

#### HomeCommand (Windows -> Pi)
```json
{
    "sequence_id": "grid-scan-001",
    "drive_axis": "a",
    "timestamp": "2026-03-30T10:00:00Z"
}
```

#### StopCommand (Windows -> Pi)
```json
{
    "sequence_id": null,
    "drive_axis": "a",
    "timestamp": "2026-03-30T10:00:00Z"
}
```
- If `drive_axis` is `null`, stop ALL drives on that camera.

#### DrivePosition (Pi -> Windows)
```json
{
    "sequence_id": "grid-scan-001",
    "drive_axis": "a",
    "current_position": 45.0,
    "target_position": 45.0,
    "state": "reached",
    "timestamp": "2026-03-30T10:00:01Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `state` | string | One of: `idle`, `moving`, `reached`, `fault`, `homing` |
| `current_position` | float | Current drive position in native units |
| `target_position` | float or null | Where the drive is going (null when idle) |

**State machine:**
```
           ┌─────────┐
      ┌───►│  idle   │◄───────────────┐
      │    └────┬────┘                │
      │         │ move cmd            │ timeout/done
      │    ┌────▼────┐          ┌─────┴────┐
      │    │ moving  │─────────►│ reached  │
      │    └────┬────┘  target  └──────────┘
      │         │ reached
      │    ┌────▼────┐
      │    │  fault  │ (stall, limit, gpio error)
      │    └─────────┘
      │
      │    ┌─────────┐
      └────│ homing  │ (home cmd -> move to limit -> reset zero)
           └─────────┘
```

**CRITICAL:** The Windows orchestrator waits for `state="reached"` before triggering image capture. Publish this state promptly when the drive reaches the target.

#### PiHealth (Pi -> Windows, every 2 seconds)
```json
{
    "online": true,
    "cpu_temp_c": 52.3,
    "uptime_s": 86400,
    "drive_states": {
        "cam1:a": "idle",
        "cam1:b": "idle",
        "cam2:a": "moving",
        "cam2:b": "idle"
    },
    "timestamp": "2026-03-30T10:00:00Z"
}
```

#### DriveError (Pi -> Windows)
```json
{
    "sequence_id": "grid-scan-001",
    "drive_axis": "a",
    "error_type": "stall",
    "message": "Drive cam1:a stall detected — expected to reach target in 5.0s but timed out",
    "timestamp": "2026-03-30T10:00:05Z"
}
```

| `error_type` | Description |
|-------------|-------------|
| `stall` | Drive didn't reach target within expected time |
| `limit_switch` | Unexpected limit switch activation during move |
| `timeout` | Command processing timeout |
| `gpio_fault` | GPIO pin error or driver fault |

### 5.4 Last Will and Testament (LWT)

When connecting to the broker, the Pi client MUST set an LWT:

```python
# LWT configuration
will_topic = "health/pi"
will_payload = '{"online": false}'
will_qos = 0
will_retain = False
```

This ensures the Windows side detects Pi disconnection within the broker's keepalive period.

---

## 6. Drive Controller Implementation

### 6.1 Recommended Architecture

```
drive_controller/
├── __init__.py
├── main.py              # Entry point, asyncio event loop
├── config.py            # YAML config loader
├── mqtt_client.py       # Async MQTT client (same pattern as Windows side)
├── drive.py             # Single drive GPIO controller
├── drive_manager.py     # Manages all 4 drives
├── health.py            # Health beacon publisher
└── models.py            # Pydantic message models (copy from Windows side)
```

### 6.2 Single Drive Controller

```python
"""drive.py — GPIO controller for one stepper drive axis."""
import asyncio
import time
import lgpio
from dataclasses import dataclass
from enum import Enum


class DriveState(str, Enum):
    IDLE = "idle"
    MOVING = "moving"
    REACHED = "reached"
    FAULT = "fault"
    HOMING = "homing"


@dataclass
class DriveConfig:
    step_pin: int
    dir_pin: int
    enable_pin: int
    limit_min_pin: int
    limit_max_pin: int
    steps_per_unit: float
    max_speed: float          # steps/second
    home_direction: int       # -1 or 1


class StepperDrive:
    """Controls one stepper motor axis via GPIO."""

    def __init__(self, cam_id: str, axis: str, config: DriveConfig) -> None:
        self.cam_id = cam_id
        self.axis = axis
        self.config = config
        self.state = DriveState.IDLE
        self.current_position: float = 0.0
        self.target_position: float | None = None
        self._gpio_handle: int | None = None

    def setup_gpio(self) -> None:
        """Initialize GPIO pins."""
        h = lgpio.gpiochip_open(0)
        self._gpio_handle = h

        # Output pins
        lgpio.gpio_claim_output(h, self.config.step_pin, 0)
        lgpio.gpio_claim_output(h, self.config.dir_pin, 0)
        lgpio.gpio_claim_output(h, self.config.enable_pin, 1)  # disabled by default

        # Input pins with pull-up (limit switches are NC to GND)
        lgpio.gpio_claim_input(h, self.config.limit_min_pin, lgpio.SET_PULL_UP)
        lgpio.gpio_claim_input(h, self.config.limit_max_pin, lgpio.SET_PULL_UP)

    def cleanup_gpio(self) -> None:
        """Release GPIO resources."""
        if self._gpio_handle is not None:
            # Disable driver
            lgpio.gpio_write(self._gpio_handle, self.config.enable_pin, 1)
            lgpio.gpiochip_close(self._gpio_handle)
            self._gpio_handle = None

    def enable(self) -> None:
        """Enable the stepper driver (active LOW)."""
        if self._gpio_handle:
            lgpio.gpio_write(self._gpio_handle, self.config.enable_pin, 0)

    def disable(self) -> None:
        """Disable the stepper driver."""
        if self._gpio_handle:
            lgpio.gpio_write(self._gpio_handle, self.config.enable_pin, 1)

    @property
    def at_min_limit(self) -> bool:
        if not self._gpio_handle:
            return False
        return lgpio.gpio_read(self._gpio_handle, self.config.limit_min_pin) == 1

    @property
    def at_max_limit(self) -> bool:
        if not self._gpio_handle:
            return False
        return lgpio.gpio_read(self._gpio_handle, self.config.limit_max_pin) == 1

    async def move_to(self, target: float, speed: float = 1.0) -> None:
        """Move to absolute position. Blocks until reached or fault.

        Args:
            target: Target position in drive-native units
            speed: 0.0-1.0 normalized speed
        """
        if self._gpio_handle is None:
            raise RuntimeError("GPIO not initialized")

        self.target_position = target
        self.state = DriveState.MOVING
        self.enable()

        # Calculate steps needed
        delta = target - self.current_position
        steps = int(abs(delta) * self.config.steps_per_unit)
        direction = 1 if delta > 0 else -1

        # Set direction pin
        lgpio.gpio_write(
            self._gpio_handle,
            self.config.dir_pin,
            1 if direction > 0 else 0,
        )

        # Calculate step delay from speed
        step_speed = self.config.max_speed * max(0.01, min(1.0, speed))
        step_delay = 1.0 / step_speed

        # Step loop
        for i in range(steps):
            # Check limit switches
            if direction > 0 and self.at_max_limit:
                self.state = DriveState.FAULT
                self.disable()
                raise RuntimeError(f"Max limit switch hit at position {self.current_position}")
            if direction < 0 and self.at_min_limit:
                self.state = DriveState.FAULT
                self.disable()
                raise RuntimeError(f"Min limit switch hit at position {self.current_position}")

            # Generate step pulse
            lgpio.gpio_write(self._gpio_handle, self.config.step_pin, 1)
            await asyncio.sleep(step_delay / 2)
            lgpio.gpio_write(self._gpio_handle, self.config.step_pin, 0)
            await asyncio.sleep(step_delay / 2)

            # Update position incrementally
            self.current_position += direction / self.config.steps_per_unit

        # Snap to target (avoid float drift)
        self.current_position = target
        self.target_position = target
        self.state = DriveState.REACHED

    async def home(self) -> None:
        """Home the drive: move toward limit switch, then reset position to 0."""
        self.state = DriveState.HOMING
        self.enable()

        direction = self.config.home_direction
        lgpio.gpio_write(
            self._gpio_handle,
            self.config.dir_pin,
            1 if direction > 0 else 0,
        )

        step_delay = 1.0 / (self.config.max_speed * 0.3)  # Home at 30% speed
        limit_check = self.at_min_limit if direction < 0 else self.at_max_limit

        max_steps = int(self.config.max_speed * 60)  # 60 second timeout
        for _ in range(max_steps):
            limit_hit = self.at_min_limit if direction < 0 else self.at_max_limit
            if limit_hit:
                break
            lgpio.gpio_write(self._gpio_handle, self.config.step_pin, 1)
            await asyncio.sleep(step_delay / 2)
            lgpio.gpio_write(self._gpio_handle, self.config.step_pin, 0)
            await asyncio.sleep(step_delay / 2)
        else:
            self.state = DriveState.FAULT
            self.disable()
            raise RuntimeError("Home: limit switch not found within timeout")

        self.current_position = 0.0
        self.target_position = None
        self.state = DriveState.IDLE

    def emergency_stop(self) -> None:
        """Immediately stop and disable the drive."""
        self.disable()
        self.target_position = None
        self.state = DriveState.IDLE
```

### 6.3 Drive Manager with MQTT Integration

```python
"""drive_manager.py — manages all drives and handles MQTT commands."""
import asyncio
import json
import logging
from typing import Any

from .drive import StepperDrive, DriveState, DriveConfig
from .models import (
    MoveCommand, HomeCommand, StopCommand,
    DrivePosition, DriveError,
)

logger = logging.getLogger(__name__)


class DriveManager:
    """Manages all 4 drives and dispatches MQTT commands."""

    def __init__(self, drives_config: dict) -> None:
        self.drives: dict[str, StepperDrive] = {}
        for cam_id, axes in drives_config.items():
            for axis, pin_cfg in axes.items():
                key = f"{cam_id}:{axis}"
                config = DriveConfig(**pin_cfg)
                self.drives[key] = StepperDrive(cam_id, axis, config)

    def setup(self) -> None:
        """Initialize GPIO for all drives."""
        for drive in self.drives.values():
            drive.setup_gpio()
            logger.info("GPIO initialized for %s:%s", drive.cam_id, drive.axis)

    def cleanup(self) -> None:
        """Release all GPIO resources."""
        for drive in self.drives.values():
            drive.emergency_stop()
            drive.cleanup_gpio()

    def get_drive(self, cam_id: str, axis: str) -> StepperDrive:
        key = f"{cam_id}:{axis}"
        if key not in self.drives:
            raise KeyError(f"Drive {key} not found")
        return self.drives[key]

    def get_all_states(self) -> dict[str, str]:
        """Return dict of drive states for health beacon."""
        return {
            key: drive.state.value
            for key, drive in self.drives.items()
        }

    async def handle_mqtt_command(
        self,
        topic: str,
        data: dict[str, Any],
        publish_fn,
    ) -> None:
        """Route an MQTT command to the appropriate drive.

        Args:
            topic: MQTT topic (e.g. "cmd/drives/cam1/move")
            data: Parsed JSON payload
            publish_fn: async callable(topic, payload_dict, qos, retain)
        """
        parts = topic.split("/")
        # cmd/drives/{cam_id}/{action}
        if len(parts) != 4:
            return
        cam_id = parts[2]
        action = parts[3]

        try:
            if action == "move":
                cmd = MoveCommand(**data)
                await self._handle_move(cam_id, cmd, publish_fn)
            elif action == "home":
                cmd = HomeCommand(**data)
                await self._handle_home(cam_id, cmd, publish_fn)
            elif action == "stop":
                cmd = StopCommand(**data)
                await self._handle_stop(cam_id, cmd, publish_fn)
            else:
                logger.warning("Unknown drive command: %s", action)
        except Exception as exc:
            logger.error("Command error [%s]: %s", topic, exc)

    async def _handle_move(
        self, cam_id: str, cmd: MoveCommand, publish_fn
    ) -> None:
        drive = self.get_drive(cam_id, cmd.drive_axis)
        pos_topic = f"status/drives/{cam_id}/position"

        # Publish "moving" state
        await publish_fn(
            pos_topic,
            DrivePosition(
                sequence_id=cmd.sequence_id,
                drive_axis=cmd.drive_axis,
                current_position=drive.current_position,
                target_position=cmd.target_position,
                state="moving",
            ).model_dump(mode="json"),
            qos=1, retain=True,
        )

        try:
            await drive.move_to(cmd.target_position, cmd.speed)

            # Publish "reached" state
            await publish_fn(
                pos_topic,
                DrivePosition(
                    sequence_id=cmd.sequence_id,
                    drive_axis=cmd.drive_axis,
                    current_position=drive.current_position,
                    target_position=cmd.target_position,
                    state="reached",
                ).model_dump(mode="json"),
                qos=1, retain=True,
            )
        except Exception as exc:
            # Publish error
            await publish_fn(
                f"error/drives/{cam_id}",
                DriveError(
                    sequence_id=cmd.sequence_id,
                    drive_axis=cmd.drive_axis,
                    error_type="stall",
                    message=str(exc),
                ).model_dump(mode="json"),
                qos=1, retain=False,
            )
            # Publish fault state
            await publish_fn(
                pos_topic,
                DrivePosition(
                    sequence_id=cmd.sequence_id,
                    drive_axis=cmd.drive_axis,
                    current_position=drive.current_position,
                    target_position=cmd.target_position,
                    state="fault",
                ).model_dump(mode="json"),
                qos=1, retain=True,
            )

    async def _handle_home(
        self, cam_id: str, cmd: HomeCommand, publish_fn
    ) -> None:
        drive = self.get_drive(cam_id, cmd.drive_axis)
        pos_topic = f"status/drives/{cam_id}/position"

        await publish_fn(
            pos_topic,
            DrivePosition(
                sequence_id=cmd.sequence_id,
                drive_axis=cmd.drive_axis,
                current_position=drive.current_position,
                state="homing",
            ).model_dump(mode="json"),
            qos=1, retain=True,
        )

        try:
            await drive.home()
            await publish_fn(
                pos_topic,
                DrivePosition(
                    sequence_id=cmd.sequence_id,
                    drive_axis=cmd.drive_axis,
                    current_position=0.0,
                    state="idle",
                ).model_dump(mode="json"),
                qos=1, retain=True,
            )
        except Exception as exc:
            await publish_fn(
                f"error/drives/{cam_id}",
                DriveError(
                    sequence_id=cmd.sequence_id,
                    drive_axis=cmd.drive_axis,
                    error_type="stall",
                    message=str(exc),
                ).model_dump(mode="json"),
                qos=1, retain=False,
            )

    async def _handle_stop(
        self, cam_id: str, cmd: StopCommand, publish_fn
    ) -> None:
        if cmd.drive_axis:
            # Stop single axis
            drive = self.get_drive(cam_id, cmd.drive_axis)
            drive.emergency_stop()
        else:
            # Stop all axes on this camera
            for key, drive in self.drives.items():
                if drive.cam_id == cam_id:
                    drive.emergency_stop()
```

### 6.4 Main Entry Point

```python
"""main.py — Pi drive controller entry point."""
import asyncio
import json
import logging
import signal
import time
from pathlib import Path

import aiomqtt
import yaml

from .drive_manager import DriveManager
from .models import PiHealth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# Load config
CONFIG_DIR = Path(__file__).parent.parent / "config"


def load_config() -> dict:
    config_file = CONFIG_DIR / "drive_pinmap.yaml"
    with open(config_file) as f:
        return yaml.safe_load(f)


async def main() -> None:
    config = load_config()
    broker_host = config.get("broker_host", "169.254.10.10")
    broker_port = config.get("broker_port", 1883)

    # Initialize drives
    drive_manager = DriveManager(config["drives"])
    drive_manager.setup()

    start_time = time.monotonic()

    try:
        # LWT for disconnect detection
        will = aiomqtt.Will(
            topic="health/pi",
            payload=json.dumps({"online": False}),
            qos=0,
            retain=False,
        )

        async with aiomqtt.Client(
            hostname=broker_host,
            port=broker_port,
            keepalive=30,
            will=will,
        ) as client:
            logger.info("Connected to MQTT broker at %s:%d", broker_host, broker_port)

            # Subscribe to all drive commands
            await client.subscribe("cmd/drives/+/+", qos=1)

            # Define publish helper
            async def publish(topic, payload, qos=1, retain=False):
                data = payload if isinstance(payload, str) else json.dumps(payload, default=str)
                await client.publish(topic, data, qos=qos, retain=retain)

            # Start health beacon task
            async def health_beacon():
                while True:
                    try:
                        cpu_temp = _read_cpu_temp()
                        uptime = int(time.monotonic() - start_time)
                        health = PiHealth(
                            online=True,
                            cpu_temp_c=cpu_temp,
                            uptime_s=uptime,
                            drive_states=drive_manager.get_all_states(),
                        )
                        await client.publish(
                            "health/pi",
                            health.model_dump_json(),
                            qos=0,
                        )
                    except Exception as exc:
                        logger.debug("Health beacon error: %s", exc)
                    await asyncio.sleep(2.0)

            health_task = asyncio.create_task(health_beacon())

            # Message handling loop
            async for message in client.messages:
                topic = str(message.topic)
                try:
                    data = json.loads(message.payload.decode())
                    await drive_manager.handle_mqtt_command(topic, data, publish)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON on %s", topic)
                except Exception as exc:
                    logger.error("Command handler error: %s", exc, exc_info=True)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        drive_manager.cleanup()
        logger.info("Drive controller stopped")


def _read_cpu_temp() -> float:
    """Read Pi CPU temperature."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return float(f.read().strip()) / 1000.0
    except Exception:
        return 0.0


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 7. Health Beacon

The health beacon is critical for the Windows side to know the Pi is alive.

### Requirements
- Publish to `health/pi` every **2 seconds**
- QoS 0 (loss-tolerant, high frequency)
- Include CPU temperature, uptime, and all drive states
- LWT on `health/pi` with `{"online": false}` for disconnect detection

### CPU Temperature

```python
def read_cpu_temp() -> float:
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return float(f.read().strip()) / 1000.0
```

### Windows Detection Thresholds
- If no health beacon received for **6 seconds**: Pi marked as degraded (yellow)
- If no health beacon received for **10 seconds**: Alert fires ("pi_offline")

---

## 8. Error Handling & Safety

### 8.1 Stall Detection

If a move command's expected duration elapses without completion:

```python
expected_time = steps / step_speed  # seconds
timeout = expected_time * 1.5 + 2.0  # 50% margin + 2s buffer
```

On stall:
1. Disable the driver (safety)
2. Set drive state to `fault`
3. Publish `DriveError` with `error_type="stall"`
4. Publish `DrivePosition` with `state="fault"`

### 8.2 Limit Switch Hit During Move

If a limit switch activates unexpectedly during a move:
1. Immediately stop stepping
2. Disable the driver
3. Publish `DriveError` with `error_type="limit_switch"`
4. Set state to `fault`

### 8.3 Emergency Stop

On receiving a `StopCommand`:
1. Immediately stop all stepping
2. Disable driver(s)
3. Set state to `idle`
4. Do NOT set to `fault` — stop is an operator command, not an error

### 8.4 Recovery from Fault

After a fault, the drive stays in `fault` state until:
- A new `home` command is received (re-homes and resets)
- The Pi service is restarted

---

## 9. Configuration Files

### 9.1 drive_pinmap.yaml

```yaml
# /home/pi/oak-drive-controller/config/drive_pinmap.yaml
broker_host: "169.254.10.10"       # Pi static IP on PoE switch LAN
broker_port: 1883

drives:
  cam1:
    a:
      step_pin: 17
      dir_pin: 27
      enable_pin: 22
      limit_min_pin: 5
      limit_max_pin: 6
      steps_per_unit: 200
      max_speed: 1000
      home_direction: -1
    b:
      step_pin: 23
      dir_pin: 24
      enable_pin: 25
      limit_min_pin: 12
      limit_max_pin: 16
      steps_per_unit: 200
      max_speed: 1000
      home_direction: -1
  cam2:
    a:
      step_pin: 20
      dir_pin: 21
      enable_pin: 26
      limit_min_pin: 19
      limit_max_pin: 13
      steps_per_unit: 200
      max_speed: 1000
      home_direction: -1
    b:
      step_pin: 18
      dir_pin: 15
      enable_pin: 14
      limit_min_pin: 9
      limit_max_pin: 11
      steps_per_unit: 200
      max_speed: 1000
      home_direction: -1
```

**IMPORTANT:** Adjust `steps_per_unit` based on your actual stepper motor, microstepping setting, and mechanical gearing. For example:
- 200-step motor at 1/16 microstepping = 3200 steps/revolution
- If gear ratio is 5:1, then 16000 steps/revolution
- If one revolution = 360 degrees, `steps_per_unit` = 44.44 steps/degree

---

## 10. Systemd Services

### 10.1 Drive Controller Service

Create `/etc/systemd/system/oak-drive-controller.service`:

```ini
[Unit]
Description=OAK-D Drive Controller
After=network-online.target mosquitto.service
Wants=network-online.target mosquitto.service

[Service]
Type=simple
User=pi
Group=gpio
WorkingDirectory=/home/pi/oak-drive-controller
ExecStart=/home/pi/oak-drive-controller/.venv/bin/python -m drive_controller.main
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Safety: stop drives on service failure
ExecStopPost=/bin/bash -c 'echo "Drive controller stopped" | logger -t oak-drives'

# Environment
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 10.2 Enable Services

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable and start
sudo systemctl enable oak-drive-controller
sudo systemctl start oak-drive-controller

# Check status
sudo systemctl status oak-drive-controller

# View logs
journalctl -u oak-drive-controller -f
```

### 10.3 Mosquitto Service (should already be enabled)

```bash
sudo systemctl enable mosquitto
sudo systemctl status mosquitto
```

---

## 11. Testing & Validation

### 11.1 Test MQTT Connectivity

From the Pi:
```bash
# Subscribe to all topics in one terminal
mosquitto_sub -h localhost -t '#' -v

# In another terminal, simulate a move command
mosquitto_pub -h localhost -t 'cmd/drives/cam1/move' -m '{
    "sequence_id": "test-001",
    "drive_axis": "a",
    "target_position": 10.0,
    "speed": 0.5,
    "timestamp": "2026-03-30T10:00:00Z"
}'
```

### 11.2 Test Without Hardware (GPIO Simulation)

For development without physical drives, create a mock drive that logs instead of pulsing GPIO:

```python
class MockStepperDrive(StepperDrive):
    """Simulated drive for testing without hardware."""

    def setup_gpio(self) -> None:
        logger.info("MOCK: GPIO setup for %s:%s", self.cam_id, self.axis)

    def cleanup_gpio(self) -> None:
        logger.info("MOCK: GPIO cleanup for %s:%s", self.cam_id, self.axis)

    async def move_to(self, target: float, speed: float = 1.0) -> None:
        self.target_position = target
        self.state = DriveState.MOVING
        # Simulate movement time
        distance = abs(target - self.current_position)
        move_time = distance / (self.config.max_speed * speed / self.config.steps_per_unit)
        await asyncio.sleep(min(move_time, 5.0))  # cap simulation time
        self.current_position = target
        self.state = DriveState.REACHED

    async def home(self) -> None:
        self.state = DriveState.HOMING
        await asyncio.sleep(2.0)
        self.current_position = 0.0
        self.state = DriveState.IDLE

    @property
    def at_min_limit(self) -> bool:
        return self.current_position <= 0

    @property
    def at_max_limit(self) -> bool:
        return self.current_position >= 100.0
```

### 11.3 End-to-End Test Checklist

1. [ ] Mosquitto running and accessible from Windows (`mosquitto_sub -h PI_IP -t '#'`)
2. [ ] Drive controller service starts without errors
3. [ ] Health beacon appears on `health/pi` every 2 seconds
4. [ ] Move command triggers drive movement and position updates
5. [ ] `state="reached"` published when drive completes
6. [ ] Windows capture triggers after receiving `reached`
7. [ ] Stop command immediately halts drive
8. [ ] Home command moves to limit and resets position
9. [ ] LWT publishes `{"online": false}` when Pi disconnects
10. [ ] Limit switch triggers stop and fault state
11. [ ] Service auto-restarts after crash

### 11.4 Verify from Windows Side

Once the Pi is running, start the Windows app and check:

```bash
# Check MQTT status
curl http://localhost:8000/api/mqtt/status

# Start a test sequence
curl -X POST http://localhost:8000/api/mqtt/sequence/start \
  -H "Content-Type: application/json" \
  -d '{"file": "config/sequences/example_grid_scan.yaml"}'

# Watch progress
curl http://localhost:8000/api/mqtt/status
```

---

## 12. Troubleshooting

### MQTT Connection Refused
```bash
# Check Mosquitto is running
sudo systemctl status mosquitto

# Check it's listening on all interfaces
ss -tlnp | grep 1883
# Should show: 0.0.0.0:1883

# Check firewall
sudo ufw status
sudo ufw allow 1883
```

### GPIO Permission Denied
```bash
# Add user to gpio group
sudo usermod -aG gpio pi
# Log out and back in

# Verify lgpio works
python3 -c "import lgpio; print(lgpio.gpiochip_open(0))"
```

### Drive Not Moving
1. Check `enable_pin` is LOW (active-low): `lgpio.gpio_read(h, enable_pin)` should be 0
2. Check motor power supply is on
3. Verify step pulses with oscilloscope or LED on step pin
4. Check microstepping DIP switches on driver board

### Health Beacon Not Appearing
```bash
# Check from Pi locally
mosquitto_sub -h localhost -t 'health/pi' -v

# Check from Windows
mosquitto_sub -h PI_IP -t 'health/#' -v
```

### Windows Not Receiving Messages
1. Verify `config/mqtt.yaml` has correct Pi IP address
2. Check Windows firewall allows outbound TCP 1883
3. Test with `mosquitto_pub`/`mosquitto_sub` tools first
4. Check `http://localhost:8000/api/mqtt/status` for connection state

---

## Pydantic Models Reference

Copy the models below to your Pi project. They must match exactly with the Windows side.

```python
"""models.py — shared message models (must match Windows side exactly)."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MoveCommand(BaseModel):
    sequence_id: str
    drive_axis: Literal["a", "b"]
    target_position: float
    speed: float = 1.0
    timestamp: datetime = Field(default_factory=_now)


class HomeCommand(BaseModel):
    sequence_id: str
    drive_axis: Literal["a", "b"]
    timestamp: datetime = Field(default_factory=_now)


class StopCommand(BaseModel):
    sequence_id: str | None = None
    drive_axis: Literal["a", "b"] | None = None
    timestamp: datetime = Field(default_factory=_now)


class DrivePosition(BaseModel):
    sequence_id: str | None = None
    drive_axis: Literal["a", "b"]
    current_position: float
    target_position: float | None = None
    state: Literal["idle", "moving", "reached", "fault", "homing"]
    timestamp: datetime = Field(default_factory=_now)


class PiHealth(BaseModel):
    online: bool = True
    cpu_temp_c: float = 0.0
    uptime_s: int = 0
    drive_states: dict[str, str] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_now)


class DriveError(BaseModel):
    sequence_id: str | None = None
    drive_axis: Literal["a", "b"]
    error_type: Literal["stall", "limit_switch", "timeout", "gpio_fault"]
    message: str
    timestamp: datetime = Field(default_factory=_now)
```

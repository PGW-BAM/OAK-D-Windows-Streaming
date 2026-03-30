# PRD: MQTT-Based Drive/Camera Synchronization System

**Version**: 1.0  
**Status**: Draft  
**Last Updated**: 2026-03-13

---

## 1. Problem Statement

Two Luxonis OAK-D 4 Pro cameras are mounted on mechanical positioning drives (4 drives total, 2 per camera) controlled by a Raspberry Pi 5 via GPIO. The cameras are connected to a Windows 11 machine via a PoE++ network switch. An **existing** camera control application on the Windows machine already handles camera discovery, pipeline setup, preview streaming, and interval-based image capture.

There is currently no coordination layer between the drive positioning (Pi) and the camera capture (Windows). The system needs a reliable, low-latency communication channel to synchronize "move to position → confirm arrival → capture image" workflows, track the health of all components, and alert operators when something goes wrong.

**This PRD specifies an extension to the existing camera application, not a replacement.**

## 2. Goals

### Primary Goals
- **G1**: Reliable command/response coordination between the existing Windows camera app and a new Pi drive controller, with <50ms messaging latency on LAN
- **G2**: Automated capture sequences — define a list of positions, the system works through them using the existing capture pipeline
- **G3**: Real-time connectivity monitoring visible as an overlay on the existing camera preview stream
- **G4**: Email notifications when errors or disconnects occur, configurable by the operator

### Non-Goals (Phase 1)
- Web-based remote control
- Multi-broker clustering or WAN operation
- Machine vision / automated crack detection (separate project)
- Replacing or rewriting the existing camera control code

## 3. System Architecture

### 3.1 Component Overview

| Component             | Platform | Status    | Role                                             |
|-----------------------|----------|-----------|--------------------------------------------------|
| Camera Control App    | Win 11   | **Exists**| DepthAI pipelines, capture, preview, storage     |
| MQTT Orchestrator     | Win 11   | **New**   | State machine wired into existing capture flow   |
| Connectivity Monitor  | Win 11   | **New**   | Health tracking, preview overlay, email alerts   |
| Mosquitto Broker      | RPi 5    | **New**   | MQTT message routing                             |
| Drive Controller      | RPi 5    | **New**   | GPIO drive control, MQTT client                  |

### 3.2 Integration Diagram

```
┌──────────────────────────────────────────────────────────┐
│  Windows 11 — Existing Camera Application                │
│                                                          │
│  ┌───────────────────────────────────────────────┐       │
│  │ Existing Code (DO NOT MODIFY core logic)      │       │
│  │  • DepthAI device discovery + pipeline        │       │
│  │  • Interval-based capture                     │       │
│  │  • Preview streaming / display                │       │
│  │  • Image storage                              │       │
│  └──────────┬───────────────────┬────────────────┘       │
│             │ extends           │ reads frames           │
│  ┌──────────▼─────────┐ ┌──────▼──────────────────┐     │
│  │ NEW: MQTT           │ │ NEW: Connectivity        │     │
│  │ Orchestrator        │ │ Monitor                  │     │
│  │                     │ │                          │     │
│  │ • Move commands     │ │ • Health subscription    │     │
│  │ • Position wait     │ │ • Status aggregation     │     │
│  │ • Settling delay    │ │ • Preview overlay        │     │
│  │ • Capture trigger   │ │ • Email alerts           │     │
│  │   (calls existing)  │ │ • SQLite history         │     │
│  └──────────┬──────────┘ └──────────┬──────────────┘     │
│             │                       │                    │
│  ┌──────────▼───────────────────────▼──────────────┐     │
│  │ NEW: Shared MQTT Layer                          │     │
│  │  • aiomqtt async client wrapper                 │     │
│  │  • Pydantic message models                      │     │
│  │  • Topic constants                              │     │
│  │  • Auto-reconnect, LWT, JSON serialization      │     │
│  └──────────┬──────────────────────────────────────┘     │
└─────────────┼────────────────────────────────────────────┘
              │ MQTT over TCP (port 1883)
              │ via PoE++ LAN
┌─────────────▼────────────────────────────────────────────┐
│  Raspberry Pi 5                                          │
│                                                          │
│  ┌────────────────┐  ┌─────────────────────────────┐     │
│  │ Mosquitto      │  │ NEW: Drive Controller        │     │
│  │ Broker         │  │                              │     │
│  │ (port 1883)    │  │ • GPIO step/dir/enable       │     │
│  │                │  │ • MQTT command subscriber     │     │
│  │                │  │ • Position publisher          │     │
│  │                │  │ • Health beacon (2s)          │     │
│  │                │  │ • Error reporting             │     │
│  └────────────────┘  └─────────────────────────────┘     │
│                        │                                 │
│                        ▼                                 │
│                   4× GPIO Drives                         │
│                   (2 per camera: axis A + axis B)        │
└──────────────────────────────────────────────────────────┘
```

### 3.3 Why MQTT

- **Decoupled**: Pi and Windows communicate via topics, no direct socket management
- **QoS guarantees**: QoS 1 ensures commands arrive even through brief network hiccups
- **Retained messages**: Reconnecting clients immediately get last known state
- **Last Will and Testament**: Instant disconnect detection without polling
- **Lightweight**: Mosquitto runs comfortably on Pi 5 alongside drive control
- **Battle-tested**: Proven in industrial IoT with similar requirements
- **Familiar**: Operator has MQTT experience from ioBroker/Victron ESS automation

### 3.4 Network Topology

All devices on the same LAN subnet. PoE++ switch provides power to cameras and connectivity.

```
Windows 11 ──┐
OAK-D #1 ────┤── PoE++ Switch ──── Raspberry Pi 5
OAK-D #2 ────┘
```

MQTT over TCP 1883 (optionally 8883 with TLS). Camera data flows over DepthAI/XLink directly between cameras and Windows PC — MQTT carries only coordination messages, not image data.

## 4. MQTT Topic Schema

### 4.1 Complete Topic Tree

```yaml
# ── Commands (Windows → Pi) ──────────────
cmd/drives/{cam_id}/move          # MoveCommand     — QoS 1
cmd/drives/{cam_id}/home          # HomeCommand     — QoS 1
cmd/drives/{cam_id}/stop          # StopCommand     — QoS 1

# ── Drive Status (Pi → Windows) ──────────
status/drives/{cam_id}/position   # DrivePosition   — QoS 1, retained

# ── Camera Status (Windows → broker) ─────
status/cameras/{cam_id}/state     # CameraStatus    — QoS 1, retained

# ── Health Beacons ────────────────────────
health/pi                         # PiHealth        — QoS 0, LWT
health/win_controller             # WinHealth       — QoS 0, LWT
health/cameras/{cam_id}           # CameraHealth    — QoS 0

# ── Errors ────────────────────────────────
error/drives/{cam_id}             # DriveError      — QoS 1
error/cameras/{cam_id}            # CameraError     — QoS 1
error/orchestration/{event}       # OrchError       — QoS 1

# ── Monitoring ────────────────────────────
monitoring/connectivity           # ConnState       — QoS 1, retained

# ── Configuration ─────────────────────────
config/sequence/active            # CaptureSequence — QoS 1, retained
```

`{cam_id}` = `cam1` | `cam2`  
`{event}` = `timeout` | `sequence_abort`

### 4.2 QoS & Retention Strategy

| Topic Pattern                    | QoS | Retained | Rationale                                      |
|----------------------------------|-----|----------|-------------------------------------------------|
| `cmd/drives/+/*`                 | 1   | No       | Commands must arrive; stale commands are dangerous|
| `status/drives/+/position`       | 1   | Yes      | Reconnecting clients need current position       |
| `status/cameras/+/state`         | 1   | Yes      | Reconnecting clients need camera state           |
| `health/*`                       | 0   | No       | Frequent, loss-tolerant; LWT covers gaps         |
| `error/**`                       | 1   | No       | Errors must arrive for alerting                  |
| `monitoring/connectivity`        | 1   | Yes      | Dashboard needs state on connect                 |
| `config/sequence/active`         | 1   | Yes      | UI needs to show active sequence                 |

### 4.3 Message Payloads

All payloads are JSON, validated by Pydantic v2 models on both ends.

```python
# ── Commands ──────────────────────────────

class MoveCommand(BaseModel):
    sequence_id: str                    # UUID for correlation
    drive_axis: Literal["a", "b"]       # Which axis
    target_position: float              # Drive-native units (degrees, mm, steps)
    speed: float = 1.0                  # 0.0–1.0 normalized
    timestamp: datetime

class HomeCommand(BaseModel):
    sequence_id: str
    drive_axis: Literal["a", "b"]
    timestamp: datetime

class StopCommand(BaseModel):
    sequence_id: str | None = None
    drive_axis: Literal["a", "b"] | None = None  # None = stop all drives on camera
    timestamp: datetime

# ── Status ────────────────────────────────

class DrivePosition(BaseModel):
    sequence_id: str | None = None
    drive_axis: Literal["a", "b"]
    current_position: float
    target_position: float | None = None
    state: Literal["idle", "moving", "reached", "fault", "homing"]
    timestamp: datetime

class CameraStatus(BaseModel):
    cam_id: str
    state: Literal["online", "offline", "capturing", "error"]
    fps: float | None = None
    resolution: str | None = None
    timestamp: datetime

# ── Health ────────────────────────────────

class PiHealth(BaseModel):
    online: bool = True
    cpu_temp_c: float = 0.0
    uptime_s: int = 0
    drive_states: dict[str, str]       # "cam1:a" → "idle"
    timestamp: datetime

class WinControllerHealth(BaseModel):
    online: bool = True
    cameras_connected: list[str]
    active_sequence: str | None = None
    timestamp: datetime

class CameraHealth(BaseModel):
    cam_id: str
    online: bool = True
    ip_address: str | None = None
    mx_id: str | None = None
    timestamp: datetime

# ── Errors ────────────────────────────────

class DriveError(BaseModel):
    sequence_id: str | None = None
    drive_axis: Literal["a", "b"]
    error_type: Literal["stall", "limit_switch", "timeout", "gpio_fault"]
    message: str
    timestamp: datetime

class CameraError(BaseModel):
    cam_id: str
    error_type: str
    message: str
    timestamp: datetime

class OrchestrationError(BaseModel):
    event: str                         # "timeout" | "sequence_abort"
    sequence_id: str | None = None
    cam_id: str | None = None
    message: str
    timestamp: datetime

# ── Monitoring ────────────────────────────

class ConnectivityState(BaseModel):
    pi_online: bool
    broker_connected: bool
    cameras: dict[str, str]            # cam_id → state
    drives: dict[str, str]             # "cam1:a" → state
    last_update: datetime

# ── Sequences ─────────────────────────────

class PositionTarget(BaseModel):
    drive_a: float
    drive_b: float

class CaptureStep(BaseModel):
    cam_id: str
    position: PositionTarget
    settling_delay_ms: int = 150

class CaptureSequence(BaseModel):
    sequence_id: str
    name: str
    mode: Literal["sequential", "parallel"] = "sequential"
    steps: list[CaptureStep]
    repeat_count: int = 1

# ── Alerts ────────────────────────────────

class AlertEvent(BaseModel):
    alert_type: str
    severity: Literal["critical", "high", "medium", "low"]
    component: str
    message: str
    system_state: ConnectivityState | None = None
    timestamp: datetime
```

## 5. Core Workflow: Move → Confirm → Capture

### 5.1 Sequence Diagram

```
Existing Camera App                 MQTT Broker              Pi Drive Controller
+ MQTT Orchestrator                     │                          │
       │                                │                          │
       │  PUB cmd/drives/cam1/move      │                          │
       │  {seq_id, axis:"a", pos:45.0}  │                          │
       │───────────────────────────────►│                          │
       │                                │  DELIVER ───────────────►│
       │                                │                          │
       │                                │        [GPIO: step/dir]  │
       │                                │                          │
       │                                │  PUB status/.../position │
       │                                │  state="moving"          │
       │                                │◄─────────────────────────│
       │  DELIVER (state=moving)        │                          │
       │◄───────────────────────────────│                          │
       │                                │                          │
       │      ... drive moving ...      │    ... drive moving ...  │
       │                                │                          │
       │                                │  PUB status/.../position │
       │                                │  state="reached"         │
       │                                │◄─────────────────────────│
       │  DELIVER (state=reached)       │                          │
       │◄───────────────────────────────│                          │
       │                                │                          │
       │  [wait settling: 150ms]        │                          │
       │                                │                          │
       │  [CALL existing capture()]     │                          │
       │  [STORE image + metadata]      │                          │
       │                                │                          │
       │  → next step in sequence       │                          │
```

### 5.2 Integration with Existing Capture

The orchestrator does NOT implement its own capture logic. It:
1. Imports or calls the existing capture function/method from the camera app
2. Adds drive position metadata to the image storage
3. Manages the sequencing loop around the existing capture mechanism

If the existing app captures via `camera.capture()` → the orchestrator calls `camera.capture()`.
If the existing app captures by reading a DepthAI output queue → the orchestrator reads the same queue at the right moment.

The cam-integration agent must study the existing capture flow and wire in accordingly.

### 5.3 Dual Mode Operation

The existing interval-based capture must continue to work when no MQTT sequence is active:

| Mode              | Trigger                  | Behavior                                  |
|-------------------|--------------------------|-------------------------------------------|
| **Standalone**    | Default / broker offline | Existing interval capture, no MQTT        |
| **MQTT Sequenced**| Sequence loaded + started| Orchestrator controls timing and positions |

Switching between modes: config flag, CLI argument, or runtime command. The orchestrator can be enabled/disabled without restarting the camera app.

### 5.4 Timeout and Error Handling

| Condition                  | Timeout | Action                                               |
|----------------------------|---------|-------------------------------------------------------|
| Move command no PUBACK     | 5s      | Retry once, then error                               |
| Drive not reached target   | 30s     | Publish stop, report to `error/drives/{cam_id}`      |
| Camera capture failure     | 10s     | Retry once, then skip position and log               |
| Pi heartbeat lost          | 6s      | Monitor marks offline; alert if >10s                 |
| Camera health lost         | 6s      | Monitor marks offline; alert if >15s                 |
| Broker connection lost     | —       | Auto-reconnect exponential backoff (1s–30s)          |
| Broker unreachable at start| —       | Run in standalone mode, retry connection in background|

### 5.5 Settling Time

Mechanical drives vibrate after reaching position. After the Pi reports `state="reached"`, the orchestrator waits a **configurable settling delay** (default: 150ms, range: 50–500ms) before triggering capture. This value should be tuned per-installation.

### 5.6 Sequence Definition Format

```yaml
sequence_id: "grid-scan-001"
name: "5x5 Grid Scan — Camera 1"
mode: "sequential"
repeat_count: 1
steps:
  - cam_id: cam1
    position: { drive_a: 0.0, drive_b: 0.0 }
    settling_delay_ms: 150
  - cam_id: cam1
    position: { drive_a: 10.0, drive_b: 0.0 }
    settling_delay_ms: 150
  # ... more positions
```

## 6. Raspberry Pi Drive Controller

### 6.1 GPIO Drive Interface

Each camera has 2 drives (axis A and axis B). Each drive is controlled via:
- **Step pin**: Pulse output for stepper motor
- **Direction pin**: High/low for CW/CCW
- **Enable pin**: Active-low driver enable
- **Limit switches**: Min and max (normally closed, pulled high)

Pin assignments in YAML config, never hardcoded.

### 6.2 Drive Operations

| Operation | MQTT Topic              | Behavior                                          |
|-----------|-------------------------|---------------------------------------------------|
| Move      | `cmd/drives/+/move`     | Move to absolute position at given speed           |
| Home      | `cmd/drives/+/home`     | Move toward limit switch, reset zero               |
| Stop      | `cmd/drives/+/stop`     | Immediately stop stepping, disable driver          |

### 6.3 Position Reporting

The Pi publishes `DrivePosition` on every state transition:
- `idle` → `moving` (command received)
- `moving` → `reached` (target reached + settling delay elapsed)
- `reached` → `idle` (ready for next command)
- Any → `fault` (error detected)
- Any → `homing` (home command received)

Messages are **retained** so reconnecting clients get current position.

### 6.4 Health Beacon

Published to `health/pi` every 2 seconds (QoS 0):
```json
{
  "online": true,
  "cpu_temp_c": 52.3,
  "uptime_s": 86400,
  "drive_states": {"cam1:a": "idle", "cam1:b": "idle", "cam2:a": "idle", "cam2:b": "idle"},
  "timestamp": "2026-03-13T14:30:00Z"
}
```

LWT payload on disconnect: `{"online": false}`

### 6.5 Stall Detection

If a drive is commanded to move but doesn't reach the target within the expected time (calculated from distance and speed), the controller:
1. Publishes `DriveError` with `error_type="stall"`
2. Sets drive state to `fault`
3. Disables the driver (safety)

## 7. Connectivity Monitoring & Overlay

### 7.1 Component Tracking

| Component       | Detection Method              | Healthy    | Alert Threshold |
|-----------------|-------------------------------|------------|-----------------|
| Pi controller   | MQTT heartbeat (2s interval)  | <6s gap    | >10s            |
| MQTT broker     | Client connection state       | Connected  | >10s disconnect |
| Camera 1        | DepthAI status + MQTT health  | Online     | >15s offline    |
| Camera 2        | DepthAI status + MQTT health  | Online     | >15s offline    |
| Drive cam1/a    | Position status messages      | idle/moving| fault state     |
| Drive cam1/b    | Position status messages      | idle/moving| fault state     |
| Drive cam2/a    | Position status messages      | idle/moving| fault state     |
| Drive cam2/b    | Position status messages      | idle/moving| fault state     |

### 7.2 Streaming Overlay Layout

The overlay is composited onto the **existing** camera preview. It must not require a separate window.

```
┌──────────────────────────────────────────────┐
│ [Existing Camera Preview]                    │
│                                              │
│  ┌─ Status Bar (semi-transparent) ─────────┐ │
│  │ ● Pi  ● MQTT  ● Cam1  ● Cam2           │ │
│  │ Drives: A=45.2° B=12.0mm               │ │
│  │ Sequence: 14/50  ▶ Running              │ │
│  └─────────────────────────────────────────┘ │
│                                              │
│         [existing camera content]            │
│                                              │
│  ┌─ Error Bar (shown on error only) ───────┐ │
│  │ ⚠ Drive cam2/a: stall detected (12s ago)│ │
│  └─────────────────────────────────────────┘ │
└──────────────────────────────────────────────┘
```

- ● = filled circle: green (OK), yellow (degraded), red (offline/fault)
- Semi-transparent background (alpha ~0.6)
- Toggle key: `H` (or UI button if framework supports it)
- Overlay data updates at 1 Hz, not every frame
- Overlay rendering must add <2ms per frame

### 7.3 Uptime History (SQLite)

```sql
CREATE TABLE connectivity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    state TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    duration_s REAL
);

CREATE TABLE alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT,
    email_sent BOOLEAN DEFAULT FALSE,
    timestamp DATETIME NOT NULL
);
```

Rolling 24-hour window. Used for uptime statistics and debugging.

## 8. Email Notification System

### 8.1 Configuration

```yaml
alerts:
  enabled: true
  email: ""  # Operator fills in at first run or in this file

  smtp:
    host: "smtp.example.com"
    port: 587
    use_tls: true
    username: "alerts@example.com"
    password: ""  # Or env var OAK_SMTP_PASSWORD

  thresholds:
    pi_offline_s: 10
    camera_offline_s: 15
    broker_offline_s: 10

  dedup_window_s: 300       # Same alert type max once per 5 min
  max_alerts_per_hour: 20   # Rate limit safety valve
```

### 8.2 Alert Types

| Alert               | Trigger                            | Severity |
|----------------------|------------------------------------|----------|
| `pi_offline`         | Pi heartbeat lost > threshold      | Critical |
| `camera_offline`     | Camera unreachable > threshold     | High     |
| `broker_offline`     | MQTT connection lost > threshold   | Critical |
| `drive_fault`        | Any drive reports fault state      | High     |
| `sequence_aborted`   | Capture sequence stopped on error  | Medium   |
| `capture_failure`    | Capture failed after retry         | Medium   |

### 8.3 Email Content

Subject: `[OAK-Drive-Sync] {SEVERITY}: {alert_type} — {component}`

Body includes (via Jinja2 template):
- Alert description and component
- Timestamp
- Current system state snapshot (all components)
- Suggested remediation steps per alert type
- Link/reference to logs

### 8.4 First-Run Setup

If `alerts.email` is empty on first run:
1. Console prompt: "Enter email for error notifications (Enter to skip):"
2. Basic format validation
3. Test email sent if SMTP configured
4. Saved to config file

### 8.5 Fallback

If SMTP send fails, the alert is written to `logs/unsent_alerts.jsonl` with full payload. Operator can review manually.

## 9. MQTT Client Requirements

### 9.1 Shared Client Wrapper

Both Pi and Windows use the same reusable async MQTT client with:
- **Auto-reconnect**: Exponential backoff (1s → 30s), infinite retries
- **LWT**: Configured per client for disconnect detection
- **Pydantic integration**: `publish(topic, model)` serializes to JSON; handlers receive parsed dicts that can be validated into models
- **Structured logging**: Every connect/disconnect/publish/subscribe/error logged via structlog
- **Topic matching**: Wildcard support for handler dispatch (`+`, `#`)
- **Graceful degradation**: If broker is unreachable, client queues critical messages and retries

### 9.2 Connection Parameters

| Parameter        | Value                          |
|------------------|--------------------------------|
| Protocol         | MQTT 5.0                       |
| Port             | 1883 (8883 with TLS)           |
| Keep-alive       | 30s                            |
| Clean session     | False (persistent session)    |
| Reconnect min    | 1s                             |
| Reconnect max    | 30s                            |
| QoS default      | 1 (commands/status), 0 (health)|

## 10. Configuration Files Needed

| File                                  | Contents                                         |
|---------------------------------------|--------------------------------------------------|
| `config/drive_pinmap.yaml`            | GPIO pin assignments per drive, settling delay    |
| `config/camera_config.yaml` (extend)  | Add MQTT broker host/port, orchestration params   |
| `config/monitoring_config.yaml`       | Alert thresholds, SMTP, overlay settings          |
| `config/mosquitto.conf`               | Broker config for the Pi                          |
| `config/sequences/*.yaml`             | Capture sequence definitions                      |
| `config/email_templates/alert.txt.j2` | Jinja2 email template                             |

## 11. Dependencies to Add

```
aiomqtt>=2.0.0          # Async MQTT client
pydantic>=2.5.0         # Message models (may already be in project)
structlog>=24.0.0       # Structured logging
pyyaml>=6.0             # Config files (may already be in project)
aiosmtplib>=3.0.0       # Async email sending
jinja2>=3.1.0           # Email templates
aiosqlite>=0.20.0       # Monitoring history database

# Pi only:
gpiozero>=2.0           # GPIO control
lgpio>=0.2.0            # Low-level GPIO (gpiozero backend on Pi 5)

# Windows only (likely already present):
depthai>=2.25.0
opencv-python>=4.9.0
```

## 12. Reliability Requirements

| Requirement                           | Target           |
|---------------------------------------|------------------|
| MQTT message delivery (QoS 1)        | 99.9% on LAN     |
| Disconnect detection latency          | <6s              |
| Auto-reconnect after network glitch   | <5s              |
| False positive alert rate             | <1/day           |
| Capture sequence completion rate      | >99% (with retries) |
| Overlay rendering overhead            | <2ms/frame       |
| Standalone mode (no broker)           | Fully functional existing behavior |

## 13. Phasing

### Phase 1 (MVP)
- Mosquitto broker on Pi with basic config
- Drive controller with GPIO + MQTT
- MQTT orchestration wired into existing camera app
- Connectivity monitoring with streaming overlay
- Email alerts for critical errors
- Sequential single-camera capture sequences
- Standalone mode preserved

### Phase 2
- Dual-camera parallel mode
- NiceGUI/web dashboard for monitoring
- TLS encryption on MQTT
- Capture sequence editor UI
- Historical uptime analytics view

### Phase 3
- Integration with fatigue crack analysis pipeline
- REST API for remote control
- Webhook notifications (Slack, Matrix)
- Multi-sequence queueing

# OAK-D Drive Sync — Claude Code Project Instructions

## Context

This is an **existing** camera control project for 2× Luxonis OAK-D 4 Pro cameras connected via PoE++ to a Windows 11 machine. The cameras are mounted on mechanical positioning drives (4 drives total, 2 per camera) controlled by a Raspberry Pi 5 via GPIO.

The Windows side now has a complete MQTT integration layer. The Pi drive controller is documented but not yet implemented (see `docs/RASPBERRY_PI_IMPLEMENTATION.md`).

## System Topology

```
Windows 11 PC (169.254.x.x) ──┐
  - Camera app (FastAPI)        │
  - MQTT orchestration          ├── PoE++ Switch (169.254.0.0/16)
  - Monitoring + email alerts   │
                                │
OAK-D 4 Pro #1 (169.254.236.75)┤
OAK-D 4 Pro #2 (169.254.106.74)┤
                                │
Raspberry Pi 5 ─────────────────┘
  eth0: 169.254.10.10/16 (static — PoE LAN, Mosquitto broker)
  wlan0: DHCP (WiFi — internet)
  - 4× GPIO drives (not yet implemented)
```

## What Is Implemented

### Windows side (complete)
- Camera discovery, pipeline setup, MJPEG streaming, recording via DepthAI SDK
- React frontend with draggable camera grid, controls, detection overlay
- **MQTT client** with auto-reconnect, LWT, Pydantic serialization (`backend/mqtt/client.py`)
  - Uses dedicated `SelectorEventLoop` thread for Windows compatibility
- **Orchestrator** — move→settle→capture state machine (`backend/mqtt/orchestrator.py`)
- **Connectivity monitor** — health tracking, threshold alerting (`backend/mqtt/monitor.py`)
- **Email alerts** — SMTP + Jinja2 + JSONL fallback (`backend/mqtt/alerts.py`)
- **SQLite history** — 24h rolling connectivity/alert log (`backend/mqtt/history.py`)
- **REST API** — `/api/mqtt/status`, `/api/mqtt/sequence/*`, `/api/mqtt/history/*`
- **Config** — `config/mqtt.yaml` (broker, orchestration, alerts, SMTP)
- **IMU-driven calibration** — live accelerometer-based roll/pitch per camera,
  with operator-taught control presets that auto-apply by nearest angle
  (`backend/calibration.py`, `config/calibration.json`).
- **Radial-angle teach mode** — operator-taught IMU angles per checkpoint for
  closed-loop radial-drive correction (`backend/angle_targets.py`,
  `config/angle_targets.json`). REST endpoints at `/api/angle_targets/*`.
  Orchestrator injects `target_angle_deg` + `resync_position` into axis-b
  `MoveCommand`s so the Pi runs iterative converge-and-resync.

### Pi side (documented, not coded)
- `docs/RASPBERRY_PI_IMPLEMENTATION.md` — full guide with GPIO wiring, Mosquitto setup, drive controller reference code, systemd services
- Mosquitto broker running at `169.254.10.10:1883`

## What Still Needs Work

1. **Pi drive controller implementation** — GPIO stepper control + MQTT client (documented in `docs/RASPBERRY_PI_IMPLEMENTATION.md`)
2. **Streaming overlay** — render connectivity status onto camera preview (Phase 2)
3. **cam_id mapping by serial number** — currently positional (cam1=first discovered)
4. **First-run email prompt** — operator must manually edit `config/mqtt.yaml`

**Before writing any code**, every agent MUST first explore the existing codebase to understand the current architecture, file layout, naming conventions, and patterns. Then integrate — don't rewrite.

## Agent Architecture

| Agent              | Scope                                              |
|--------------------|-----------------------------------------------------|
| `pi-controller`    | Raspberry Pi GPIO drive code, MQTT client, health   |
| `cam-integration`  | Wire MQTT orchestration into existing camera app    |
| `mqtt-infra`       | Broker config, topic schema, shared models/helpers  |
| `monitoring`       | Connectivity dashboard, streaming overlay, alerts   |
| `integration-test` | End-to-end test harness, simulation, CI             |

## Memory

This project uses the **`thedotmack/claude-mem`** plugin for persistent memory.
Do NOT create or use `memory.md` files — all persistent context is managed via claude-mem.

Key memory namespaces:
- `project:architecture` — system design decisions, existing codebase findings
- `project:mqtt-topics` — canonical topic tree
- `project:hardware` — pin mappings, camera serial numbers, network addresses
- `project:existing-code` — notes on the existing camera app structure, key classes, entry points
- `project:issues` — known bugs and workarounds

## Coding Conventions

- **Match the existing project's style** — check indentation, naming, import patterns before writing
- Python 3.11+ with type hints
- asyncio-first for all new MQTT/network code
- Pydantic v2 models for MQTT message payloads
- Structured logging via `structlog` (or match existing logger if one is in use)
- All MQTT topics as constants, never hardcoded strings

## Integration Principles

1. **Don't replace, extend.** The existing camera code works. New functionality plugs into it.
2. **Discover first.** Every agent reads the codebase before writing. Store findings in claude-mem under `project:existing-code`.
3. **Minimal coupling.** MQTT integration should be injectable — the camera app can run without MQTT (graceful degradation).
4. **Shared models.** All MQTT payloads use Pydantic models in a shared location so both Pi and Windows code stay in sync.
5. **Config-driven.** No hardcoded IPs, pins, serial numbers, or thresholds. Everything in YAML config files.

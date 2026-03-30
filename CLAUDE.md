# OAK-D Drive Sync — Claude Code Project Instructions

## Context

This is an **existing** camera control project for 2× Luxonis OAK-D 4 Pro cameras connected via PoE++ to a Windows 11 machine. The cameras are mounted on mechanical positioning drives (4 drives total, 2 per camera) controlled by a Raspberry Pi 5 via GPIO.

**The goal of this extension** is to add MQTT-based coordination between the Windows camera control software and the Pi drive controller, plus connectivity monitoring, a streaming overlay, and email alerting.

## System Topology

```
┌─────────────────────┐       PoE++ Switch        ┌──────────────────┐
│  Windows 11 PC      │◄──────────────────────────►│ OAK-D 4 Pro #1  │
│  - Existing cam app  │◄──────────────────────────►│ OAK-D 4 Pro #2  │
│  - MQTT orchestration│       Ethernet             │                  │
│  - Monitoring/overlay│◄──────────────────────────►│ Raspberry Pi 5   │
│  - Email alerts      │       (MQTT over TCP)      │  - 4× GPIO drives│
└─────────────────────┘                             │  - MQTT client   │
                                                    │  - Mosquitto     │
                                                    └──────────────────┘
```

## What Already Exists

- Camera discovery, pipeline setup, image capture via DepthAI SDK
- Interval-based recording logic on the Windows machine
- Camera streaming / preview functionality

**Before writing any code**, every agent MUST first explore the existing codebase to understand the current architecture, file layout, naming conventions, and patterns. Use `find`, `grep`, and `cat` to map out what's there. Then integrate — don't rewrite.

## What Needs to Be Added

See `docs/PRD-MQTT.md` for the full specification. In summary:

1. **MQTT communication layer** — Mosquitto broker on Pi, async clients on both sides
2. **Pi drive controller** — GPIO-based drive positioning, MQTT command listener, health beacons
3. **Orchestration** — move→confirm→settle→capture workflow wired into the existing camera app
4. **Connectivity monitoring** — track health of all components via MQTT heartbeats
5. **Streaming overlay** — render connection status, drive positions, sequence progress onto camera preview
6. **Email alerts** — notify operator on failures (configurable address, SMTP, deduplication)

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

# OAK-D Streaming Dashboard — Project State

**Last updated:** 2026-04-13
**Status:** Camera streaming MVP complete + MQTT drive sync integration implemented

---

## What Was Built

A full-stack multi-camera OAK-D 4 Pro browser streaming dashboard, implementing the PRD in `compass_artifact_wf-ded0aeb2-1c12-4063-b8f7-843dc21db55b_text_markdown.md`.

### Stack
| Layer | Technology | Version |
|-------|-----------|---------|
| Python env | uv | 0.10.9 |
| Backend | FastAPI + Uvicorn | 0.135 / 0.41 |
| Camera SDK | DepthAI | 3.4.0 |
| Video mux | PyAV | 16.1 |
| Image proc | OpenCV-Python | 4.13 |
| Data models | Pydantic v2 + pydantic-settings | 2.12 |
| MQTT client | aiomqtt (paho-mqtt backend) | 2.5.1 |
| Async SMTP | aiosmtplib | 5.1.0 |
| Async SQLite | aiosqlite | 0.22.1 |
| Templating | Jinja2 | 3.1+ |
| Logging | structlog | 25.5.0 |
| Config | PyYAML | 6.0+ |
| Frontend build | Vite 7 + React 19 + TypeScript | — |
| Grid layout | react-grid-layout | 2.2.2 |

---

## Repository Structure

```
OAK-D_streaming/
├── start.bat                  # Double-click to run — activates .venv and starts backend
├── run.py                     # Uvicorn entry point (port 8000)
├── pyproject.toml             # uv project + dependency declarations
├── uv.lock                    # Pinned dependency lockfile
├── .venv/                     # Python 3.12 virtual environment (uv-managed)
├── .env                       # (optional) override settings from config.py
├── recordings/                # Default output directory for all recordings
│
├── backend/
│   ├── __init__.py
│   ├── config.py              # Pydantic-settings config (env-overridable)
│   ├── models.py              # All Pydantic request/response models
│   ├── camera_manager.py      # CameraManager + CameraWorker (thread-per-camera)
│   ├── recording.py           # VideoRecorder, IntervalRecorder, RecordingWorker
│   ├── main.py                # FastAPI app + all API endpoints + lifespan
│   └── mqtt/                  # MQTT drive sync integration
│       ├── __init__.py
│       ├── models.py          # 14 Pydantic MQTT message types (commands, status, health, errors)
│       ├── topics.py          # MQTT topic constants (single source of truth)
│       ├── config.py          # YAML + env-var config loader (broker, alerts, orchestration)
│       ├── client.py          # Async MQTT client (SelectorEventLoop thread for Windows)
│       ├── orchestrator.py    # Move → settle → capture state machine + sequence runner
│       ├── monitor.py         # Real-time component health tracking + threshold alerting
│       ├── history.py         # SQLite connectivity + alert history (24h rolling)
│       ├── alerts.py          # Email alerts via aiosmtplib + Jinja2 templates
│       └── service.py         # Top-level facade wiring all MQTT subsystems
│
├── config/
│   ├── mqtt.yaml              # Broker host/port, orchestration timeouts, SMTP, alert thresholds
│   ├── monitoring.yaml        # Component health definitions, overlay settings
│   ├── sequences/             # Capture sequence YAML definitions
│   │   └── example_grid_scan.yaml
│   └── email_templates/
│       └── alert.txt.j2       # Jinja2 email alert template
│
├── data/                      # (gitignored) SQLite connectivity database
├── logs/                      # (gitignored) Unsent alert fallback logs
│
├── frontend/
│   ├── package.json
│   ├── vite.config.ts         # Vite config + proxy (/api, /ws → localhost:8000)
│   ├── dist/                  # Production build (served by FastAPI at GET /)
│   └── src/
│       ├── App.tsx            # Root component — header + CameraGrid
│       ├── App.css / index.css
│       ├── types/index.ts     # TypeScript types mirroring backend models
│       ├── hooks/useCamera.ts # Data hooks + all API fetch helpers
│       └── components/
│           ├── CameraGrid.tsx      # react-grid-layout grid with preset layouts
│           ├── CameraCard.tsx      # Single camera tile (MJPEG img + status bar)
│           ├── DetectionOverlay.tsx # Canvas bounding box renderer
│           └── ControlPanel.tsx    # Right-side drawer: camera controls + recording + AI
│
├── docs/
│   ├── PRD-MQTT.md            # Full MQTT system specification
│   └── RASPBERRY_PI_IMPLEMENTATION.md  # Pi-side implementation guide
│
└── documentation/             # This folder
    ├── PROJECT_STATE.md       # ← you are here
    ├── ARCHITECTURE.md        # Detailed system architecture
    ├── API_REFERENCE.md       # All REST + WebSocket endpoints
    └── KNOWN_ISSUES.md        # Known issues and next steps
```

---

## How to Run

### Backend only
```bat
start.bat          # double-click — runs .venv\Scripts\python.exe run.py
```
Or from a terminal:
```bash
python -m uv run python run.py
```
Backend listens on `http://0.0.0.0:8000`. Opens browser to `http://localhost:8000` to see the pre-built React frontend.

### Frontend dev server (hot reload)
```bash
cd frontend
npm run dev        # http://localhost:5173  — proxies /api and /ws to :8000
```

### Rebuild frontend after changes
```bash
cd frontend
npm run build      # outputs to frontend/dist/ — then served by FastAPI
```

### Install optional host-GPU inference (Ultralytics + PyTorch)
```bash
python -m uv sync --extra host-inference
# Then set CUDA index if needed:
# uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```

---

## Camera Discovery

On startup, `CameraManager.discover()` calls `dai.Device.getAllAvailableDevices()` which scans USB and PoE (UDP broadcast). For PoE cameras:
- Camera default IP: `169.254.1.222`
- Host adapter must be on same subnet: e.g., `169.254.1.1 / 255.255.0.0`
- Multiple PoE cameras: assign static IPs via `poe_set_ip` or `oakctl`
- Windows Firewall: allow Python on the PoE adapter (UDP broadcast + TCP)

The `POST /api/cameras/discover` endpoint triggers a manual rescan at runtime.

---

## Network Topology

```
Windows 11 PC ─────┐
  169.254.x.x       │
                     ├── PoE++ Switch (169.254.0.0/16 link-local LAN)
OAK-D #1 ──────────┤
  169.254.236.75     │
OAK-D #2 ──────────┤
  169.254.106.74     │
                     │
Raspberry Pi 5 ─────┘
  eth0: 169.254.10.10/16 (static — PoE LAN, cameras + MQTT)
  wlan0: DHCP (WiFi — internet access)
```

- The PoE switch carries both camera data (DepthAI/XLink) and MQTT traffic
- The Pi runs Mosquitto broker on port 1883, accessible at `169.254.10.10`
- The Pi uses WiFi for internet; Ethernet is dedicated to the camera LAN

---

## MQTT Integration

The MQTT service starts automatically with the FastAPI app. If the broker is unreachable, the app falls back to standalone mode (camera streaming works normally without MQTT).

- **Broker:** Mosquitto on Raspberry Pi 5 at `169.254.10.10:1883`
- **Config:** `config/mqtt.yaml` (env-var overrides: `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`)
- **Windows fix:** The MQTT client runs in a dedicated thread with `asyncio.SelectorEventLoop` because Windows' default `ProactorEventLoop` doesn't support `add_reader`/`add_writer` (required by paho-mqtt)
- **Auto-reconnect:** Exponential backoff (1s → 30s), infinite retries
- **Health beacons:** Published every 2s to `health/win_controller` and `health/cameras/{id}`
- **Connectivity DB:** SQLite at `data/connectivity.db` (24h rolling window)

### New API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/mqtt/status` | GET | MQTT connection state, component health, sequence progress |
| `/api/mqtt/connectivity` | GET | Detailed connectivity state for all components |
| `/api/mqtt/sequence/start` | POST | Start a capture sequence (from YAML file or inline) |
| `/api/mqtt/sequence/stop` | POST | Stop the active sequence |
| `/api/mqtt/sequences` | GET | List available sequence YAML files |
| `/api/mqtt/history/connectivity` | GET | Query connectivity history |
| `/api/mqtt/history/alerts` | GET | Query alert history |

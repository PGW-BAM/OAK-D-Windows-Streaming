# OAK-D Streaming Dashboard — Project State

**Last updated:** 2026-03-09
**Status:** MVP scaffolding complete, ready for hardware testing

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
│   └── main.py                # FastAPI app + all API endpoints + lifespan
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

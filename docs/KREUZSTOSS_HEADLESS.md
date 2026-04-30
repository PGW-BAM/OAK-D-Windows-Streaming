# Kreuzstoß Headless — Project Spec

A minimal, GUI-less Windows program that runs the Kreuzstoß recording loop on
2× OAK-D 4 Pro cameras. Start the executable, the program calibrates the
cameras once, locks the resulting exposure / white balance, and then loops
the recording sequence indefinitely until stopped (Ctrl-C) or the disk fills
up.

No web UI, no REST API, no MQTT, no operator controls at runtime. Everything
is read from a config file at startup; everything else is logs on stdout.

---

## 1. Runtime behavior

### 1.1 At startup

1. Read `config.json` (save directory, file prefix, inter-cycle interval).
2. Discover both OAK-D 4 Pro cameras over PoE. Refuse to start unless
   exactly two cameras are present and reachable.
3. Assign stable `cam1` / `cam2` IDs (by serial / MxID — fixed mapping in
   the config, not positional).
4. Build each camera's pipeline at the **calibration preset**: 1080p
   MJPEG, 29 fps. Both cameras run concurrently at this low-bandwidth
   preset for the calibration phase.
5. **Auto-calibrate exposure and white balance** (see §2). When the values
   have converged, read them back and re-issue them as **manual / fixed**
   values. Auto loops are disabled for the rest of runtime.
6. Verify free disk space on the save directory is above the floor
   (default 2 GB). Abort with a clear error otherwise.
7. Enter the main loop.

### 1.2 Main loop

Repeat forever, one *cycle* at a time:

1. Run the recording sequence (§3).
2. Sleep `interval_seconds` (operator-configured, minimum 5 s).
3. Re-check disk free space; stop if below floor.
4. On Ctrl-C / SIGTERM: finish the current step gracefully, then exit.

If a single cycle fails (e.g. a camera misses its first frame after a
pipeline rebuild), log a warning and try again on the next cycle. After
N consecutive failures (default 5), exit with a non-zero status.

### 1.3 Logging

- Plain stdout logs, one line per step (`step 4/13: recording cam1 5 s @ 4K / 29 fps`).
- A rotating log file (`logs/kreuzstoss.log`, ~10 MB, 5 generations) for
  unattended runs.
- No external monitoring. The program is self-contained.

---

## 2. One-shot calibration on startup

The OAK-D's auto-exposure (AE) and auto-white-balance (AWB) loops produce
good values for a static scene within a few seconds. We use them once,
then freeze the result so subsequent cycles are bit-identical in look.

Procedure (per camera, both cameras in parallel):

1. Configure the camera at 1080p / 29 fps MJPEG (bandwidth-safe).
2. Enable **auto exposure** and **auto white balance** via the camera's
   control API.
3. Stream frames for a fixed warm-up window (default **5 seconds**) so the
   AE/AWB loops reach steady state.
4. Read back the converged values from the camera's frame metadata:
   `exposure_us`, `iso`, `white_balance_k`.
5. Apply those same values back to the camera as **manual** settings
   (`setManualExposure(exposure_us, iso)` and
   `setManualWhiteBalance(white_balance_k)`). Auto loops are now off.
6. Save the locked values to `calibration_last.json` next to the config so
   the operator can inspect what was used (informational only — the next
   run re-calibrates from scratch).

After this point, **no auto loops are ever re-enabled**. The recording
sequence only changes resolution / fps / MJPEG quality, never exposure or
white balance. This is the whole point of the program — guarantee
consistent look across every cycle of the loop.

If a camera fails to converge (no frame metadata after the warm-up
window), abort startup with a clear error rather than locking in garbage.

---

## 3. Recording sequence (one cycle)

Each cycle is a fixed 13-step sequence that records a 4K video clip + a
4K still + a 1080p high-fps clip from each camera in turn. **Only one
camera runs at high bandwidth at a time** — the other stays at the
low-bandwidth preset so the GbE PoE link doesn't saturate.

Tunables (defaults in parentheses):

| Knob                  | Default | Purpose                              |
|-----------------------|---------|--------------------------------------|
| `clip_4k_seconds`     | 5.0     | Length of the 4K clip per camera     |
| `clip_1080p_seconds` | 5.0     | Length of the 1080p / 59 fps clip    |
| `settle_after_rebuild_s` | 3.5  | Pause after a pipeline mode change   |
| `settle_after_record_s` | 2.0   | Pause after stopping a recording     |
| `settle_before_snapshot_s` | 1.0 | Pause before grabbing a still      |
| `settle_after_snapshot_s` | 1.0  | Pause after grabbing a still         |
| `ready_timeout_s`     | 40.0    | Max wait for first frame after rebuild |

### 3.1 Bandwidth presets

Three stream presets, chosen so any two cameras can run concurrently
without exceeding the PoE link:

| Preset name        | Resolution | FPS | MJPEG quality | Use                  |
|--------------------|------------|-----|---------------|----------------------|
| `low_bandwidth`    | 1080p      | 29  | 85            | The idle camera      |
| `high_4k`          | 4K         | 29  | 70            | Active camera, 4K    |
| `high_1080p_fast`  | 1080p      | 59  | 85            | Active camera, fast  |

The 4K preset uses MJPEG quality 70 (not 85+) because the OAK-D's MJPEG
quality knob is near-flat above 80 — q≥85 saturates the PoE link at 4K /
29 fps and produces stuck frames.

### 3.2 The 13 steps

Cam1 active phase:

1. **cam2 → low bandwidth.** Apply `low_bandwidth` to cam2 *first* so
   the link has headroom before cam1 ramps up. Wait for first frame +
   settle.
2. **cam1 → 4K.** Apply `high_4k` to cam1. Wait for first frame + settle.
3. **Record cam1, 5 s @ 4K / 29 fps.** Save as
   `<prefix>_cam1_4K_29fps_<timestamp>.mp4`.
4. **Snapshot cam1 @ 4K.** Save as
   `<prefix>_cam1_4K_29fps_<timestamp>.jpg`. Strip the OAK-D's bogus
   EXIF block before writing (it would otherwise show "Date Taken: 1970"
   in Windows Explorer).
5. **cam1 → 1080p / 59 fps.** Apply `high_1080p_fast` to cam1.
6. **Record cam1, 5 s @ 1080p / 59 fps.** Save as
   `<prefix>_cam1_1080p_59fps_<timestamp>.mp4`.

Cam2 active phase:

7. **cam1 → low bandwidth.** Drop cam1 back to `low_bandwidth` before
   ramping cam2 up.
8. **cam2 → 4K.** Apply `high_4k` to cam2.
9. **Record cam2, 5 s @ 4K / 29 fps.**
10. **Snapshot cam2 @ 4K.** (EXIF-stripped, same as step 4.)
11. **cam2 → 1080p / 59 fps.**
12. **Record cam2, 5 s @ 1080p / 59 fps.**

Inter-cycle:

13. **Sleep `interval_seconds`.** Then loop back to step 1.

### 3.3 File layout

All files for one camera live under `<save_dir>/<cam_id>/`:

```
<save_dir>/
  cam1/
    <prefix>_cam1_4K_29fps_2026-04-29T10-00-00-123.mp4
    <prefix>_cam1_4K_29fps_2026-04-29T10-00-00-456.jpg
    <prefix>_cam1_1080p_59fps_2026-04-29T10-00-08-789.mp4
    ...
  cam2/
    ...
```

Timestamps are UTC, ISO 8601 with millisecond precision, filename-safe
(`-` instead of `:`).

---

## 4. Config

Single `config.json` next to the executable. All fields optional except
`save_dir`. Example:

```json
{
  "save_dir": "D:\\Kreuzstoesse\\P02",
  "prefix": "kreuzstoss",
  "interval_seconds": 30.0,
  "calibration_warmup_s": 5.0,
  "disk_free_floor_gb": 2.0,
  "max_consecutive_cycle_failures": 5,
  "cameras": {
    "cam1": "<MxID-of-camera-1>",
    "cam2": "<MxID-of-camera-2>"
  }
}
```

If `save_dir` is unwritable, fall back to `./recordings/` next to the
executable and log a warning.

---

## 5. Dependencies

- Python 3.11+
- `depthai` (Luxonis DepthAI SDK) — camera pipeline, MJPEG encoder,
  camera-control API.
- `av` (PyAV) — MP4 muxing of the MJPEG stream.
- `pydantic` v2 — config + internal data classes.
- Standard library: `asyncio`, `logging`, `pathlib`, `shutil`, `json`.

No GUI toolkit, no FastAPI, no React, no MQTT.

---

## 6. Out of scope

- Web UI / REST API / MQTT / live preview / IMU calibration / drive
  position annotation / operator-configurable per-cycle parameters.
- Multi-camera scaling beyond exactly 2 cameras.
- Re-running auto exposure mid-session. Calibration happens once at
  startup and is final for the lifetime of the process.
- Crash recovery beyond "log it and try the next cycle, exit after N
  consecutive failures."

---

## 7. Acceptance criteria

1. `python -m kreuzstoss_headless` (or the packaged `.exe`) starts both
   cameras, completes the calibration warm-up, prints the locked
   exposure / ISO / white-balance values, and begins the recording loop
   without operator interaction.
2. Across a multi-hour run, every produced clip is fully exposed from
   the first frame (no black or under-exposed openings) and every
   produced clip / still has identical color and brightness.
3. The PoE link never saturates — at most one camera is at a
   high-bandwidth preset at any moment.
4. Ctrl-C stops the program cleanly within at most one settle window.
5. The program exits with status 1 (and a clear log line) when free
   disk space drops below the configured floor or after N consecutive
   cycle failures.

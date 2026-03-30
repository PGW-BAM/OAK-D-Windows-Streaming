# OAK-D 4 Pro — Bandwidth Test Results

**Test date:** 2026-03-13
**Cameras tested:** 4x OAK-D 4 Pro (PoE, 169.254.x.x)
**PoE switch:** 2.5 Gbps (usable: ~2,000 Mbps after protocol overhead)
**Encoder:** On-device hardware MJPEG (zero host CPU)

---

## Key Finding

The OAK-D 4 Pro's hardware MJPEG encoder produces **nearly constant frame sizes
regardless of the JPEG quality setting** (Q30–Q100). The quality parameter has
minimal impact on bandwidth — **resolution and FPS are the only meaningful
controls**.

This means: lowering quality does **not** free up bandwidth. To reduce bandwidth
usage, lower the **resolution** or the **FPS**.

---

## Measured Frame Sizes (per frame, single camera)

| Resolution | Pixels       | Avg Frame Size | Mbps @ 30 FPS | Mbps @ 60 FPS |
|------------|-------------|----------------|---------------|---------------|
| **4K**     | 3840 × 2160 | ~3,015 KB      | ~725 Mbps     | ~1,450 Mbps   |
| **1080p**  | 1920 × 1080 | ~641 KB        | ~262 Mbps     | ~524 Mbps     |
| **720p**   | 1280 × 720  | ~288 KB        | ~110 Mbps     | ~220 Mbps     |
| **480p**   | 640 × 480   | ~117 KB        | ~30 Mbps      | ~61 Mbps      |

> Frame sizes are averaged across quality settings Q30–Q100 since the hardware
> encoder produces virtually identical output sizes.

---

## Maximum FPS per Camera Count

Maximum sustainable FPS within the 2,000 Mbps usable bandwidth budget:

### Main Camera Only (RGB)

| Resolution | 1 Camera | 2 Cameras | 3 Cameras | 4 Cameras |
|------------|----------|-----------|-----------|-----------|
| **4K**     | 60       | 41        | 27        | 20        |
| **1080p**  | 60       | 60        | 60        | 57        |
| **720p**   | 60       | 60        | 60        | 60        |
| **480p**   | 60       | 60        | 60        | 60        |

### Quick Reference — "What can I run?"

| Scenario                          | Resolution | FPS  | Total Bandwidth | Status |
|-----------------------------------|-----------|------|-----------------|--------|
| 4 cameras, max quality            | 4K        | 60   | ~5,800 Mbps     | **OVER** — use max 20 FPS |
| 4 cameras, high res, high FPS     | 1080p     | 60   | ~2,096 Mbps     | **TIGHT** — use max 57 FPS |
| 4 cameras, balanced               | 1080p     | 30   | ~1,048 Mbps     | **OK** — 52% utilization |
| 4 cameras, full FPS               | 720p      | 60   | ~880 Mbps       | **OK** — 44% utilization |
| 4 cameras, smooth streaming       | 720p      | 30   | ~440 Mbps       | **OK** — 22% utilization |
| 2 cameras, high res               | 4K        | 30   | ~1,450 Mbps     | **OK** — 73% utilization |
| 1 camera, max everything          | 4K        | 60   | ~1,450 Mbps     | **OK** — 73% utilization |

---

## Concurrent Multi-Camera Verification

Actual measured throughput with cameras streaming simultaneously:

| Cameras | Resolution | Quality | Measured Throughput | Status |
|---------|-----------|---------|-------------------|--------|
| 2       | 720p      | Q85     | 170 Mbps          | OK     |
| 3       | 720p      | Q85     | 232 Mbps          | OK     |
| 4       | 720p      | Q85     | 328 Mbps          | OK     |
| 2       | 1080p     | Q85     | 395 Mbps          | OK     |
| 3       | 1080p     | Q85     | 553 Mbps          | OK     |
| 4       | 1080p     | Q85     | 728 Mbps          | OK     |
| 2       | 1080p     | Q50     | 402 Mbps          | OK     |
| 3       | 1080p     | Q50     | 528 Mbps          | OK     |
| 4       | 1080p     | Q50     | 723 Mbps          | OK     |

> All concurrent tests were at 30 FPS. Throughput scales linearly with camera
> count — no contention observed up to 4 cameras at these settings.

---

## Recommendations

### For 4 cameras simultaneously:

| Priority         | Recommended Setting                | Bandwidth Used |
|------------------|------------------------------------|----------------|
| **Best quality** | 1080p @ 30 FPS                     | ~1,048 Mbps (52%) |
| **Smooth video** | 720p @ 60 FPS                      | ~880 Mbps (44%) |
| **Balanced**     | 1080p @ 25 FPS                     | ~873 Mbps (44%) |
| **Low bandwidth**| 480p @ 30 FPS                      | ~122 Mbps (6%) |
| **Max possible** | 4K @ 20 FPS                        | ~1,933 Mbps (97%) |

### For 2 cameras simultaneously:

| Priority         | Recommended Setting                | Bandwidth Used |
|------------------|------------------------------------|----------------|
| **Best quality** | 4K @ 30 FPS                        | ~1,450 Mbps (73%) |
| **Smooth video** | 1080p @ 60 FPS                     | ~1,048 Mbps (52%) |
| **Balanced**     | 1080p @ 30 FPS                     | ~524 Mbps (26%) |

### For 1 camera:

Any combination up to 4K @ 60 FPS fits comfortably (~1,450 Mbps = 73%).

---

## Stereo Mode Impact

When stereo cameras (left + right mono, 1280×800 each) are active:

- **Main + Stereo ("both"):** Add ~2× 230 KB per frame overhead (~110 Mbps @ 30 FPS)
- **Stereo only:** ~460 KB per frame pair (~110 Mbps @ 30 FPS)

With stereo enabled on 4 cameras at 1080p @ 30 FPS:
- Main streams: ~1,048 Mbps
- Stereo streams: ~440 Mbps
- **Total: ~1,488 Mbps (74%)** — fits within budget

---

## Technical Notes

- The OAK-D 4 Pro hardware MJPEG encoder uses a **fixed-bitrate-like** behavior
  where the quality parameter primarily affects visual quality, not output size.
  This is different from software JPEG encoders where quality directly controls
  compression ratio.
- Frame sizes have ~7% standard deviation due to scene complexity variations.
  The "max" frame sizes were ~4% above average in testing.
- The 80% usable bandwidth factor (2,000 Mbps from 2,500 Mbps link) accounts
  for Ethernet framing, TCP/IP overhead, and PoE signaling.
- At 4K, the camera maxes out at ~29 FPS actual output even when 30 FPS is
  requested — the sensor/ISP pipeline is the bottleneck, not bandwidth.

---

## How to Re-run This Test

```bash
# Full test (all resolutions, all quality levels, up to 4 cameras)
python -m uv run python -m backend.bandwidth_test

# Quick test (fewer quality levels, 2 cameras max)
python -m uv run python -m backend.bandwidth_test --cameras 2 --qualities 50,85

# Longer measurement window for more accurate averages
python -m uv run python -m backend.bandwidth_test --seconds 10

# Print only, don't save profiles
python -m uv run python -m backend.bandwidth_test --no-save
```

Results are saved to `backend/bandwidth_profiles.json` and automatically loaded
by the streaming app to show real measured values in the bandwidth info panel.

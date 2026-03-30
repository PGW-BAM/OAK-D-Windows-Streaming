#!/usr/bin/env python3
"""Bandwidth test — measure actual MJPEG frame sizes across OAK-D cameras.

Iterates through resolution / quality combinations on 1-4 connected cameras
and records average frame sizes.  Results are saved to bandwidth_profiles.json
and displayed as a summary table.

Usage:
    python -m uv run python -m backend.bandwidth_test
    python -m uv run python -m backend.bandwidth_test --seconds 5 --cameras 2
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

try:
    import depthai as dai
except ImportError:
    logger.error("depthai not installed — cannot run hardware bandwidth test")
    sys.exit(1)

from backend.bandwidth import (
    RESOLUTIONS,
    USABLE_BANDWIDTH_MBPS,
    check_feasibility,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROFILES_PATH = Path(__file__).parent / "bandwidth_profiles.json"

QUALITY_LEVELS = [30, 50, 70, 85, 100]
TEST_FPS = 30  # request 30 FPS during measurement; we only care about frame sizes
WARMUP_FRAMES = 10  # discard first N frames
MEASURE_SECONDS = 3  # how long to collect frames per test


# ---------------------------------------------------------------------------
# Single-camera frame-size measurement
# ---------------------------------------------------------------------------

@dataclass
class MeasureResult:
    resolution: str
    quality: int
    avg_bytes: int
    min_bytes: int
    max_bytes: int
    stddev_bytes: int
    frames_collected: int
    actual_fps: float


@dataclass
class FrameCollector:
    """Collects frame sizes from a running pipeline."""
    sizes: list[int] = field(default_factory=list)
    started: float = 0.0
    done: bool = False


def measure_single(
    device_info: dai.DeviceInfo,
    resolution: str,
    quality: int,
    seconds: float = MEASURE_SECONDS,
) -> MeasureResult | None:
    """Connect to one camera, run a pipeline, and measure frame sizes."""
    res = RESOLUTIONS.get(resolution)
    if res is None:
        return None

    device = None
    pipeline = None
    try:
        device = dai.Device(device_info)
        pipeline = dai.Pipeline(device)

        cam = pipeline.create(dai.node.Camera)
        cam.build(dai.CameraBoardSocket.CAM_A)

        encoder = pipeline.create(dai.node.VideoEncoder)
        encoder.setDefaultProfilePreset(
            TEST_FPS, dai.VideoEncoderProperties.Profile.MJPEG
        )
        encoder.setQuality(quality)

        cam_output = cam.requestOutput(
            size=res,
            type=dai.ImgFrame.Type.NV12,
            fps=TEST_FPS,
        )
        cam_output.link(encoder.input)

        q = encoder.bitstream.createOutputQueue()
        q.setMaxSize(4)
        q.setBlocking(False)

        pipeline.start()

        # Warmup
        warmup_count = 0
        t0 = time.monotonic()
        while warmup_count < WARMUP_FRAMES and (time.monotonic() - t0) < 10:
            pkt = q.tryGet()
            if pkt is not None:
                warmup_count += 1
            else:
                time.sleep(0.001)

        # Measure
        sizes: list[int] = []
        start = time.monotonic()
        while (time.monotonic() - start) < seconds:
            pkt = q.tryGet()
            if pkt is not None:
                sizes.append(len(pkt.getData()))
            else:
                time.sleep(0.001)

        pipeline.stop()
        device.close()

        if not sizes:
            return None

        elapsed = time.monotonic() - start
        return MeasureResult(
            resolution=resolution,
            quality=quality,
            avg_bytes=int(statistics.mean(sizes)),
            min_bytes=min(sizes),
            max_bytes=max(sizes),
            stddev_bytes=int(statistics.stdev(sizes)) if len(sizes) > 1 else 0,
            frames_collected=len(sizes),
            actual_fps=round(len(sizes) / elapsed, 1),
        )

    except Exception as exc:
        logger.error("Measurement failed (%s q%d): %s", resolution, quality, exc)
        if pipeline:
            try:
                pipeline.stop()
            except Exception:
                pass
        if device:
            try:
                device.close()
            except Exception:
                pass
        return None


# ---------------------------------------------------------------------------
# Multi-camera concurrent measurement
# ---------------------------------------------------------------------------

def measure_concurrent(
    device_infos: list[dai.DeviceInfo],
    resolution: str,
    quality: int,
    seconds: float = MEASURE_SECONDS,
) -> list[MeasureResult]:
    """Measure frame sizes on multiple cameras simultaneously."""
    results: list[MeasureResult | None] = [None] * len(device_infos)

    def worker(idx: int, info: dai.DeviceInfo) -> None:
        results[idx] = measure_single(info, resolution, quality, seconds)

    threads = []
    for i, info in enumerate(device_infos):
        t = threading.Thread(target=worker, args=(i, info))
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=seconds + 15)

    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# Full test suite
# ---------------------------------------------------------------------------

def run_full_test(
    max_cameras: int = 4,
    seconds: float = MEASURE_SECONDS,
    qualities: list[int] | None = None,
) -> dict:
    """Run the complete bandwidth test across all combinations.

    Returns a dict suitable for saving as bandwidth_profiles.json.
    """
    if qualities is None:
        qualities = QUALITY_LEVELS

    logger.info("Discovering OAK-D devices...")
    all_devices = dai.Device.getAllAvailableDevices()
    if not all_devices:
        logger.error("No OAK-D devices found!")
        return {}

    n_devices = min(len(all_devices), max_cameras)
    logger.info("Found %d device(s), will test with up to %d", len(all_devices), n_devices)

    frame_sizes: dict[str, int] = {}
    detailed_results: list[dict] = []

    resolutions_to_test = list(RESOLUTIONS.keys())
    total_tests = len(resolutions_to_test) * len(qualities)
    test_num = 0

    for res_name in resolutions_to_test:
        for q in qualities:
            test_num += 1
            logger.info(
                "[%d/%d] Testing %s @ quality %d ...",
                test_num, total_tests, res_name, q,
            )

            # Measure with single camera first (most accurate frame-size baseline)
            result = measure_single(all_devices[0], res_name, q, seconds)
            if result is None:
                logger.warning("  SKIPPED (measurement failed)")
                continue

            key = f"{res_name}_{q}"
            frame_sizes[key] = result.avg_bytes

            # Calculate bandwidth implications
            per_cam_bps = result.avg_bytes * TEST_FPS * 8
            per_cam_mbps = per_cam_bps / 1_000_000

            detail = {
                "resolution": res_name,
                "quality": q,
                "avg_frame_bytes": result.avg_bytes,
                "min_frame_bytes": result.min_bytes,
                "max_frame_bytes": result.max_bytes,
                "stddev_bytes": result.stddev_bytes,
                "frames_collected": result.frames_collected,
                "actual_fps": result.actual_fps,
                "per_camera_mbps_at_30fps": round(per_cam_mbps, 2),
            }

            # Calculate max FPS for 1-4 cameras based on measured frame size
            for n_cams in range(1, 5):
                budget_bytes_per_sec = USABLE_BANDWIDTH_MBPS * 1_000_000 / 8
                per_cam_budget = budget_bytes_per_sec / n_cams
                max_fps = min(60, int(per_cam_budget / result.avg_bytes))
                detail[f"max_fps_{n_cams}cam"] = max(0, max_fps)

            detailed_results.append(detail)

            logger.info(
                "  avg=%d B  (%.1f KB)  @30fps → %.1f Mbps/cam  "
                "max_fps: 1cam=%d  2cam=%d  3cam=%d  4cam=%d",
                result.avg_bytes,
                result.avg_bytes / 1024,
                per_cam_mbps,
                detail["max_fps_1cam"],
                detail["max_fps_2cam"],
                detail["max_fps_3cam"],
                detail["max_fps_4cam"],
            )

            # Brief pause between tests to let the device reset
            time.sleep(1)

    # Multi-camera concurrent test (if >1 device available)
    concurrent_results: list[dict] = []
    if n_devices > 1:
        logger.info("\n--- Concurrent multi-camera tests ---")
        # Test a subset with all cameras simultaneously
        test_combos = [
            ("720p", 85),
            ("1080p", 85),
            ("1080p", 50),
        ]
        for res_name, q in test_combos:
            for n_cams in range(2, n_devices + 1):
                logger.info(
                    "Concurrent: %d cameras @ %s q%d ...",
                    n_cams, res_name, q,
                )
                results = measure_concurrent(
                    all_devices[:n_cams], res_name, q, seconds,
                )
                if results:
                    avg_across = int(statistics.mean(r.avg_bytes for r in results))
                    total_mbps = sum(
                        r.avg_bytes * r.actual_fps * 8 / 1_000_000
                        for r in results
                    )
                    concurrent_results.append({
                        "resolution": res_name,
                        "quality": q,
                        "num_cameras": n_cams,
                        "avg_frame_bytes": avg_across,
                        "total_actual_mbps": round(total_mbps, 2),
                        "per_camera_results": [
                            {
                                "avg_bytes": r.avg_bytes,
                                "actual_fps": r.actual_fps,
                            }
                            for r in results
                        ],
                    })
                    logger.info(
                        "  total throughput: %.1f Mbps across %d cameras",
                        total_mbps, n_cams,
                    )
                time.sleep(1)

    output = {
        "test_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "num_devices_found": len(all_devices),
        "num_devices_tested": n_devices,
        "poe_bandwidth_gbps": 2.5,
        "usable_bandwidth_mbps": USABLE_BANDWIDTH_MBPS,
        "frame_sizes": frame_sizes,
        "detailed_results": detailed_results,
        "concurrent_results": concurrent_results,
    }

    return output


# ---------------------------------------------------------------------------
# Pretty-print summary table
# ---------------------------------------------------------------------------

def print_summary(data: dict) -> None:
    """Print a human-readable summary table."""
    results = data.get("detailed_results", [])
    if not results:
        print("No results to display.")
        return

    print()
    print("=" * 90)
    print(f"  OAK-D Bandwidth Test Results — {data.get('test_timestamp', '?')}")
    print(f"  PoE switch: {data.get('poe_bandwidth_gbps', 2.5)} Gbps"
          f"  (usable: {data.get('usable_bandwidth_mbps', 2000)} Mbps)")
    print(f"  Devices tested: {data.get('num_devices_tested', '?')}")
    print("=" * 90)
    print()

    # Table header
    header = (
        f"{'Resolution':<10} {'Quality':>7} {'Avg Frame':>10} "
        f"{'Mbps@30':>8}  "
        f"{'1 cam':>6} {'2 cam':>6} {'3 cam':>6} {'4 cam':>6}"
    )
    print(header)
    print(f"{'':10} {'':>7} {'(KB)':>10} {'(/cam)':>8}  "
          f"{'maxFPS':>6} {'maxFPS':>6} {'maxFPS':>6} {'maxFPS':>6}")
    print("-" * 90)

    for r in results:
        fps_cells = []
        for n in range(1, 5):
            mfps = r.get(f"max_fps_{n}cam", 0)
            if mfps == 0:
                fps_cells.append("  ---")
            elif mfps >= 60:
                fps_cells.append("  60+")
            else:
                fps_cells.append(f"{mfps:>5}")

        print(
            f"{r['resolution']:<10} {r['quality']:>7} "
            f"{r['avg_frame_bytes']/1024:>9.1f}K "
            f"{r['per_camera_mbps_at_30fps']:>8.1f}  "
            f"{'  '.join(fps_cells)}"
        )

    print()

    # Concurrent results
    conc = data.get("concurrent_results", [])
    if conc:
        print("--- Concurrent Multi-Camera Results ---")
        for c in conc:
            status = "OK" if c["total_actual_mbps"] < data.get("usable_bandwidth_mbps", 2000) else "OVER"
            print(
                f"  {c['num_cameras']} cameras @ {c['resolution']} q{c['quality']}: "
                f"{c['total_actual_mbps']:.1f} Mbps total [{status}]"
            )
        print()

    print("Legend: maxFPS = maximum sustainable FPS within PoE bandwidth budget")
    print("        '---' = even 1 FPS exceeds bandwidth budget")
    print("        '60+' = full 60 FPS fits comfortably")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="OAK-D bandwidth test — measure MJPEG frame sizes"
    )
    parser.add_argument(
        "--seconds", type=float, default=MEASURE_SECONDS,
        help=f"Seconds to measure per combination (default: {MEASURE_SECONDS})",
    )
    parser.add_argument(
        "--cameras", type=int, default=4,
        help="Max cameras to test (default: 4)",
    )
    parser.add_argument(
        "--qualities", type=str, default=None,
        help="Comma-separated quality levels (default: 30,50,70,85,100)",
    )
    parser.add_argument(
        "--output", type=str, default=str(PROFILES_PATH),
        help=f"Output JSON path (default: {PROFILES_PATH})",
    )
    parser.add_argument(
        "--no-save", action="store_true",
        help="Don't save results to file (just print)",
    )
    args = parser.parse_args()

    qualities = None
    if args.qualities:
        qualities = [int(q.strip()) for q in args.qualities.split(",")]

    data = run_full_test(
        max_cameras=args.cameras,
        seconds=args.seconds,
        qualities=qualities,
    )

    if not data:
        logger.error("Test produced no results. Are cameras connected?")
        sys.exit(1)

    print_summary(data)

    if not args.no_save:
        out_path = Path(args.output)
        out_path.write_text(json.dumps(data, indent=2))
        logger.info("Results saved to %s", out_path)


if __name__ == "__main__":
    main()

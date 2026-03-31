"""MQTT orchestrator — move -> confirm -> settle -> capture state machine.

Wires into the existing CameraManager to trigger captures at the right
moment after drives have reached their target positions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .client import MqttClient
from .config import mqtt_settings
from .models import (
    CaptureSequence,
    CaptureStep,
    DrivePosition,
    MoveCommand,
    OrchestrationError,
    StopCommand,
)
from .topics import Topics

if TYPE_CHECKING:
    from backend.camera_manager import CameraManager

logger = logging.getLogger(__name__)


class OrchestratorState(str, Enum):
    IDLE = "idle"
    MOVING = "moving"
    SETTLING = "settling"
    CAPTURING = "capturing"
    PAUSED = "paused"
    ERROR = "error"


class SequenceRunner:
    """Runs a CaptureSequence through the move-settle-capture workflow."""

    def __init__(
        self,
        mqtt: MqttClient,
        camera_manager: CameraManager,
    ) -> None:
        self._mqtt = mqtt
        self._camera_manager = camera_manager
        self._cfg = mqtt_settings.orchestration

        # Current state
        self.state = OrchestratorState.IDLE
        self.active_sequence: CaptureSequence | None = None
        self.current_step_idx: int = 0
        self.current_repeat: int = 0
        self.total_captures: int = 0

        # Drive position tracking (updated by monitor subscriptions)
        self._drive_positions: dict[str, DrivePosition] = {}
        self._position_events: dict[str, asyncio.Event] = {}

        # Task handle for the sequence loop
        self._task: asyncio.Task | None = None

    @property
    def progress(self) -> str:
        if not self.active_sequence:
            return ""
        total = len(self.active_sequence.steps) * self.active_sequence.repeat_count
        current = self.current_repeat * len(self.active_sequence.steps) + self.current_step_idx
        return f"{current}/{total}"

    async def handle_drive_position(self, topic: str, data: dict[str, Any]) -> None:
        """Called by the MQTT message handler when a drive position update arrives."""
        try:
            pos = DrivePosition(**data)
        except Exception as exc:
            logger.warning("Invalid DrivePosition payload: %s", exc)
            return

        # Extract cam_id from topic: status/drives/{cam_id}/position
        parts = topic.split("/")
        if len(parts) >= 3:
            cam_id = parts[2]
            key = f"{cam_id}:{pos.drive_axis}"
            self._drive_positions[key] = pos

            # Signal waiters if drive reached target
            if pos.state == "reached" and key in self._position_events:
                self._position_events[key].set()

    async def load_sequence_file(self, path: str | Path) -> CaptureSequence:
        """Load a capture sequence from a YAML file."""
        p = Path(path)
        with open(p) as f:
            data = yaml.safe_load(f)
        return CaptureSequence(**data)

    async def start_sequence(self, sequence: CaptureSequence) -> None:
        """Start executing a capture sequence."""
        if self.state not in (OrchestratorState.IDLE, OrchestratorState.ERROR):
            raise RuntimeError(f"Cannot start sequence in state {self.state}")

        self.active_sequence = sequence
        self.current_step_idx = 0
        self.current_repeat = 0
        self.total_captures = 0
        self.state = OrchestratorState.IDLE

        # Publish active sequence to broker
        await self._mqtt.publish(
            Topics.config_sequence_active(),
            sequence,
            retain=True,
        )

        self._task = asyncio.create_task(
            self._run_sequence(), name="sequence-runner"
        )
        logger.info(
            "Sequence '%s' started (%d steps, %d repeats)",
            sequence.name,
            len(sequence.steps),
            sequence.repeat_count,
        )

    async def stop_sequence(self) -> None:
        """Stop the active sequence."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Send stop to all drives
        for cam_id in (mqtt_settings.cam_ids):
            await self._mqtt.publish(
                Topics.cmd_stop(cam_id),
                StopCommand(),
            )

        self.state = OrchestratorState.IDLE
        self.active_sequence = None

        # Clear retained active sequence
        await self._mqtt.publish(
            Topics.config_sequence_active(), "{}", retain=True
        )
        logger.info("Sequence stopped")

    async def pause_sequence(self) -> None:
        if self.state == OrchestratorState.MOVING:
            self.state = OrchestratorState.PAUSED

    async def resume_sequence(self) -> None:
        if self.state == OrchestratorState.PAUSED:
            self.state = OrchestratorState.IDLE

    # ------------------------------------------------------------------
    # Internal sequence execution
    # ------------------------------------------------------------------

    async def _run_sequence(self) -> None:
        """Main sequence loop: iterate steps x repeats."""
        seq = self.active_sequence
        if not seq:
            return

        try:
            for repeat in range(seq.repeat_count):
                self.current_repeat = repeat
                for idx, step in enumerate(seq.steps):
                    self.current_step_idx = idx

                    # Wait if paused
                    while self.state == OrchestratorState.PAUSED:
                        await asyncio.sleep(0.1)

                    await self._execute_step(step)

            logger.info(
                "Sequence '%s' completed — %d captures",
                seq.name,
                self.total_captures,
            )
        except asyncio.CancelledError:
            logger.info("Sequence cancelled")
            raise
        except Exception as exc:
            self.state = OrchestratorState.ERROR
            logger.error("Sequence error: %s", exc, exc_info=True)
            await self._publish_error(
                "sequence_abort",
                seq.sequence_id,
                step.cam_id if 'step' in dir() else None,
                str(exc),
            )
        finally:
            if self.state != OrchestratorState.ERROR:
                self.state = OrchestratorState.IDLE
            self.active_sequence = None

    async def _execute_step(self, step: CaptureStep) -> None:
        """Execute one step: move both axes -> wait -> settle -> capture."""
        cam_id = step.cam_id
        seq_id = self.active_sequence.sequence_id if self.active_sequence else ""

        # --- Move both axes ---
        self.state = OrchestratorState.MOVING

        axis_a_key = f"{cam_id}:a"
        axis_b_key = f"{cam_id}:b"
        self._position_events[axis_a_key] = asyncio.Event()
        self._position_events[axis_b_key] = asyncio.Event()

        move_a = MoveCommand(
            sequence_id=seq_id,
            drive_axis="a",
            target_position=step.position.drive_a,
        )
        move_b = MoveCommand(
            sequence_id=seq_id,
            drive_axis="b",
            target_position=step.position.drive_b,
        )

        await self._mqtt.publish(Topics.cmd_move(cam_id), move_a)
        await self._mqtt.publish(Topics.cmd_move(cam_id), move_b)

        # --- Wait for both axes to reach target ---
        timeout = self._cfg.move_timeout_s
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    self._position_events[axis_a_key].wait(),
                    self._position_events[axis_b_key].wait(),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Send stop and report error
            await self._mqtt.publish(
                Topics.cmd_stop(cam_id),
                StopCommand(sequence_id=seq_id),
            )
            await self._publish_error(
                "timeout", seq_id, cam_id,
                f"Drive move timeout after {timeout}s",
            )
            raise RuntimeError(f"Drive move timeout for {cam_id}")

        # --- Settling delay ---
        self.state = OrchestratorState.SETTLING
        settling_ms = step.settling_delay_ms or self._cfg.default_settling_ms
        await asyncio.sleep(settling_ms / 1000.0)

        # --- Capture ---
        self.state = OrchestratorState.CAPTURING
        try:
            await asyncio.wait_for(
                self._do_capture(cam_id, seq_id, step),
                timeout=self._cfg.capture_timeout_s,
            )
            self.total_captures += 1
        except asyncio.TimeoutError:
            logger.warning("Capture timeout for %s — retrying once", cam_id)
            try:
                await asyncio.wait_for(
                    self._do_capture(cam_id, seq_id, step),
                    timeout=self._cfg.capture_timeout_s,
                )
                self.total_captures += 1
            except asyncio.TimeoutError:
                logger.error("Capture failed after retry for %s — skipping", cam_id)
                await self._publish_error(
                    "timeout", seq_id, cam_id, "Capture timeout after retry"
                )

        # Clean up events
        self._position_events.pop(axis_a_key, None)
        self._position_events.pop(axis_b_key, None)

    async def _do_capture(
        self, cam_id: str, seq_id: str, step: CaptureStep
    ) -> None:
        """Trigger capture using the existing camera manager."""
        # Find the camera worker by cam_id mapping
        # cam_id is "cam1"/"cam2" — we need to map to the actual device MX ID
        workers = self._camera_manager.all_workers()

        # Map cam1 -> first worker, cam2 -> second worker (by discovery order)
        cam_index = int(cam_id.replace("cam", "")) - 1
        if cam_index >= len(workers):
            raise RuntimeError(f"Camera {cam_id} not found (only {len(workers)} connected)")

        worker = workers[cam_index]

        # Use the existing snapshot capture
        loop = asyncio.get_event_loop()
        jpeg_data = await loop.run_in_executor(None, worker.capture_snapshot)

        # Save with drive position metadata
        pos_a = self._drive_positions.get(f"{cam_id}:a")
        pos_b = self._drive_positions.get(f"{cam_id}:b")
        metadata = {
            "sequence_id": seq_id,
            "cam_id": cam_id,
            "step_index": self.current_step_idx,
            "repeat": self.current_repeat,
            "drive_a_position": pos_a.current_position if pos_a else None,
            "drive_b_position": pos_b.current_position if pos_b else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Save image and metadata to recordings directory
        from backend.config import settings as app_settings

        out_dir = app_settings.recordings_dir / cam_id / "sequences" / seq_id
        out_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        img_path = out_dir / f"{ts}_step{self.current_step_idx:04d}.jpg"
        meta_path = img_path.with_suffix(".json")

        img_path.write_bytes(jpeg_data)
        meta_path.write_text(json.dumps(metadata, indent=2))

        logger.info(
            "Captured %s step %d -> %s",
            cam_id, self.current_step_idx, img_path.name,
        )

    async def _publish_error(
        self, event: str, seq_id: str | None, cam_id: str | None, message: str
    ) -> None:
        err = OrchestrationError(
            event=event,
            sequence_id=seq_id,
            cam_id=cam_id,
            message=message,
        )
        await self._mqtt.publish(Topics.error_orchestration(event), err)

"""Orchestrator move-wait semantics (no hardware, no broker).

Regression tests for: a stale "reached" (previous move / retained replay) must not
trigger capture before the new move started, and a drive "fault" must abort the step.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.mqtt.models import CaptureSequence, CaptureStep, PositionTarget
from backend.mqtt.orchestrator import SequenceRunner
from backend.mqtt.topics import Topics


class FakeMqtt:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, topic: str, payload: Any, qos: int = 1, retain: bool = False) -> None:
        self.published.append((topic, payload))


def _status(seq: str, axis: str, state: str, pos: float = 0.0) -> dict[str, Any]:
    return {
        "sequence_id": seq,
        "drive_axis": axis,
        "current_position": pos,
        "target_position": pos,
        "state": state,
    }


@pytest.fixture()
def runner() -> tuple[SequenceRunner, FakeMqtt, list[str]]:
    mqtt = FakeMqtt()
    r = SequenceRunner(mqtt, camera_manager=None)  # type: ignore[arg-type]
    r._cfg = r._cfg.model_copy(update={"move_timeout_s": 1.0, "default_settling_ms": 0})
    captured: list[str] = []

    async def fake_capture(cam_id: str, seq_id: str, step: CaptureStep) -> None:
        captured.append(cam_id)

    r._do_capture = fake_capture  # type: ignore[method-assign]
    r.active_sequence = CaptureSequence(sequence_id="seq-1", name="t", steps=[])
    return r, mqtt, captured


def _step() -> CaptureStep:
    return CaptureStep(
        cam_id="cam1", position=PositionTarget(drive_a=1.0, drive_b=2.0), settling_delay_ms=0
    )


async def _feed(r: SequenceRunner, msgs: list[dict[str, Any]]) -> None:
    await asyncio.sleep(0.01)  # let _execute_step register its waiters
    for m in msgs:
        await r.handle_drive_position(Topics.status_drive_position("cam1"), m)
        await asyncio.sleep(0)


async def test_stale_reached_alone_does_not_trigger_capture(runner) -> None:
    r, mqtt, captured = runner
    feeder = _feed(r, [_status("seq-1", "a", "reached"), _status("seq-1", "b", "reached")])
    with pytest.raises(RuntimeError, match="timeout"):
        await asyncio.gather(r._execute_step(_step()), feeder)
    assert captured == []


async def test_moving_then_reached_triggers_capture(runner) -> None:
    r, mqtt, captured = runner
    msgs = [
        _status("seq-1", "a", "moving"),
        _status("seq-1", "a", "reached", 1.0),
        _status("seq-1", "b", "moving"),
        _status("seq-1", "b", "reached", 2.0),
    ]
    await asyncio.gather(r._execute_step(_step()), _feed(r, msgs))
    assert captured == ["cam1"]


async def test_old_pi_stale_status_then_reached_still_works(runner) -> None:
    """Older Pi firmware: pre-move status carries the stale state, then 'reached'."""
    r, mqtt, captured = runner
    msgs = [
        _status("seq-1", "a", "reached", 0.0),
        _status("seq-1", "a", "reached", 1.0),
        _status("seq-1", "b", "idle", 0.0),
        _status("seq-1", "b", "reached", 2.0),
    ]
    await asyncio.gather(r._execute_step(_step()), _feed(r, msgs))
    assert captured == ["cam1"]


async def test_other_sequence_status_is_ignored(runner) -> None:
    r, mqtt, captured = runner
    msgs = [_status("other", ax, st) for ax in "ab" for st in ("moving", "reached")]
    with pytest.raises(RuntimeError, match="timeout"):
        await asyncio.gather(r._execute_step(_step()), _feed(r, msgs))
    assert captured == []


async def test_fault_aborts_step_without_capture(runner) -> None:
    r, mqtt, captured = runner
    msgs = [
        _status("seq-1", "a", "moving"),
        _status("seq-1", "a", "reached", 1.0),
        _status("seq-1", "b", "moving"),
        _status("seq-1", "b", "fault", 1.5),
    ]
    with pytest.raises(RuntimeError, match="fault"):
        await asyncio.gather(r._execute_step(_step()), _feed(r, msgs))
    assert captured == []
    assert any(t == Topics.cmd_stop("cam1") for t, _ in mqtt.published)

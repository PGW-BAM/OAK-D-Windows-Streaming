"""Reusable async MQTT client wrapper with auto-reconnect, LWT, and Pydantic serialization."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import aiomqtt
from pydantic import BaseModel

from .config import mqtt_settings
from .topics import Topics

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class MqttClient:
    """Async MQTT client with auto-reconnect and structured publish/subscribe.

    Usage:
        client = MqttClient()
        client.on("status/drives/+/position", handle_position)
        await client.start()
        await client.publish(Topics.cmd_move("cam1"), move_cmd)
        ...
        await client.stop()
    """

    def __init__(self) -> None:
        self._cfg = mqtt_settings.broker
        self._handlers: list[tuple[str, MessageHandler]] = []
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._stopping = False

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def on(self, topic_filter: str, handler: MessageHandler) -> None:
        """Register a handler for a topic pattern (supports + and # wildcards)."""
        self._handlers.append((topic_filter, handler))

    async def start(self) -> None:
        """Start the MQTT client loop with auto-reconnect."""
        self._stopping = False
        self._task = asyncio.create_task(self._run_loop(), name="mqtt-client")

    async def stop(self) -> None:
        """Gracefully shut down the client."""
        self._stopping = True
        self._connected.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def publish(
        self,
        topic: str,
        payload: BaseModel | dict | str,
        qos: int = 1,
        retain: bool = False,
    ) -> None:
        """Publish a message. Pydantic models are auto-serialized to JSON."""
        if isinstance(payload, BaseModel):
            data = payload.model_dump_json()
        elif isinstance(payload, dict):
            data = json.dumps(payload, default=str)
        else:
            data = payload

        if not self.is_connected or self._client is None:
            logger.warning("MQTT not connected — dropping publish to %s", topic)
            return

        try:
            await self._client.publish(topic, data, qos=qos, retain=retain)
            logger.debug("PUB %s (qos=%d, retain=%s)", topic, qos, retain)
        except Exception as exc:
            logger.error("MQTT publish error on %s: %s", topic, exc)

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        """Wait until connected or timeout. Returns True if connected."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Connection loop with exponential backoff reconnect."""
        delay = self._cfg.reconnect_min_s

        while not self._stopping:
            try:
                lwt = aiomqtt.Will(
                    topic=Topics.health_win(),
                    payload=json.dumps({"online": False}),
                    qos=0,
                    retain=False,
                )
                async with aiomqtt.Client(
                    hostname=self._cfg.host,
                    port=self._cfg.port,
                    keepalive=self._cfg.keepalive,
                    will=lwt,
                ) as client:
                    self._client = client
                    self._connected.set()
                    delay = self._cfg.reconnect_min_s
                    logger.info(
                        "MQTT connected to %s:%d", self._cfg.host, self._cfg.port
                    )

                    # Subscribe to all registered topic filters
                    for topic_filter, _ in self._handlers:
                        await client.subscribe(topic_filter, qos=1)
                        logger.debug("SUB %s", topic_filter)

                    # Message dispatch loop
                    async for message in client.messages:
                        topic_str = str(message.topic)
                        try:
                            payload_bytes = message.payload
                            if isinstance(payload_bytes, (bytes, bytearray)):
                                payload_dict = json.loads(payload_bytes.decode())
                            else:
                                payload_dict = json.loads(str(payload_bytes))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            logger.warning("Non-JSON message on %s", topic_str)
                            continue

                        for topic_filter, handler in self._handlers:
                            if _topic_matches(topic_filter, topic_str):
                                try:
                                    await handler(topic_str, payload_dict)
                                except Exception as exc:
                                    logger.error(
                                        "Handler error for %s: %s",
                                        topic_str,
                                        exc,
                                        exc_info=True,
                                    )

            except aiomqtt.MqttError as exc:
                self._connected.clear()
                self._client = None
                if self._stopping:
                    break
                logger.warning(
                    "MQTT connection lost (%s) — reconnecting in %.1fs", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._cfg.reconnect_max_s)

            except asyncio.CancelledError:
                break

            except Exception as exc:
                self._connected.clear()
                self._client = None
                if self._stopping:
                    break
                logger.error("MQTT unexpected error: %s — reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._cfg.reconnect_max_s)

        self._connected.clear()
        self._client = None
        logger.info("MQTT client stopped")


def _topic_matches(filter_pattern: str, topic: str) -> bool:
    """Check if a topic matches an MQTT filter pattern (supports + and #)."""
    filter_parts = filter_pattern.split("/")
    topic_parts = topic.split("/")

    fi = 0
    ti = 0
    while fi < len(filter_parts) and ti < len(topic_parts):
        if filter_parts[fi] == "#":
            return True
        if filter_parts[fi] == "+" or filter_parts[fi] == topic_parts[ti]:
            fi += 1
            ti += 1
        else:
            return False

    return fi == len(filter_parts) and ti == len(topic_parts)

"""Reusable async MQTT client wrapper with auto-reconnect, LWT, and Pydantic serialization.

On Windows, paho-mqtt requires a SelectorEventLoop (ProactorEventLoop doesn't
support add_reader/add_writer).  This module runs the MQTT connection in a
dedicated thread with its own SelectorEventLoop and bridges calls back to the
main asyncio loop used by FastAPI/uvicorn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import threading
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
        self._connected_flag = threading.Event()
        self._stopping = False

        # Dedicated event loop for the MQTT thread (SelectorEventLoop on Windows)
        self._mqtt_loop: asyncio.AbstractEventLoop | None = None
        self._mqtt_thread: threading.Thread | None = None

        # Main loop reference for dispatching handlers
        self._main_loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected_flag.is_set()

    def on(self, topic_filter: str, handler: MessageHandler) -> None:
        """Register a handler for a topic pattern (supports + and # wildcards)."""
        self._handlers.append((topic_filter, handler))

    async def start(self) -> None:
        """Start the MQTT client loop with auto-reconnect."""
        self._stopping = False
        self._main_loop = asyncio.get_running_loop()

        # Create a dedicated SelectorEventLoop in a background thread
        self._mqtt_loop = asyncio.SelectorEventLoop()
        self._mqtt_thread = threading.Thread(
            target=self._thread_entry, name="mqtt-thread", daemon=True
        )
        self._mqtt_thread.start()

    async def stop(self) -> None:
        """Gracefully shut down the client."""
        self._stopping = True
        self._connected_flag.clear()
        if self._mqtt_loop and self._mqtt_loop.is_running():
            self._mqtt_loop.call_soon_threadsafe(self._mqtt_loop.stop)
        if self._mqtt_thread:
            self._mqtt_thread.join(timeout=5)
            self._mqtt_thread = None
        if self._mqtt_loop and not self._mqtt_loop.is_closed():
            self._mqtt_loop.close()
            self._mqtt_loop = None

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

        if not self.is_connected or self._client is None or self._mqtt_loop is None:
            logger.debug("MQTT not connected — dropping publish to %s", topic)
            return

        future = asyncio.run_coroutine_threadsafe(
            self._do_publish(topic, data, qos, retain),
            self._mqtt_loop,
        )
        try:
            future.result(timeout=5.0)
        except Exception as exc:
            logger.error("MQTT publish error on %s: %s", topic, exc)

    async def _do_publish(
        self, topic: str, data: str, qos: int, retain: bool
    ) -> None:
        if self._client:
            await self._client.publish(topic, data, qos=qos, retain=retain)
            logger.debug("PUB %s (qos=%d, retain=%s)", topic, qos, retain)

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        """Wait until connected or timeout. Returns True if connected."""
        return await asyncio.get_event_loop().run_in_executor(
            None, self._connected_flag.wait, timeout
        )

    # ------------------------------------------------------------------
    # MQTT thread
    # ------------------------------------------------------------------

    def _thread_entry(self) -> None:
        """Entry point for the dedicated MQTT thread."""
        asyncio.set_event_loop(self._mqtt_loop)
        self._mqtt_loop.run_until_complete(self._run_loop())

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
                    self._connected_flag.set()
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

                        # Dispatch to handlers on the main event loop
                        for topic_filter, handler in self._handlers:
                            if _topic_matches(topic_filter, topic_str):
                                if self._main_loop and not self._main_loop.is_closed():
                                    asyncio.run_coroutine_threadsafe(
                                        _safe_call(handler, topic_str, payload_dict),
                                        self._main_loop,
                                    )

            except aiomqtt.MqttError as exc:
                self._connected_flag.clear()
                self._client = None
                if self._stopping:
                    break
                logger.warning(
                    "MQTT connection lost (%s) — reconnecting in %.1fs", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._cfg.reconnect_max_s)

            except Exception as exc:
                self._connected_flag.clear()
                self._client = None
                if self._stopping:
                    break
                logger.error(
                    "MQTT unexpected error: %s — reconnecting in %.1fs", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._cfg.reconnect_max_s)

        self._connected_flag.clear()
        self._client = None
        logger.info("MQTT client stopped")


async def _safe_call(
    handler: MessageHandler, topic: str, data: dict[str, Any]
) -> None:
    """Call a handler with exception logging."""
    try:
        await handler(topic, data)
    except Exception as exc:
        logger.error("Handler error for %s: %s", topic, exc, exc_info=True)


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

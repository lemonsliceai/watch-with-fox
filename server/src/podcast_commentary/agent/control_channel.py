"""commentary.control data channel — outbound publish + inbound dispatch.

Owns the wire format for the LiveKit data channel the Chrome extension
listens on for ``commentary_start`` / ``commentary_end`` (highlight the
right avatar) and ``agent_ready`` (enumerate speakers), and dispatches
inbound packets (``skip``, ``settings``) to handlers registered by the
Director.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from livekit import rtc

logger = logging.getLogger("podcast-commentary.control")


class ControlChannel:
    """Bidirectional adapter for the ``commentary.control`` topic."""

    def __init__(self, room: rtc.Room) -> None:
        self._room = room
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {}

    def register(self, msg_type: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """Bind a handler for an inbound message type. Last-write-wins."""
        self._handlers[msg_type] = handler

    def attach(self) -> None:
        """Start dispatching incoming ``data_received`` packets."""
        self._room.on("data_received", self._on_data_received)

    def _on_data_received(self, data_packet: Any) -> None:
        msg = self._parse(data_packet)
        if msg is None:
            return
        handler = self._handlers.get(msg.get("type"))
        if handler is None:
            return
        try:
            handler(msg)
        except Exception:
            logger.warning("control handler for %r failed", msg.get("type"), exc_info=True)

    @staticmethod
    def _parse(data_packet: Any) -> dict | None:
        raw = getattr(data_packet, "data", b"")
        try:
            return json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            return None

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    async def publish_commentary_start(self, speaker: str, *, phase: str = "commentary") -> None:
        await self._publish({"type": "commentary_start", "speaker": speaker, "phase": phase})

    async def publish_commentary_end(self, speaker: str, *, phase: str = "commentary") -> None:
        await self._publish({"type": "commentary_end", "speaker": speaker, "phase": phase})

    async def publish_agent_ready(self, speakers: list[dict[str, str]]) -> None:
        await self._publish({"type": "agent_ready", "speakers": speakers})

    async def _publish(self, payload: dict) -> None:
        try:
            await self._room.local_participant.publish_data(
                json.dumps(payload),
                topic="commentary.control",
                reliable=True,
            )
        except Exception:
            logger.warning("Failed to publish %s", payload.get("type"), exc_info=True)


__all__ = ["ControlChannel"]

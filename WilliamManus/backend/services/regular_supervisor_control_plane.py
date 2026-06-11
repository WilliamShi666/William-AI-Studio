from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from services import redis
from utils.logger import logger

_CONTROL_CHANNEL_PATTERN = "agent_run:*:control"


def _normalize_control_payload(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return str(value or "").strip().upper()


def _extract_agent_run_id_from_control_channel(channel: Any) -> str | None:
    if isinstance(channel, bytes):
        channel = channel.decode("utf-8")
    normalized_channel = str(channel or "").strip()
    if not normalized_channel:
        return None
    segments = normalized_channel.split(":")
    if len(segments) != 3:
        return None
    if segments[0] != "agent_run" or segments[2] != "control":
        return None
    agent_run_id = segments[1].strip()
    return agent_run_id or None


@dataclass(slots=True)
class ControlPlaneSlot:
    agent_run_id: str
    attempt_id: str
    execution_epoch: int
    owner_token: str
    stop_requested: bool = False
    control_plane_error: RuntimeError | None = None
    refresh_callback: Callable[[ControlPlaneSlot], Awaitable[None]] | None = None

    def request_stop(self) -> None:
        self.stop_requested = True

    def set_refresh_callback(
        self,
        callback: Callable[[ControlPlaneSlot], Awaitable[None]] | None,
    ) -> None:
        self.refresh_callback = callback


class RegularSupervisorControlPlane:
    def __init__(
        self,
        *,
        supervisor_id: str,
        pubsub_factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.supervisor_id = str(supervisor_id or "").strip()
        self._pubsub_factory = pubsub_factory or redis.create_pubsub
        self._slots_by_run_id: dict[str, ControlPlaneSlot] = {}
        self._pubsub: Any | None = None

    def register(
        self,
        *,
        agent_run_id: str,
        attempt_id: str,
        execution_epoch: int,
        owner_token: str,
    ) -> ControlPlaneSlot:
        slot = ControlPlaneSlot(
            agent_run_id=str(agent_run_id or "").strip(),
            attempt_id=str(attempt_id or "").strip(),
            execution_epoch=max(0, int(execution_epoch or 0)),
            owner_token=str(owner_token or "").strip(),
        )
        self._slots_by_run_id[slot.agent_run_id] = slot
        return slot

    def unregister(
        self,
        *,
        agent_run_id: str,
    ) -> ControlPlaneSlot | None:
        normalized_agent_run_id = str(agent_run_id or "").strip()
        if not normalized_agent_run_id:
            return None
        return self._slots_by_run_id.pop(normalized_agent_run_id, None)

    async def route_control_message(
        self,
        agent_run_id: str,
        payload: Any,
    ) -> None:
        normalized_payload = _normalize_control_payload(payload)
        if normalized_payload != "STOP":
            return
        slot = self._slots_by_run_id.get(str(agent_run_id or "").strip())
        if slot is None:
            return
        slot.request_stop()

    async def run_refresh_tick(
        self,
        refresh_callback: Callable[[ControlPlaneSlot], Awaitable[None]] | None = None,
    ) -> None:
        active_slots = list(self._slots_by_run_id.values())
        for slot in active_slots:
            resolved_callback = refresh_callback or slot.refresh_callback
            if resolved_callback is None:
                continue
            try:
                await resolved_callback(slot)
            except Exception as refresh_error:
                logger.warning(
                    "Regular supervisor control-plane refresh failed supervisor_id=%s agent_run_id=%s attempt_id=%s: %s",
                    self.supervisor_id,
                    slot.agent_run_id,
                    slot.attempt_id,
                    refresh_error,
                )
                slot.control_plane_error = RuntimeError(
                    "Regular supervisor control-plane refresh failed for "
                    f"{slot.agent_run_id}: {refresh_error}"
                )

    async def run_stop_listener(self) -> None:
        if self._pubsub is None:
            self._pubsub = await self._pubsub_factory()
            await self._pubsub.psubscribe(_CONTROL_CHANNEL_PATTERN)

        try:
            while True:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=0.5,
                )
                if not message:
                    await asyncio.sleep(0.05)
                    continue
                if str(message.get("type") or "").strip().lower() != "pmessage":
                    continue
                agent_run_id = _extract_agent_run_id_from_control_channel(
                    message.get("channel")
                )
                if not agent_run_id:
                    continue
                await self.route_control_message(
                    agent_run_id,
                    message.get("data"),
                )
        except asyncio.CancelledError:
            return

    async def aclose(self) -> None:
        if self._pubsub is None:
            return
        try:
            if hasattr(self._pubsub, "punsubscribe"):
                await self._pubsub.punsubscribe(_CONTROL_CHANNEL_PATTERN)
            elif hasattr(self._pubsub, "unsubscribe"):
                await self._pubsub.unsubscribe()
            if hasattr(self._pubsub, "unsubscribe"):
                await self._pubsub.unsubscribe()
            if hasattr(self._pubsub, "aclose"):
                await self._pubsub.aclose()
        finally:
            self._pubsub = None

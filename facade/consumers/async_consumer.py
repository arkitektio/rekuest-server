import logging
import uuid
from typing import Optional

from channels.generic.websocket import AsyncWebsocketConsumer

from facade import caller_events, codes, messages, models
from facade.consumers.agent_protocol import AgentProtocol
from facade.consumers.agent_queue import RedisAgentQueue

logger = logging.getLogger(__name__)


def _agent_group(agent_id: str) -> str:
    """Channel-layer group holding every live connection for one agent."""
    return f"agent-{agent_id}"


def _caller_group(caller_id: str) -> str:
    """Channel-layer group carrying the task events a caller originated."""
    return f"task_caller_{caller_id}"


class _PayloadEventLike:
    """Adapts a ``TaskEventPayload`` dict off the channel layer to ``caller_events.EventLike``."""

    def __init__(self, payload: dict) -> None:
        self.id = int(payload["id"])
        self.task_id = payload["task"]
        self.kind = payload["kind"]
        self.message = payload.get("message")
        self.progress = payload.get("progress")
        self.returns = payload.get("returns")
        self.level = payload.get("level")


class _ProbeEventLike:
    """Adapts a ``ProbeEventBroadcast`` dict to ``caller_events.EventLike``.

    ``id`` is the per-probe seq (there is no row PK), ``task_id`` the probe id.
    """

    def __init__(self, payload: dict) -> None:
        self.id = int(payload["seq"])
        self.task_id = payload["probe"]
        self.kind = payload["kind"]
        self.message = payload.get("message")
        self.progress = payload.get("progress")
        self.returns = payload.get("returns")
        self.level = payload.get("level")


class AgentConsumer(AsyncWebsocketConsumer):
    """Thin Channels adapter around :class:`AgentProtocol`.

    All conversation logic lives in the (transport-agnostic, unit-tested)
    ``AgentProtocol``; this class only wires the WebSocket transport to it and
    manages the connection lifecycle.
    """

    @classmethod
    def broadcast(cls, agent: "models.Agent | int | str", message: messages.ToAgentMessage, *, priority: bool = False) -> bool:
        """Send a message to a specific agent over its transport (thin facade).

        Kept for the existing backend/signal call sites; delegates to the typed
        :func:`facade.transport.deliver_to_agent`, which picks redis queue (WEBSOCKET) vs
        HMAC-signed POST (WEBHOOK). Called only AFTER the row is persisted, so a failed
        delivery is recoverable from the DB. Pass the ``Agent`` row when it is already
        loaded; an id falls back to the TTL-cached delivery lookup. Returns whether the
        transport accepted the message (see ``deliver_to_agent``).
        """
        from facade import transport  # lazy: transport imports this consumer's queue module

        if not isinstance(agent, models.Agent):
            agent = transport.get_agent_for_delivery(int(agent))
        return transport.deliver_to_agent(agent, message, priority=priority)

    async def connect(self) -> None:
        """Accept the socket and build a protocol bound to this transport."""
        # Make sure the process-wide reaper runs (idempotent). ``rekuest/asgi.py`` starts it as
        # soon as the server's event loop is up; this covers any other way of hosting the consumer.
        from facade.reaper import ensure_reaper_started  # lazy: avoids import at app-load time

        ensure_reaper_started()
        await self.accept()
        # Identifies this connection within its agent group so a force-register
        # can displace the others without closing itself.
        self.connection_id = str(uuid.uuid4())
        self._agent_group: Optional[str] = None
        self._caller_group: Optional[str] = None
        self.protocol = AgentProtocol(
            send=lambda text: self.send(text_data=text),
            close=lambda code: self.close(code=code),
            queue=RedisAgentQueue.from_settings(),
            register_connection=self.register_connection,
            kick_others=self.kick_others,
            register_caller=self.register_caller,
            connection_id=self.connection_id,
        )

    async def register_connection(self, agent_id: str) -> None:
        """Join the agent's connection group once the agent is known."""
        self._agent_group = _agent_group(agent_id)
        await self.channel_layer.group_add(self._agent_group, self.channel_name)

    async def register_caller(self, caller_id: str) -> None:
        """Join the caller event group so events of work this identity originated reach us."""
        self._caller_group = _caller_group(caller_id)
        await self.channel_layer.group_add(self._caller_group, self.channel_name)

    async def channel_TaskEventCreatedEvent(self, event: dict) -> None:
        """Forward a caller-bound task event to this socket as a ``…Event`` mirror.

        Producer side: ``facade/transport.py`` broadcasts a payload-carrying
        ``TaskEventCreatedEvent`` to ``task_caller_{caller_id}`` on every TaskEvent save
        (this WS forward consumes every caller event, root and child; the slim GraphQL
        change feeds consume the separate ``root_tasks_*`` topics). We only forward the
        ``event`` branch — the ``create`` branch is covered authoritatively by
        ``AssignResponse``, so forwarding it too would race the ack. The payload carries
        everything the mirror needs, so no lookup happens here. Best-effort: a brief
        disconnect simply misses events.
        """
        protocol = getattr(self, "protocol", None)
        if protocol is None or protocol.session is None:
            return  # not registered yet — nothing to correlate against

        payload = (event.get("message") or {}).get("event")
        if not payload:
            return  # a `create` (or malformed) payload — not a task event

        message = caller_events.build_execution_event(_PayloadEventLike(payload))
        if message is not None:
            await protocol.send_to_agent_message(message)

    async def channel_probe_event_broadcast(self, event: dict) -> None:
        """Forward an agent-originated probe's event to the requester as a ``…Event`` mirror.

        Producer side: :func:`facade.probes.persist.probe_topics` mirrors agent-origin
        probe events onto ``task_caller_{caller_id}`` — the group this socket joined at
        registration, so there is no membership race with the executor's first report.
        The mirror reuses the caller-event vocabulary with ``task`` = the probe id and
        both ``event``/``seq`` from the per-probe counter (mirrors are unacked, so
        cross-probe event-id collisions are harmless). Best-effort like all mirrors.
        """
        protocol = getattr(self, "protocol", None)
        if protocol is None or protocol.session is None:
            return  # not registered yet — nothing to correlate against

        payload = event.get("message") or {}
        probe_id = payload.get("probe")
        if not probe_id:
            return

        message = caller_events.build_execution_event(_ProbeEventLike(payload))
        if message is not None:
            await protocol.send_to_agent_message(message)

    async def kick_others(self) -> None:
        """Tell every other connection in this agent's group to close."""
        if self._agent_group is None:
            return
        await self.channel_layer.group_send(
            self._agent_group,
            {"type": "agent.displace", "initiator": self.connection_id},
        )

    async def agent_displace(self, event: dict) -> None:
        """Channel-layer handler: close unless we initiated the displacement.

        Stop draining FIRST. ``close()`` only sends the close frame — if our peer is half-open it
        is never acknowledged, ``disconnect()`` never runs, and until our next heartbeat failed
        its lease renewal we would keep popping the new connection's Assigns off the shared
        queue and "delivering" them into a dead socket.
        """
        if event.get("initiator") != self.connection_id:
            protocol = getattr(self, "protocol", None)
            if protocol is not None and protocol.session is not None:
                await protocol.session.stop_executing()
            await self.close(code=codes.AGENT_REPLACED_CODE)

    async def receive(self, text_data: Optional[str] = None, bytes_data: Optional[bytes] = None) -> None:
        """Forward an inbound frame to the protocol."""
        await self.protocol.receive(text_data)

    async def disconnect(self, code: int) -> None:
        """Tear down the protocol's background work on socket close."""
        group = getattr(self, "_agent_group", None)
        if group is not None:
            await self.channel_layer.group_discard(group, self.channel_name)
        caller_group = getattr(self, "_caller_group", None)
        if caller_group is not None:
            await self.channel_layer.group_discard(caller_group, self.channel_name)
        if hasattr(self, "protocol"):
            await self.protocol.shutdown()
        logger.warning(f"Agent disconnected with code {code}")

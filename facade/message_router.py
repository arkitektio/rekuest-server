"""Transport-agnostic routing of FromAgent messages to the persist backend.

Both transports — the WebSocket ``AgentProtocol`` and the HTTP HookAgent intake — feed
their validated FromAgent messages through :func:`route_from_agent_message`. It performs
the side effects (persisting events, originating caller work, shelving) and **returns** the
optional reply message (``EventAck`` / ``AssignResponse`` / ``Shelved`` / ``Unshelved``)
rather than sending it, so each transport delivers the reply its own way (over the socket,
or in the HTTP response).

``HeartbeatEvent`` is intentionally NOT handled here — it is WebSocket-only liveness and
stays in ``AgentProtocol``.
"""

from __future__ import annotations

import logging
from typing import Optional

from channels.db import database_sync_to_async
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied

from facade import messages
from facade.probes.ids import is_probe_id
from facade.probes.persist import probe_event_backend
from facade.ports import PersistBackend

logger = logging.getLogger(__name__)

# Refusals the backend raises on purpose (task already terminal, not permitted, unknown
# target, invalid input). They are answered with a NACK carrying the message, so they log
# as one line; anything else is a bug and keeps its traceback.
_REFUSALS = (ValueError, LookupError, PermissionError, PermissionDenied, ObjectDoesNotExist)


def log_refusal(what: str, e: Exception) -> None:
    if isinstance(e, _REFUSALS):
        logger.info("%s refused: %s", what, e)
    else:
        logger.exception("%s failed", what)


class UnknownAgentMessage(Exception):
    """Raised for a FromAgent message this router does not handle (caller decides the fate)."""


def _ack(message: messages.FromAgentEvent) -> messages.EventAck:
    """The durable-report acknowledgement so the agent can stop retaining a terminal report."""
    return messages.EventAck(
        event=message.id,
        task=getattr(message, "task", None),
        seq=getattr(message, "seq", None),
    )


def reply_for_duplicate(message: messages.FromAgentMessage) -> "tuple[Optional[messages.ToAgentMessage], int] | None":
    """How to answer a request the replay guard has already seen — or ``None`` to route it anyway.

    A duplicate is not routed a second time; that is the point. But the sender still deserves the
    answer the original earned, or it keeps retrying:

    * an **assign request** → ``None``: route it. It is idempotent on ``(caller, reference)``, so
      it returns the existing task with ``created=False``, which is exactly the right answer.
    * a **report** (Started/Paused/terminals) → its ``EventAck``, so the agent stops retaining it.
      The original request already persisted it.
    * a **control request** → 409. Cancel/interrupt/pause/resume are instructions, not facts;
      acking one the server did not apply this time would be a lie. The sender re-signs and retries.
    * everything else (Yield/Log/Progress, the state stream) → 200 and nothing. Dropping the
      duplicate IS the correct handling, and it is what makes a naive HTTP retry safe.
    """
    match message:
        case messages.AssignRequest():
            return None
        case messages.Shelve() | messages.Unshelve():
            # An idempotent upsert, and an unshelve that answers "unknown drawer": route again.
            return None
        case messages.ControlRequest():
            return messages.ProtocolError(error="Replayed control request — re-sign and retry."), 409
        case messages.FromAgentEvent():
            return _ack(message), 200
        case _:
            return None, 200


async def _control(op, agent_id, message, connection_id, session_id) -> messages.ControlResponse:
    """Run a caller lifecycle-control request and return its ack (NACK on error, never raise)."""
    try:
        task = await op(agent_id, message, connection_id=connection_id, session_id=session_id)
    except Exception as e:
        log_refusal("Caller control request", e)
        return messages.ControlResponse(request=message.id, task=message.task, accepted=False, error=str(e))
    return messages.ControlResponse(request=message.id, task=str(task.pk), accepted=True)


async def route_from_agent_message(
    backend: PersistBackend,
    agent_id: int,
    message: messages.FromAgentMessage,
    *,
    connection_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[messages.ToAgentMessage]:
    """Dispatch a FromAgent message and return the optional reply.

    Raises :class:`UnknownAgentMessage` for messages it does not handle (e.g. a second
    Register) so the transport can close the socket / return a 4xx.
    """
    # Probes share the agent wire protocol: agents report on a probe exactly as on
    # a task, distinguished only by the id prefix — those reports go to the redis-backed
    # probe handlers and must never reach the DB backend (integer PK lookups would raise).
    task_ref = getattr(message, "task", None)
    if task_ref is not None and is_probe_id(task_ref):
        return await _route_probe_message(agent_id, message)

    match message:
        case messages.AssignRequest():
            # An agent assigning dependent work. A bad request (including a parentless root
            # assign) NACKs with an error result rather than propagating — it must never tear
            # down the transport.
            if is_probe_id(message.parent or ""):
                return messages.AssignResponse(
                    request=message.id,
                    reference=message.reference,
                    task=None,
                    created=False,
                    error="A probe cannot parent dependent work — assign a task instead.",
                )
            try:
                task, created = await backend.on_caller_assign(
                    agent_id,
                    message,
                    connection_id=connection_id,
                    session_id=session_id,
                )
            except Exception as e:
                log_refusal("AssignRequest", e)
                return messages.AssignResponse(request=message.id, reference=message.reference, task=None, created=False, error=str(e))
            return messages.AssignResponse(request=message.id, reference=message.reference, task=str(task.pk), created=created)

        case messages.ProbeRequest():
            # An agent firing a probe under its own identity. Refusals (allow_probe not
            # declared, inflight cap, unknown target) NACK rather than propagate — they
            # must never tear down the transport.
            from facade.probes.backend import probe_backend  # lazy: probes.backend reaches facade.backend

            try:
                state = await database_sync_to_async(probe_backend.probe_for_agent)(agent_id, message)
            except Exception as e:
                log_refusal("ProbeRequest", e)
                return messages.ProbeResponse(request=message.id, probe=None, error=str(e))
            return messages.ProbeResponse(request=message.id, probe=state["id"])

        # Caller lifecycle-control requests (two-phase; the outcome streams back as …Event mirrors).
        case messages.CancelRequest():
            return await _control(backend.on_caller_cancel, agent_id, message, connection_id, session_id)
        case messages.InterruptRequest():
            return await _control(backend.on_caller_interrupt, agent_id, message, connection_id, session_id)
        case messages.PauseRequest():
            return await _control(backend.on_caller_pause, agent_id, message, connection_id, session_id)
        case messages.ResumeRequest():
            return await _control(backend.on_caller_resume, agent_id, message, connection_id, session_id)

        # Lifecycle confirmation events from the executing agent.
        case messages.Started():
            await backend.on_agent_started(agent_id, message)
            return _ack(message)
        case messages.Cancelled():
            await backend.on_agent_cancelled(agent_id, message)
            return _ack(message)
        case messages.Interrupted():
            await backend.on_agent_interrupted(agent_id, message)
            return _ack(message)
        case messages.Paused():
            await backend.on_agent_paused(agent_id, message)
            return _ack(message)
        case messages.Resumed():
            await backend.on_agent_resumed(agent_id, message)
            return _ack(message)
        case messages.Yield():
            await backend.on_agent_yield(agent_id, message)
            return None
        case messages.Log():
            await backend.on_agent_log(agent_id, message)
            return None
        case messages.Progress():
            await backend.on_agent_progress(agent_id, message)
            return None
        case messages.Completed():
            await backend.on_agent_done(agent_id, message)
            return _ack(message)
        case messages.Failed():
            await backend.on_agent_error(agent_id, message)
            return _ack(message)
        case messages.Critical():
            await backend.on_agent_critical(agent_id, message)
            return _ack(message)
        case messages.StatePatch():
            # ``Patch.task`` is a real FK — a probe id cannot be recorded there. Keep the
            # patch (state truth matters) but drop the provenance link.
            if is_probe_id(message.task_id or ""):
                message = message.model_copy(update={"task_id": None})
            await backend.on_agent_state_patch(agent_id, message)
            return None
        case messages.StateSnapshot():
            await backend.on_agent_state_snapshot(agent_id, message)
            return None
        case messages.SessionInit():
            await backend.on_agent_session_init(agent_id, message)
            return None

        # Distributed locks: acquire records the holding task, unlock clears it (advisory,
        # fire-and-forget — there is no lock-grant message on the wire).
        case messages.Lock():
            await backend.on_agent_lock(agent_id, message)
            return None
        case messages.Unlock():
            await backend.on_agent_unlock(agent_id, message)
            return None

        # Shelving: request/reply, a failure answers with ``error`` and never tears down
        # the transport.
        case messages.Shelve():
            try:
                drawer = await backend.on_agent_shelve(agent_id, message)
            except Exception as e:
                log_refusal("Shelve", e)
                return messages.Shelved(ref=message.ref, error=str(e))
            return messages.Shelved(ref=message.ref, drawer=str(drawer.pk))
        case messages.Unshelve():
            try:
                await backend.on_agent_unshelve(agent_id, message)
            except Exception as e:
                logger.warning("Unshelve failed: %s", e)
                return messages.Unshelved(ref=message.ref, error=str(e))
            return messages.Unshelved(ref=message.ref)
        case _:
            raise UnknownAgentMessage(type(message).__name__)


async def _route_probe_message(agent_id: int, message: messages.FromAgentMessage) -> Optional[messages.ToAgentMessage]:
    """Dispatch a probe-id-carrying FromAgent message to the redis probe handlers.

    Mirrors the task dispatch: lifecycle confirmations and terminals are acked (agents
    retry terminal reports until acked; the probe store dedups the resends), the streaming
    events are fire-and-forget. What a probe cannot do is refused without tearing down the
    transport: agent-side control of a probe NACKs, a Lock held by a probe is ignored
    (probes lose distributed locking by design — there is no row to record the holder).
    """
    match message:
        case messages.Started():
            await probe_event_backend.on_agent_started(agent_id, message)
            return _ack(message)
        case messages.Paused():
            await probe_event_backend.on_agent_paused(agent_id, message)
            return _ack(message)
        case messages.Resumed():
            await probe_event_backend.on_agent_resumed(agent_id, message)
            return _ack(message)
        case messages.Cancelled():
            await probe_event_backend.on_agent_cancelled(agent_id, message)
            return _ack(message)
        case messages.Interrupted():
            await probe_event_backend.on_agent_interrupted(agent_id, message)
            return _ack(message)
        case messages.Completed():
            await probe_event_backend.on_agent_done(agent_id, message)
            return _ack(message)
        case messages.Failed():
            await probe_event_backend.on_agent_error(agent_id, message)
            return _ack(message)
        case messages.Critical():
            await probe_event_backend.on_agent_critical(agent_id, message)
            return _ack(message)
        case messages.Yield():
            await probe_event_backend.on_agent_yield(agent_id, message)
            return None
        case messages.Log():
            await probe_event_backend.on_agent_log(agent_id, message)
            return None
        case messages.Progress():
            await probe_event_backend.on_agent_progress(agent_id, message)
            return None
        case messages.CancelRequest() | messages.InterruptRequest() | messages.PauseRequest() | messages.ResumeRequest():
            return messages.ControlResponse(
                request=message.id,
                task=message.task,
                accepted=False,
                error="Probes are controlled by their caller via GraphQL, not from an agent connection.",
            )
        case messages.Lock():
            logger.warning("Lock %s requested by probe %s — ignored (probes cannot hold locks)", message.key, message.task)
            return None
        case _:
            raise UnknownAgentMessage(type(message).__name__)

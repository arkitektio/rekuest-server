"""Transport-agnostic agent protocol (the Humble Object).

All of the agent conversation logic lives here as a plain object with injected
dependencies — it knows nothing about Django Channels or WebSockets. ``send`` and
``close`` are callables wired by the adapter (``AgentConsumer``); ``backend`` and
``queue`` are ports; ``authenticator`` resolves a registration to an agent.

Because every collaborator is injected, the protocol/lifecycle/heartbeat
behaviour is unit-testable with fakes — no docker, no DB, no monkeypatching.

The conversation has two phases, split across two objects so the static types match
the runtime contract. :class:`AgentProtocol` is built at connect time and owns the
pre-register handshake (parse / validate / gate). On a successful ``Register`` it
constructs a :class:`RegisteredSession`, the post-register half, whose ``agent`` and
``lease_epoch`` are therefore non-Optional for its whole lifetime — no defensive
``self.agent`` guards scattered through dispatch/heartbeat/shutdown.

Every socket connection is an **agent**: it executes work and holds that agent's write-lease.
Users originate work only through the GraphQL ``assign`` mutation, so there are no
caller/observer connection modes, and over this socket an agent may assign *dependent* work
only (see ``AssignRequest``).
"""

import asyncio
import json
import logging
import uuid
from typing import Awaitable, Callable, Optional

from authentikate.expand import (
    aexpand_client_from_token,
    aexpand_organization_from_token,
    aexpand_user_from_token,
)
from authentikate.utils import authenticate_token_or_none
from channels.db import database_sync_to_async
from django.conf import settings
from pydantic import BaseModel, Field

from facade import codes, messages, models, registration
from facade.consumers.agent_queue import AgentQueue
from facade.message_router import UnknownAgentMessage, log_refusal, route_from_agent_message
from facade.persist_backend import persist_backend
from facade.ports import PersistBackend

logger = logging.getLogger(__name__)

# How often a registered connection re-joins its channel-layer groups (must stay well below the
# layer's ``group_expiry``).
GROUP_REFRESH_SECONDS = 3600.0

SendCallable = Callable[[str], Awaitable[None]]
CloseCallable = Callable[[int], Awaitable[None]]
# A guarded send of an already-serialized frame (the outer protocol's lock-held ``_send``),
# and the typed message send built on top of it — both injected into a RegisteredSession so
# every outbound frame still funnels through the single outer send-lock.
SendTextCallable = Callable[[str], Awaitable[None]]
SendMessageCallable = Callable[[messages.ToAgentMessage], Awaitable[None]]
# The authenticator resolves a Register to the durable agent identity (creating it on first contact).
Authenticator = Callable[[messages.Register], Awaitable["models.Agent"]]
# Wired by the adapter so the protocol can join the agent's connection group and
# displace other live connections — both are no-ops by default (unit tests).
RegisterConnectionCallable = Callable[[str], Awaitable[None]]
KickOthersCallable = Callable[[], Awaitable[None]]
# Wired by the adapter so the protocol can join the caller event group
# (``task_caller_{caller_id}``) and receive the events of work it originated.
RegisterCallerCallable = Callable[[str], Awaitable[None]]


async def _noop_register_connection(agent_id: str) -> None:
    return None


async def _noop_kick_others() -> None:
    return None


async def _noop_register_caller(caller_id: str) -> None:
    return None


class FromAgentPayload(BaseModel):
    """Pydantic model representing the payload sent by the agent."""

    message: messages.FromAgentMessage = Field(discriminator="type")


async def default_authenticator(register: messages.Register) -> "models.Agent":
    """Resolve a ``Register`` to its ``Agent`` via the token, creating it if need be.

    Ensure semantics, the socket twin of the ``ensureAgent`` mutation: the agent row (app and
    release from the token's client; named after the client until its first ``Implement``
    names it), its memory shelve, and the drawers of any previous process forgotten -- a
    process that just registered holds nothing in memory. Runs off the event loop: the
    client's release/app is a lazy FK chain.
    """
    token = await authenticate_token_or_none(register.token)
    if not token:
        raise ValueError("Invalid token")

    user = await aexpand_user_from_token(token)
    client = await aexpand_client_from_token(token)
    organization = await aexpand_organization_from_token(token)

    def ensure() -> "models.Agent":
        agent = registration.ensure_agent(client, user, organization)
        registration.clear_drawers(agent)
        return agent

    return await database_sync_to_async(ensure)()


class RegisteredSession:
    """The post-registration half of the agent conversation.

    Built by :class:`AgentProtocol.on_register` only once a ``Register`` has authenticated
    and won the lease, so ``agent``/``lease_epoch`` are non-Optional for the object's whole
    lifetime. It owns dispatch of post-register frames, the executor task-queue drain, the
    heartbeat loop, and the disconnect cascade.

    Transport access is via the three callables injected by the outer protocol
    (``send_to_agent_message``/``_send``/``close``) rather than a back-reference, so the
    outbound send-lock stays singular on the outer object and there is no reference cycle.
    """

    def __init__(
        self,
        *,
        agent: "models.Agent",
        session_id: Optional[str],
        caller_id: str,
        connection_id: str,
        lease_epoch: int,
        backend: PersistBackend,
        queue: AgentQueue,
        send_to_agent_message: SendMessageCallable,
        send: SendTextCallable,
        close: CloseCallable,
        heartbeat_interval: float,
        heartbeat_timeout: float,
        refresh_groups: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.agent = agent
        self.session_id = session_id
        self.caller_id = caller_id
        self.connection_id = connection_id
        # The fencing token this connection claimed. Every connection is an agent, so every
        # session holds one and renews it on every heartbeat.
        self.lease_epoch = lease_epoch
        self.backend = backend
        self.queue = queue
        self.send_to_agent_message = send_to_agent_message
        self._send = send
        self.close = close
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.refresh_groups = refresh_groups
        # The queue key for this agent, derived ONCE. ``agent.pk`` is an int, and the queue is
        # addressed by string: passing the raw pk to some queue methods and ``str(pk)`` to others
        # works only as long as the backing store stringifies for you. Redis does; an in-memory
        # queue keyed by the value it was handed does not, and would silently park a frame under
        # ``1`` that ``recover`` then looks for under ``"1"``.
        self.agent_key = str(agent.pk)

        self.heartbeat_future: Optional[asyncio.Future] = None
        self.listen_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None

    async def dispatch(self, message: messages.FromAgentMessage) -> None:
        """Route a validated, post-registration message to its handler.

        ``HeartbeatEvent`` is WS-only liveness and handled here; everything else goes through
        the shared :func:`route_from_agent_message` (the same router the HTTP HookAgent intake
        uses) and its returned reply is sent over the socket. A second Register / Lock / etc.
        raises ``UnknownAgentMessage`` and closes the connection.
        """
        if isinstance(message, messages.HeartbeatEvent):
            await self.on_agent_heartbeat()
            return

        try:
            reply = await route_from_agent_message(
                self.backend,
                self.agent.pk,
                message,
                connection_id=self.connection_id,
                session_id=self.session_id,
            )
        except UnknownAgentMessage:
            logger.error("Unknown message in agent")
            await self.close(codes.FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE)
            return

        if reply is not None:
            await self.send_to_agent_message(reply)

    async def on_agent_heartbeat(self) -> None:
        """Renew the agent's write-lease and resolve the pending heartbeat future.

        Note what this deliberately does NOT do: it does not write ``connected``. That flag is a
        transition, owned by the claim/release/revoke paths; re-asserting it on every beat is
        what let a revoked connection resurrect itself after the sweep had already failed its
        in-flight work.
        """
        # Resolve the handshake BEFORE persisting. The renewal is a DB round-trip; if it ran
        # first a slow write could push resolution past the heartbeat timeout and the loop
        # would close a connection that actually answered.
        if self.heartbeat_future and not self.heartbeat_future.done():
            self.heartbeat_future.set_result(None)
            self.heartbeat_future = None
        else:
            logger.error("Received heartbeat without future, possible race condition.")

        if not await self.backend.renew_agent_lease(self.agent.pk, self.lease_epoch):
            # Our epoch no longer exists: a newer connection displaced us, or the stale sweep
            # revoked us. Either way this connection has lost the right to execute work, so it
            # must terminate rather than keep draining the queue against a lease it lost.
            logger.warning("Agent %s lost its lease (epoch %s) — closing displaced connection.", self.agent.pk, self.lease_epoch)
            await self.stop_executing()
            await self.close(codes.AGENT_REPLACED_CODE)

    async def stop_executing(self) -> None:
        """Stop draining the executor queue *now*, before closing the socket.

        ``close()`` only sends the close frame: Channels does not invoke ``disconnect()`` for a
        server-initiated close, so ``shutdown()`` (which cancels the loops) may not run until the
        peer acknowledges — or at all, if it never does. Until then ``listen_for_tasks`` keeps
        popping Assigns off the redis queue and acking them, which for a connection that has just
        been fenced or has stopped answering heartbeats is precisely the work it must no longer
        do. Cancelling here is idempotent: ``shutdown`` cancels an already-cancelled task safely.
        """
        task = self.listen_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("Error cancelling the executor listen loop", exc_info=True)

    async def heartbeat(self) -> None:
        """Periodically ping the agent and close if it stops answering."""
        last_group_refresh = asyncio.get_running_loop().time()
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                # Channel-layer group membership EXPIRES (``group_expiry``, a day by default),
                # and agent sockets are meant to live far longer than that. An expired membership
                # fails silently: no displacement hint, no mirrored caller events. Re-joining is
                # idempotent, so do it well inside the expiry window.
                now = asyncio.get_running_loop().time()
                if self.refresh_groups is not None and now - last_group_refresh >= GROUP_REFRESH_SECONDS:
                    last_group_refresh = now
                    try:
                        await self.refresh_groups()
                    except Exception:
                        logger.error("Agent %s: refreshing channel-layer groups failed", self.agent.pk, exc_info=True)
                # Arm the future before sending so an answer always has a target.
                self.heartbeat_future = asyncio.Future()
                await self.send_to_agent_message(messages.Heartbeat())
                try:
                    await asyncio.wait_for(self.heartbeat_future, self.heartbeat_timeout)
                except asyncio.TimeoutError:
                    logger.error(f"Timeout on client {self.agent.pk} for heartbeat")
                    # Same reasoning as the fenced-lease path: an agent that stopped answering
                    # must stop being handed work immediately, not whenever the close is
                    # acknowledged. (Pre-existing: this path closed without stopping the drain.)
                    await self.stop_executing()
                    await self.close(codes.HEARTBEAT_NOT_RESPONDED_CODE)
                    return
        except asyncio.CancelledError:
            return

    async def _is_stale_assign(self, frame: str) -> bool:
        """The delivery-time fence: is this an Assign for a task the server already closed?

        A frame can outlive its task in the queue of an offline agent — the pickup watchdog,
        the expiry sweep or a cancel may have finalized it meanwhile. Delivering it would run
        work whose every report is then dropped. Probe Assigns carry no ``Task`` row and are
        never fenced; anything unparseable is delivered as-is (the agent validates frames).
        """
        if '"ASSIGN"' not in frame:
            return False  # cheap pre-filter: nearly every other frame skips the JSON parse
        try:
            raw = json.loads(frame)
        except ValueError:
            return False
        if not isinstance(raw, dict) or raw.get("type") != messages.ToAgentMessageType.ASSIGN.value or raw.get("probe"):
            return False
        task_id = raw.get("task")
        if task_id is None:
            return False
        return not await self.backend.is_task_open(str(task_id))

    async def listen_for_tasks(self) -> None:
        """Relay queued messages (e.g. from ``broadcast``) to the agent — and never die quietly.

        This loop is the ONLY way work reaches the agent, while liveness is decided by the
        heartbeat loop next to it. If it ever stopped on its own, the agent would keep looking
        perfectly alive, keep being selected for new work, and never receive any of it. So:

        * a queue (redis) failure is survived — drop the connection, back off, and start over
          by recovering whatever was popped-but-not-acked;
        * a failure to write to the socket means the connection is unusable: the frame stays
          in the in-flight area (the next lease holder recovers it) and the socket is closed,
          so the agent reconnects instead of sitting behind a dead pipe.
        """
        backoff = 0.5
        recovered = False
        try:
            while True:
                try:
                    if not recovered:
                        # We hold the lease and the previous holder was told to stop draining,
                        # so anything still in-flight was popped but (possibly) never delivered.
                        count = await self.queue.recover(self.agent_key)
                        if count:
                            logger.warning("Recovered %s undelivered message(s) for agent %s", count, self.agent.pk)
                        recovered = True
                    task = await self.queue.pop(self.agent_key)
                    backoff = 0.5
                    if not task:
                        # Idle, so nothing of OURS is in flight: whatever sits in the in-flight
                        # area was stranded by someone else — a displaced holder that died between
                        # its pop and its requeue, or a cancel that landed mid-``BLMOVE``.
                        await self.queue.recover(self.agent_key)
                        continue
                    if not await self.backend.holds_lease(self.agent.pk, self.lease_epoch):
                        # Fenced: a newer connection (possibly on another backend) owns this agent
                        # and this frame is meant for ITS socket. Hand it back untouched and stop —
                        # without waiting for the displacement hint (which the channel layer may
                        # drop) or for our next heartbeat to notice.
                        logger.warning("Agent %s: lost the lease (epoch %s) — returning an undelivered frame and closing", self.agent.pk, self.lease_epoch)
                        await self.queue.requeue(self.agent_key, task)
                        await self.close(codes.AGENT_REPLACED_CODE)
                        return
                    if await self._is_stale_assign(task):
                        logger.info("Dropping a queued Assign for an already finalized task (agent %s)", self.agent.pk)
                        await self.queue.ack(self.agent_key, task)
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error("Agent %s: task queue failed; retrying in %.1fs", self.agent.pk, backoff, exc_info=True)
                    try:
                        await self.queue.close()
                    except Exception:
                        logger.debug("Closing the agent queue failed", exc_info=True)
                    recovered = False
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                    continue

                try:
                    # Deliver first, then ack — so a crash mid-delivery leaves the
                    # message recoverable (at-least-once).
                    await self._send(task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error("Agent %s: could not write to the socket; closing so it reconnects", self.agent.pk, exc_info=True)
                    await self.close(codes.AGENT_TRANSPORT_FAILED_CODE)
                    return

                try:
                    await self.queue.ack(self.agent_key, task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Delivered but not acked: it is recovered and delivered once more, which
                    # at-least-once allows (the agent dedupes an Assign by task id).
                    logger.error("Agent %s: could not ack a delivered message", self.agent.pk, exc_info=True)
                    recovered = False
        except asyncio.CancelledError:
            return

    def _on_listen_task_done(self, task: "asyncio.Task[None]") -> None:
        """Last resort: the drain loop must never end while the socket lives on."""
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return  # closed the socket itself after a failed send
        logger.error("Agent %s: the task drain loop died unexpectedly; closing the connection", self.agent.pk, exc_info=error)
        asyncio.ensure_future(self.close(codes.AGENT_TRANSPORT_FAILED_CODE))

    async def shutdown(self) -> None:
        """Cancel loops and drive the disconnect cascade for this session."""
        for task in (self.listen_task, self.heartbeat_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.error("Error cancelling agent task", exc_info=True)

        # Agent teardown drives the in-flight failure/grace cascade over its executed work.
        # Dependent work this agent *assigned* needs no separate cascade: a child's fate
        # follows its parent, and the root belongs to a human on the GraphQL side.
        await self.backend.on_agent_disconnected(self.agent.pk, self.connection_id)


class AgentProtocol:
    """Drives the pre-register handshake over injected transport + ports.

    Built at connect time, before the agent is known. It parses/validates inbound
    frames, gates the ``Register``, and on success constructs the
    :class:`RegisteredSession` that owns the rest of the conversation. The single
    Optional is :attr:`session`, checked once at dispatch.
    """

    def __init__(
        self,
        *,
        send: SendCallable,
        close: CloseCallable,
        queue: AgentQueue,
        backend: PersistBackend = persist_backend,
        authenticator: Authenticator = default_authenticator,
        register_connection: RegisterConnectionCallable = _noop_register_connection,
        kick_others: KickOthersCallable = _noop_kick_others,
        register_caller: RegisterCallerCallable = _noop_register_caller,
        connection_id: Optional[str] = None,
        heartbeat_interval: Optional[float] = None,
        heartbeat_timeout: Optional[float] = None,
    ) -> None:
        self.send = send
        self.close = close
        self.queue = queue
        self.backend = backend
        self.authenticator = authenticator
        self.register_connection = register_connection
        self.kick_others = kick_others
        self.register_caller = register_caller
        # Identifies this connection so the backend can tell, on disconnect,
        # whether we are still the agent's active connection or were displaced.
        self.connection_id = connection_id or str(uuid.uuid4())
        self.heartbeat_interval = heartbeat_interval if heartbeat_interval is not None else settings.AGENT_HEARTBEAT_INTERVAL
        self.heartbeat_timeout = heartbeat_timeout if heartbeat_timeout is not None else settings.AGENT_HEARTBEAT_RESPONSE_TIMEOUT

        # Built only on a successful Register; until then there is no agent identity.
        self.session: Optional[RegisteredSession] = None
        self.received_initial_payload = False
        # All outbound frames funnel through this lock. ``receive`` is serialized
        # by Channels, but the heartbeat and listen loops send concurrently with
        # it (and each other) on the same loop — without serialization their
        # frames could interleave on the wire. ``close`` deliberately stays
        # outside the lock and is never called while it is held (no deadlock).
        self._send_lock = asyncio.Lock()

    async def _send(self, text: str) -> None:
        """Single guarded path to the transport — the only place ``send`` is called."""
        async with self._send_lock:
            await self.send(text)

    async def send_to_agent_message(self, message: messages.ToAgentMessage) -> None:
        """Serialize and hand a message to the transport."""
        await self._send(message.model_dump_json())

    async def receive(self, text_data: Optional[str]) -> None:
        """Parse, validate and dispatch a single inbound frame."""
        if text_data is None:
            # A non-text (e.g. binary) frame — there is no JSON to parse.
            await self.close(codes.FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE)
            return
        try:
            raw = json.loads(text_data)
        except json.JSONDecodeError as e:
            logger.warning("Agent sent a frame that is not JSON: %s", e)
            await self.close(codes.FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE)
            return

        try:
            payload = FromAgentPayload(message=raw)
        except Exception as e:
            logger.warning("Agent message does not match the schema: %s", e)
            logger.debug("Offending agent message: %.500s", raw)
            await self.send_to_agent_message(messages.ProtocolError(error=str(e)))
            await self.close(codes.FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE)
            return

        try:
            if not self.received_initial_payload:
                if not isinstance(payload.message, messages.Register):
                    raise ValueError("First message must be a register")
                self.received_initial_payload = True
                await self.on_register(payload.message)
            elif self.session is not None:
                await self.session.dispatch(payload.message)
            else:
                # received_initial_payload is set but no session exists — registration
                # was attempted and rejected (gate closed the socket). Nothing to dispatch.
                await self.close(codes.FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE)
        except Exception:
            logger.error("Unknown error handling agent message", exc_info=True)
            await self.close(codes.FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE)

    async def on_register(self, register: messages.Register) -> None:
        """Authenticate, reconcile the declaration, claim the lease, send ``Init``, spawn the loops.

        Gate order (each gate may close and return, leaving ``self.session`` None):
        authenticate → blocked → declaration → lease claim/displacement → build session +
        Init + loops. Registering IS implementing: the declaration the ``Register`` carries is
        reconciled in one transaction before the lease is claimed (a refused one strands no
        lease), skipped when its hash is the one already held, and its non-fatal findings
        ride back on ``Init``. A hard finding (catalog mismatch, ownership conflict) refuses
        the connection: ``ProtocolError`` + ``AGENT_REGISTRATION_REJECTED``.
        """
        agent = await self.authenticator(register)
        session_id = register.session_id

        if agent.blocked:
            await self.close(codes.AGENT_IS_BLOCKED_CODE)
            return

        diagnostics: list = []
        if register.declares and not (register.hash is not None and register.hash == agent.hash):
            try:
                agent, diagnostics = await self.backend.on_agent_implement(agent.pk, register)
            except Exception as e:
                log_refusal("Registration", e)
                await self.send_to_agent_message(messages.ProtocolError(error=f"Registration refused: {e}"))
                await self.close(codes.AGENT_REGISTRATION_REJECTED_CODE)
                return

        # Join the caller event group: an agent assigns *dependent* work and must receive its
        # results back over this socket. Done before Init so no event is missed.
        caller_id = await self.backend.get_or_create_caller_id(agent.pk)
        await self.register_caller(caller_id)

        # Join the agent's connection group first (so we can later be kicked). Done before the
        # claim even though we may still lose it: a rejected registration closes the socket,
        # and ``disconnect`` discards the group membership again.
        await self.register_connection(agent.pk)

        # An agent is a singleton: only one LIVE connection per agent. Gate and claim happen
        # together, inside one locked transaction in the backend — reading liveness here and
        # writing there would let two concurrent registrations both observe the same stale
        # incumbent and both be handed the in-flight work.
        #
        # Rejected only when the incumbent is provably live (``connected`` AND a fresh
        # heartbeat) and ``force`` is not set. A STALE incumbent — ``connected`` stuck True but
        # its lease expired (crashed/killed worker, in-memory timers lost on restart) — is
        # auto-displaced WITHOUT ``force``, so a genuinely-dead connection never wedges the
        # agent behind a ``--force`` reconnect.
        claim = await self.backend.on_agent_connected(agent.pk, self.connection_id, session_id=session_id, force=register.force)
        if not claim.claimed:
            await self.send_to_agent_message(messages.ProtocolError(error="Another connection is already registered for this agent. Reconnect with force to take over."))
            await self.close(codes.AGENT_ALREADY_CONNECTED_CODE)
            return

        # Displace any prior connection whenever there was one — a forced takeover of a live
        # incumbent OR an auto-takeover of a stale one whose socket may still be half-open on
        # another worker. ``kick_others`` is keyed on our ``connection_id`` (never closes us)
        # and is a harmless no-op when the group is empty (the dead-worker case). Since the
        # claim bumped ``lease_epoch``, this is now an *optimization*: an unreachable prior
        # connection fences itself on its next heartbeat regardless.
        if claim.displaced_incumbent:
            await self.kick_others()

        # Registration succeeded: everything from here is the post-register half.
        self.session = RegisteredSession(
            agent=agent,
            session_id=session_id,
            caller_id=caller_id,
            connection_id=self.connection_id,
            lease_epoch=claim.epoch,
            backend=self.backend,
            queue=self.queue,
            send_to_agent_message=self.send_to_agent_message,
            send=self._send,
            close=self.close,
            heartbeat_interval=self.heartbeat_interval,
            heartbeat_timeout=self.heartbeat_timeout,
            refresh_groups=lambda: self._refresh_groups(str(agent.pk), caller_id),
        )

        await self.send_to_agent_message(
            messages.Init(
                agent=str(agent.pk),
                hash=agent.hash or None,
                diagnostics=diagnostics,
                inquiries=[messages.AssignInquiry(task=str(a.pk)) for a in claim.tasks],
            )
        )

        self.session.listen_task = asyncio.create_task(self.session.listen_for_tasks())
        self.session.listen_task.add_done_callback(self.session._on_listen_task_done)
        self.session.heartbeat_task = asyncio.create_task(self.session.heartbeat())

    async def _refresh_groups(self, agent_pk: str, caller_id: str) -> None:
        """Re-join this connection's groups (``group_add`` is idempotent and resets the expiry)."""
        await self.register_caller(caller_id)
        await self.register_connection(agent_pk)

    async def shutdown(self) -> None:
        """Tear down the registered session (if any) and release the queue."""
        if self.session is not None:
            await self.session.shutdown()
        await self.queue.close()

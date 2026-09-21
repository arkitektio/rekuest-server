"""AgentProtocol unit tests.

These drive the transport-agnostic ``AgentProtocol`` directly with in-memory fakes
(no Channels, no DB, no redis, no docker, no monkeypatch). They cover the
protocol/lifecycle/routing/heartbeat decisions that would otherwise need the full
stack. Run them with ``pytest tests/agent/test_protocol_unit.py`` with the stack
down to confirm they have no external dependency.

The fakes (``FakeAgent`` / ``FakeBackend`` / ``make_protocol``) and the
``_wait_for`` / ``_register_frame`` helpers are unit-only and intentionally local
to this module.
"""

import asyncio
import datetime
import json
import uuid
from types import SimpleNamespace

import pytest
from django.utils import timezone

from facade import liveness, messages
from facade.ports import LeaseClaim
from facade.codes import (
    AGENT_ALREADY_CONNECTED_CODE,
    AGENT_IS_BLOCKED_CODE,
    AGENT_REGISTRATION_REJECTED_CODE,
    AGENT_REPLACED_CODE,
    FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE,
    FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE,
    HEARTBEAT_NOT_RESPONDED_CODE,
)
from facade.consumers.agent_protocol import AgentProtocol, RegisteredSession
from facade.consumers.agent_queue import InMemoryAgentQueue
from rekuest_core.objects.models import DiagnosticModel

from tests.factories import TEST_TOKEN


_UNSET = object()


class FakeAgent:
    """Stand-in for the ``Agent`` model the authenticator would return.

    ``last_seen`` defaults to *now* when ``connected`` (a genuinely-LIVE incumbent) and to
    ``None`` otherwise, matching the real model — so the reconnect gate's staleness check sees
    a fresh heartbeat. Pass ``last_seen`` explicitly to model a STALE incumbent.
    """

    def __init__(self, pk="agent-1", instance_id="unit-agent", blocked=False, connected=False, last_seen=_UNSET):
        self.pk = pk
        self.instance_id = instance_id
        self.blocked = blocked
        self.connected = connected
        self.last_seen = (timezone.now() if connected else None) if last_seen is _UNSET else last_seen
        self.active_connection_id = None
        self.hash = ""  # never implemented, like a freshly created row
        self.saves = 0

    async def asave(self, **kwargs):
        self.saves += 1


class FakeBackend:
    """Records which persist-backend hook the protocol routed each message to.

    Also models the *lease* half of the port contract, because the executor-singleton gate
    lives in the backend (it has to: gating and claiming must happen under one lock). ``agent``
    is wired by ``make_protocol`` so a ``FakeAgent(connected=...)`` still drives the gate, and
    ``epoch`` is the fencing token — bumped on every claim, so an older epoch fails renewal
    exactly as a displaced connection's would.
    """

    def __init__(self, tasks=None, caller_assign_error=None, agent=None):
        self.tasks = tasks or []
        self.calls = []
        # When set, on_caller_assign raises it (to exercise the nack path).
        self.caller_assign_error = caller_assign_error
        self.agent = agent
        self.epoch = 0

    async def on_agent_done(self, agent_id, message):
        self.calls.append(("done", agent_id, message))

    async def on_caller_assign(self, agent_id, message, connection_id=None, session_id=None):
        self.calls.append(("caller_assign", agent_id, message))
        if self.caller_assign_error is not None:
            raise self.caller_assign_error
        return SimpleNamespace(pk="new-ass-1"), True  # stands in for the created Task

    async def on_caller_cancel(self, agent_id, message, *, connection_id=None, session_id=None):
        self.calls.append(("caller_cancel", agent_id, message))
        if self.caller_assign_error is not None:
            raise self.caller_assign_error
        return SimpleNamespace(pk="ctrl-ass-1")

    async def on_agent_connected(self, agent_id, connection_id=None, session_id=None, force=False):
        self.calls.append(("connected", agent_id))
        connected = getattr(self.agent, "connected", False)
        last_seen = getattr(self.agent, "last_seen", None)
        if liveness.agent_is_live(connected, last_seen) and not force:
            return LeaseClaim(claimed=False)
        self.epoch += 1
        if self.agent is not None:
            self.agent.connected = True
            self.agent.last_seen = timezone.now()
        return LeaseClaim(claimed=True, epoch=self.epoch, tasks=self.tasks, displaced_incumbent=bool(connected))

    async def renew_agent_lease(self, agent_id, lease_epoch):
        self.calls.append(("renew", agent_id, lease_epoch))
        return lease_epoch == self.epoch

    async def get_or_create_caller_id(self, agent_id):
        self.calls.append(("caller_id", agent_id))
        return f"caller-{agent_id}"

    async def holds_lease(self, agent_id, lease_epoch):
        # Fencing at delivery time — the same truth ``renew_agent_lease`` reports.
        return lease_epoch == self.epoch

    async def is_task_open(self, task_id):
        # The delivery-time fence: ``closed_tasks`` stands in for tasks the server finalized.
        return task_id not in getattr(self, "closed_tasks", set())

    async def on_agent_disconnected(self, agent_id, connection_id=None):
        self.calls.append(("disconnected", agent_id))

    async def on_agent_log(self, agent_id, message):
        self.calls.append(("log", agent_id, message))

    # Registration and shelving (request/reply). ``registration_error`` makes all three raise.
    registration_error = None

    async def on_agent_implement(self, agent_id, register):
        self.calls.append(("implement", agent_id, register))
        if self.registration_error:
            raise self.registration_error
        return SimpleNamespace(pk=agent_id, hash=register.hash, blocked=False), [DiagnosticModel(code="unknown_operation", message="fake finding")]

    async def on_agent_shelve(self, agent_id, message):
        self.calls.append(("shelve", agent_id, message))
        if self.registration_error:
            raise self.registration_error
        return SimpleNamespace(pk="drawer-1")

    async def on_agent_unshelve(self, agent_id, message):
        self.calls.append(("unshelve", agent_id, message))
        if self.registration_error:
            raise self.registration_error


def make_protocol(agent=None, backend=None, queue=None, heartbeat_interval=10.0, heartbeat_timeout=5.0, kick_others=None, register_connection=None):
    """Build an ``AgentProtocol`` wired to list-collecting transport callables."""
    sent = []
    closed = []

    async def send(text):
        sent.append(text)

    async def close(code):
        closed.append(code)

    agent = agent if agent is not None else FakeAgent()
    backend = backend if backend is not None else FakeBackend()
    # The executor-singleton gate lives in the backend now, so the fake needs the row it gates.
    if getattr(backend, "agent", None) is None:
        backend.agent = agent

    async def authenticator(register):
        return agent

    kwargs = {}
    if kick_others is not None:
        kwargs["kick_others"] = kick_others
    if register_connection is not None:
        kwargs["register_connection"] = register_connection

    protocol = AgentProtocol(
        send=send,
        close=close,
        queue=queue if queue is not None else InMemoryAgentQueue(),
        backend=backend,
        authenticator=authenticator,
        heartbeat_interval=heartbeat_interval,
        heartbeat_timeout=heartbeat_timeout,
        **kwargs,
    )
    return protocol, sent, closed, agent


async def _wait_for(predicate, timeout=2.0, interval=0.01):
    """Poll ``predicate`` until true or ``timeout`` elapses; return its truthiness."""
    waited = 0.0
    while waited < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        waited += interval
    return predicate()


def _register_frame(instance_id="unit-agent", token=TEST_TOKEN, force=False, session_id=None):
    return messages.Register(token=token, force=force, session_id=session_id).model_dump_json()


@pytest.mark.asyncio
class TestAgentProtocolUnit:
    async def test_invalid_json_closes(self):
        protocol, sent, closed, _ = make_protocol()
        await protocol.receive("this is not json")
        assert closed == [FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE]
        assert sent == []

    async def test_first_message_must_be_register(self):
        protocol, sent, closed, _ = make_protocol()
        await protocol.receive(messages.HeartbeatEvent().model_dump_json())
        assert closed == [FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE]

    async def test_schema_mismatch_sends_protocol_error_then_closes(self):
        protocol, sent, closed, _ = make_protocol()
        await protocol.receive('{"type": "TOTALLY_UNKNOWN"}')
        assert json.loads(sent[0])["type"] == messages.ToAgentMessageType.PROTOCOL_ERROR.value
        assert closed == [FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE]

    async def test_register_sends_init(self):
        protocol, sent, closed, agent = make_protocol()
        await protocol.receive(_register_frame())

        init = json.loads(sent[0])
        assert init["type"] == messages.ToAgentMessageType.INIT.value
        assert init["agent"] == str(agent.pk)
        assert init["inquiries"] == []
        assert closed == []
        await protocol.shutdown()

    async def test_blocked_agent_closes_without_init(self):
        protocol, sent, closed, _ = make_protocol(agent=FakeAgent(blocked=True))
        await protocol.receive(_register_frame())
        assert closed == [AGENT_IS_BLOCKED_CODE]
        assert sent == []

    async def test_register_rejected_when_already_connected_without_force(self):
        # An already-connected agent + no force -> ProtocolError then close 4004,
        # and crucially NO Init (the incumbent connection keeps the agent).
        kicked = []

        async def kick_others():
            kicked.append(True)

        protocol, sent, closed, _ = make_protocol(agent=FakeAgent(connected=True), kick_others=kick_others)
        await protocol.receive(_register_frame(force=False))

        assert json.loads(sent[0])["type"] == messages.ToAgentMessageType.PROTOCOL_ERROR.value
        assert closed == [AGENT_ALREADY_CONNECTED_CODE]
        assert kicked == []  # nobody is displaced on a plain rejection
        # No session (and thus no background loops) for a rejected registration.
        assert protocol.session is None

    async def test_force_register_kicks_incumbent_when_connected(self):
        # An already-connected agent + force -> displace the incumbent and proceed
        # to a normal Init.
        kicked = []

        async def kick_others():
            kicked.append(True)

        protocol, sent, closed, agent = make_protocol(agent=FakeAgent(connected=True), kick_others=kick_others)
        await protocol.receive(_register_frame(force=True))

        assert kicked == [True]
        assert json.loads(sent[0])["type"] == messages.ToAgentMessageType.INIT.value
        assert closed == []
        await protocol.shutdown()

    async def test_force_register_does_not_kick_when_not_connected(self):
        # force is a no-op when there is no incumbent: nobody gets kicked.
        kicked = []

        async def kick_others():
            kicked.append(True)

        protocol, sent, closed, _ = make_protocol(agent=FakeAgent(connected=False), kick_others=kick_others)
        await protocol.receive(_register_frame(force=True))

        assert kicked == []
        assert json.loads(sent[0])["type"] == messages.ToAgentMessageType.INIT.value
        await protocol.shutdown()

    async def test_live_incumbent_rejected_without_force(self):
        # A LIVE incumbent (connected + a fresh heartbeat) still wins: no force -> 4004, no Init,
        # nobody displaced. This is the singleton invariant the gate must keep protecting.
        kicked = []

        async def kick_others():
            kicked.append(True)

        agent = FakeAgent(connected=True, last_seen=timezone.now())
        protocol, sent, closed, _ = make_protocol(agent=agent, kick_others=kick_others)
        await protocol.receive(_register_frame(force=False))

        assert json.loads(sent[0])["type"] == messages.ToAgentMessageType.PROTOCOL_ERROR.value
        assert closed == [AGENT_ALREADY_CONNECTED_CODE]
        assert kicked == []
        assert protocol.session is None

    async def test_stale_incumbent_auto_takeover_without_force(self):
        # A STALE incumbent (connected stuck True but its heartbeat expired) is auto-displaced
        # WITHOUT force: no 4004, a normal Init, and kick_others is called to boot any lingering
        # half-open socket. This is the fix for "had to reconnect with --force".
        kicked = []

        async def kick_others():
            kicked.append(True)

        stale = timezone.now() - datetime.timedelta(seconds=120)
        agent = FakeAgent(connected=True, last_seen=stale)
        protocol, sent, closed, _ = make_protocol(agent=agent, kick_others=kick_others)
        await protocol.receive(_register_frame(force=False))

        assert json.loads(sent[0])["type"] == messages.ToAgentMessageType.INIT.value
        assert closed == []
        assert kicked == [True]
        assert protocol.session is not None
        await protocol.shutdown()

    # ----------------------------------------------------------------------- #
    # Lease fencing: a connection that cannot renew must terminate itself.
    # ----------------------------------------------------------------------- #
    async def test_executor_carries_the_claimed_lease_epoch(self):
        protocol, sent, closed, _ = make_protocol()
        await protocol.receive(_register_frame())

        assert protocol.session.lease_epoch == protocol.backend.epoch
        await protocol.shutdown()

    async def test_heartbeat_renews_the_lease(self):
        protocol, sent, closed, _ = make_protocol()
        await protocol.receive(_register_frame())
        protocol.session.heartbeat_future = asyncio.get_event_loop().create_future()

        await protocol.session.on_agent_heartbeat()

        assert ("renew", protocol.session.agent.pk, protocol.session.lease_epoch) in protocol.backend.calls
        assert closed == []
        await protocol.shutdown()

    async def test_fenced_heartbeat_closes_the_connection(self):
        # The connection was displaced (or the sweep revoked it) while it was still running: its
        # renewal matches no row, so it must close rather than keep draining the executor queue
        # against a lease it no longer holds. ``kick_others`` may never have reached it.
        protocol, sent, closed, _ = make_protocol()
        await protocol.receive(_register_frame())
        protocol.session.heartbeat_future = asyncio.get_event_loop().create_future()

        listen_task = protocol.session.listen_task
        protocol.backend.epoch += 1  # somebody else claimed the lease behind our back
        await protocol.session.on_agent_heartbeat()

        assert closed == [AGENT_REPLACED_CODE]
        # And it must have STOPPED DRAINING, not merely sent a close frame: Channels does not
        # call disconnect() for a server-initiated close, so shutdown() (which cancels the loops)
        # may not run until the peer acknowledges — or ever. Until then this connection would go
        # on popping Assigns off the redis queue against a lease it no longer holds.
        assert listen_task.done(), "a fenced connection must stop draining the executor queue"
        await protocol.shutdown()

    async def test_unanswered_heartbeat_also_stops_draining(self):
        # Same defect class on the pre-existing heartbeat-timeout path: an agent that stopped
        # answering must stop being handed work immediately, not whenever the close lands.
        protocol, sent, closed, _ = make_protocol(heartbeat_interval=0.05, heartbeat_timeout=0.1)
        await protocol.receive(_register_frame())
        listen_task = protocol.session.listen_task

        assert await _wait_for(lambda: closed == [HEARTBEAT_NOT_RESPONDED_CODE])
        assert listen_task.done()
        await protocol.shutdown()

    # ----------------------------------------------------------------------- #
    # Every connection is an agent: no modes, no capability gating, one lease.
    # ----------------------------------------------------------------------- #
    async def test_every_connection_claims_a_lease_and_drains_the_queue(self):
        # There is no non-executor connection kind any more. Register used to carry a `mode`
        # that could yield a caller/observer session with no lease and no queue drain; now a
        # registered session always holds an epoch and always drains.
        protocol, sent, closed, _ = make_protocol()
        await protocol.receive(_register_frame())

        assert protocol.session.lease_epoch == protocol.backend.epoch
        assert protocol.session.listen_task is not None
        assert protocol.session.heartbeat_task is not None
        await protocol.shutdown()

    async def test_register_rejects_an_unknown_field(self):
        # `mode` is gone from the wire. A client still sending it must fail loudly at
        # validation rather than being silently treated as an executor.
        protocol, sent, closed, _ = make_protocol()
        await protocol.receive(json.dumps({"type": "REGISTER", "token": TEST_TOKEN, "mode": "OBSERVER"}))

        assert protocol.session is None
        assert closed == [FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE]

    async def test_executor_threads_session_id_to_backend_connect(self):
        recorded = {}

        class RecordingBackend(FakeBackend):
            async def on_agent_connected(self, agent_id, connection_id=None, session_id=None, force=False):
                recorded["session_id"] = session_id
                return await super().on_agent_connected(agent_id, connection_id, session_id, force)

        protocol, sent, closed, _ = make_protocol(backend=RecordingBackend())
        await protocol.receive(_register_frame(session_id="proc-xyz"))
        assert recorded["session_id"] == "proc-xyz"
        await protocol.shutdown()

    async def test_caller_assign_routes_and_acks(self):
        backend = FakeBackend()
        protocol, sent, closed, agent = make_protocol(backend=backend)
        await protocol.receive(_register_frame())
        sent.clear()

        req = messages.AssignRequest(reference="ref-1", action="act-1", args={"x": 1})
        await protocol.receive(req.model_dump_json())

        # routed to the backend
        assert any(c[0] == "caller_assign" for c in backend.calls)
        # and a AssignResponse ack echoing the request id + reference
        result = json.loads(sent[-1])
        assert result["type"] == messages.ToAgentMessageType.ASSIGN_RESPONSE.value
        assert result["request"] == req.id and result["reference"] == "ref-1"
        assert result["task"] == "new-ass-1" and result["created"] is True
        assert closed == []
        await protocol.shutdown()

    async def test_caller_assign_failure_nacks_without_closing(self):
        # A backend error must nack the caller, NOT close the socket (which would kill all
        # the agent's other work).
        backend = FakeBackend(caller_assign_error=PermissionError("parent is required"))
        protocol, sent, closed, _ = make_protocol(backend=backend)
        await protocol.receive(_register_frame())
        sent.clear()

        await protocol.receive(messages.AssignRequest(reference="ref-2", args={}).model_dump_json())

        result = json.loads(sent[-1])
        assert result["type"] == messages.ToAgentMessageType.ASSIGN_RESPONSE.value
        assert result["task"] is None and result["created"] is False
        assert "parent is required" in result["error"]
        assert closed == []  # crucially, the connection stays open
        await protocol.shutdown()

    async def test_a_declaring_register_implements_and_init_carries_the_diagnostics(self):
        backend = FakeBackend()
        protocol, sent, closed, agent = make_protocol(backend=backend)

        await protocol.receive(messages.Register(token=TEST_TOKEN, hash="h1", implementations=[]).model_dump_json())

        (call,) = [c for c in backend.calls if c[0] == "implement"]
        assert call[1] == agent.pk and call[2].hash == "h1"
        init = json.loads(sent[-1])
        assert init["type"] == messages.ToAgentMessageType.INIT.value
        assert init["hash"] == "h1" and init["diagnostics"][0]["code"] == "unknown_operation"
        assert closed == []
        await protocol.shutdown()

    async def test_a_bare_register_implements_nothing(self):
        backend = FakeBackend()
        protocol, sent, closed, _ = make_protocol(backend=backend)
        await protocol.receive(_register_frame())

        assert not any(c[0] == "implement" for c in backend.calls)
        assert json.loads(sent[-1])["type"] == messages.ToAgentMessageType.INIT.value
        await protocol.shutdown()

    async def test_a_matching_hash_skips_the_reconciliation(self):
        agent = FakeAgent()
        agent.hash = "same"
        backend = FakeBackend()
        protocol, sent, closed, _ = make_protocol(agent=agent, backend=backend)

        await protocol.receive(messages.Register(token=TEST_TOKEN, hash="same", implementations=[]).model_dump_json())

        assert not any(c[0] == "implement" for c in backend.calls)
        assert json.loads(sent[-1])["hash"] == "same"
        await protocol.shutdown()

    async def test_a_refused_declaration_closes_with_the_registration_code(self):
        backend = FakeBackend()
        backend.registration_error = ValueError("does not fit the catalog")
        protocol, sent, closed, _ = make_protocol(backend=backend)

        await protocol.receive(messages.Register(token=TEST_TOKEN, hash="h2", implementations=[]).model_dump_json())

        error = json.loads(sent[-1])
        assert error["type"] == messages.ToAgentMessageType.PROTOCOL_ERROR.value
        assert "does not fit the catalog" in error["error"]
        assert closed == [AGENT_REGISTRATION_REJECTED_CODE]
        assert protocol.session is None
        await protocol.shutdown()

    async def test_shelve_and_unshelve_route_and_reply(self):
        backend = FakeBackend()
        protocol, sent, closed, _ = make_protocol(backend=backend)
        await protocol.receive(_register_frame())
        sent.clear()

        await protocol.receive(messages.Shelve(ref="s-1", identifier="@x/y", resource_id="1").model_dump_json())
        shelved = json.loads(sent[-1])
        assert shelved["type"] == messages.ToAgentMessageType.SHELVED.value
        assert shelved["ref"] == "s-1" and shelved["drawer"] == "drawer-1" and shelved["error"] is None

        await protocol.receive(messages.Unshelve(ref="u-1", drawer="drawer-1").model_dump_json())
        unshelved = json.loads(sent[-1])
        assert unshelved["type"] == messages.ToAgentMessageType.UNSHELVED.value
        assert unshelved["ref"] == "u-1" and unshelved["error"] is None
        assert [c[0] for c in backend.calls if c[0] in ("shelve", "unshelve")] == ["shelve", "unshelve"]
        assert closed == []
        await protocol.shutdown()

    async def test_shelving_failures_reply_with_an_error_and_keep_the_socket(self):
        backend = FakeBackend()
        backend.registration_error = ValueError("refused")
        protocol, sent, closed, _ = make_protocol(backend=backend)
        await protocol.receive(_register_frame())
        sent.clear()

        for request in (
            messages.Shelve(ref="s-2", identifier="@x/y", resource_id="1"),
            messages.Unshelve(ref="u-2", drawer="d"),
        ):
            await protocol.receive(request.model_dump_json())
            reply = json.loads(sent[-1])
            assert reply["ref"] == request.ref and reply["error"] == "refused"
        assert closed == []
        await protocol.shutdown()

    async def test_init_carries_the_agents_hash(self):
        agent = FakeAgent()
        agent.hash = "stored"
        protocol, sent, closed, _ = make_protocol(agent=agent)
        await protocol.receive(_register_frame())

        init = json.loads(sent[-1])
        assert init["type"] == messages.ToAgentMessageType.INIT.value and init["hash"] == "stored"
        await protocol.shutdown()

    async def test_terminal_event_is_acked(self):
        backend = FakeBackend()
        protocol, sent, closed, _ = make_protocol(backend=backend)
        await protocol.receive(_register_frame())
        sent.clear()

        done = messages.Completed(task="ass-9", seq=7)
        await protocol.receive(done.model_dump_json())

        ack = json.loads(sent[-1])
        assert ack["type"] == messages.ToAgentMessageType.EVENT_ACK.value
        assert ack["event"] == done.id and ack["task"] == "ass-9" and ack["seq"] == 7
        await protocol.shutdown()

    async def test_caller_control_routes_and_acks(self):
        backend = FakeBackend()
        protocol, sent, closed, _ = make_protocol(backend=backend)
        await protocol.receive(_register_frame())
        sent.clear()

        req = messages.CancelRequest(task="ass-7", auto_interrupt=5)
        await protocol.receive(req.model_dump_json())

        assert any(c[0] == "caller_cancel" for c in backend.calls)
        result = json.loads(sent[-1])
        assert result["type"] == messages.ToAgentMessageType.CONTROL_RESPONSE.value
        assert result["request"] == req.id and result["accepted"] is True
        assert closed == []
        await protocol.shutdown()

    async def test_caller_control_failure_nacks_without_closing(self):
        backend = FakeBackend(caller_assign_error=PermissionError("not the caller"))
        protocol, sent, closed, _ = make_protocol(backend=backend)
        await protocol.receive(_register_frame())
        sent.clear()

        await protocol.receive(messages.CancelRequest(task="ass-x").model_dump_json())

        result = json.loads(sent[-1])
        assert result["type"] == messages.ToAgentMessageType.CONTROL_RESPONSE.value
        assert result["accepted"] is False and "not the caller" in result["error"]
        assert closed == []  # a bad control request never tears down the socket
        await protocol.shutdown()

    async def test_unhandled_message_closes(self):
        protocol, sent, closed, _ = make_protocol()
        await protocol.receive(_register_frame())
        # A second Register (after the handshake) has no router case → closes the socket.
        await protocol.receive(_register_frame())
        assert FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE in closed
        await protocol.shutdown()

    async def test_log_event_routes_to_backend(self):
        backend = FakeBackend()
        protocol, sent, closed, _ = make_protocol(backend=backend)
        await protocol.receive(_register_frame())
        await protocol.receive(messages.Log(task=str(uuid.uuid4()), message="hello", level="INFO").model_dump_json())
        assert any(call[0] == "log" for call in backend.calls)
        await protocol.shutdown()

    async def test_shutdown_marks_agent_disconnected(self):
        backend = FakeBackend()
        protocol, sent, closed, agent = make_protocol(backend=backend)
        await protocol.receive(_register_frame())
        await protocol.shutdown()
        assert ("disconnected", agent.pk) in backend.calls

    async def test_queued_message_is_relayed_to_agent(self):
        queue = InMemoryAgentQueue()
        protocol, sent, closed, agent = make_protocol(queue=queue)
        await protocol.receive(_register_frame())

        queue.push(str(agent.pk), '{"hello": 1}')

        assert await _wait_for(lambda: any('"hello"' in s for s in sent))
        await protocol.shutdown()

    async def test_heartbeat_answer_keeps_protocol_open(self):
        protocol, sent, closed, _ = make_protocol(heartbeat_interval=0.05, heartbeat_timeout=0.3)
        await protocol.receive(_register_frame())

        def _heartbeats():
            return [s for s in sent if json.loads(s)["type"] == messages.ToAgentMessageType.HEARTBEAT.value]

        assert await _wait_for(lambda: len(_heartbeats()) >= 1)
        await protocol.session.on_agent_heartbeat()

        # Give the loop time to time out if the answer had not been accepted.
        await asyncio.sleep(0.2)
        assert HEARTBEAT_NOT_RESPONDED_CODE not in closed
        await protocol.shutdown()

    async def test_unanswered_heartbeat_closes(self):
        protocol, sent, closed, _ = make_protocol(heartbeat_interval=0.05, heartbeat_timeout=0.1)
        await protocol.receive(_register_frame())

        assert await _wait_for(lambda: HEARTBEAT_NOT_RESPONDED_CODE in closed)
        await protocol.shutdown()

    # ----------------------------------------------------------------------- #
    # Race-condition isolation (R1-R4). Each test fails if its fix is reverted.
    # ----------------------------------------------------------------------- #
    async def test_concurrent_sends_are_serialized(self):
        # R1: every outbound frame funnels through one lock. A send that yields
        # mid-flight must never have a second send overlapping it. Without the
        # lock, the gathered sends would all enter and ``max`` would reach 10.
        state = {"now": 0, "max": 0}

        async def send(text):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            await asyncio.sleep(0)  # yield: an unguarded peer could interleave here
            state["now"] -= 1

        async def close(code):
            pass

        async def authenticator(register):
            return FakeAgent()

        protocol = AgentProtocol(
            send=send,
            close=close,
            queue=InMemoryAgentQueue(),
            backend=FakeBackend(),
            authenticator=authenticator,
        )

        # All concurrent producers (heartbeat ping, listen relay, receive-path
        # sends) reach the wire through ``send_to_agent_message`` / ``_send``.
        await asyncio.gather(*[protocol.send_to_agent_message(messages.Heartbeat()) for _ in range(10)])

        assert state["max"] == 1

    async def test_heartbeat_answer_resolved_before_persist(self):
        # R2: on_agent_heartbeat must resolve the handshake future BEFORE awaiting
        # the (slow) DB write. A blocking lease renewal must not delay resolution.
        agent = FakeAgent()
        release = asyncio.Event()
        backend = FakeBackend(agent=agent)

        async def blocking_renew(agent_id, lease_epoch):
            await release.wait()
            return True

        backend.renew_agent_lease = blocking_renew

        protocol, sent, closed, _ = make_protocol(agent=agent, backend=backend)
        # Build the post-registration session directly to exercise its heartbeat handling.
        session = RegisteredSession(
            agent=agent,
            session_id=None,
            caller_id="caller",
            connection_id=protocol.connection_id,
            lease_epoch=1,
            backend=protocol.backend,
            queue=protocol.queue,
            send_to_agent_message=protocol.send_to_agent_message,
            send=protocol._send,
            close=protocol.close,
            heartbeat_interval=protocol.heartbeat_interval,
            heartbeat_timeout=protocol.heartbeat_timeout,
        )
        protocol.session = session
        future = asyncio.get_event_loop().create_future()
        session.heartbeat_future = future

        task = asyncio.create_task(session.on_agent_heartbeat())
        try:
            # The future is resolved even though the lease renewal is still blocked.
            assert await _wait_for(lambda: future.done())
            assert not task.done()  # still parked in the renewal
        finally:
            release.set()
            await task

    async def test_heartbeat_loop_stops_after_timeout_close(self):
        # R3: after an unanswered timeout the loop must terminate, not keep
        # pinging a closed socket.
        protocol, sent, closed, _ = make_protocol(heartbeat_interval=0.05, heartbeat_timeout=0.1)
        await protocol.receive(_register_frame())

        def _pings():
            return [s for s in sent if json.loads(s)["type"] == messages.ToAgentMessageType.HEARTBEAT.value]

        assert await _wait_for(lambda: HEARTBEAT_NOT_RESPONDED_CODE in closed)
        pings_at_close = len(_pings())

        # Several more intervals elapse; a live loop would emit more pings/closes.
        await asyncio.sleep(0.3)
        assert len(_pings()) == pings_at_close == 1
        assert closed.count(HEARTBEAT_NOT_RESPONDED_CODE) == 1
        await protocol.shutdown()

    async def test_second_register_is_rejected_without_respawning_tasks(self):
        # R4: a duplicate Register must not re-run on_register (which would orphan
        # the first listen/heartbeat task pair). It closes instead.
        protocol, sent, closed, _ = make_protocol()
        await protocol.receive(_register_frame())
        session = protocol.session
        first_listen = session.listen_task
        first_heartbeat = session.heartbeat_task

        await protocol.receive(_register_frame())

        assert FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE in closed
        # The session and its task handles are untouched — no new pair was spawned.
        assert protocol.session is session
        assert session.listen_task is first_listen
        assert session.heartbeat_task is first_heartbeat

        await protocol.shutdown()
        # The original pair is the one shutdown cancels — nothing left orphaned.
        assert first_listen.cancelled() or first_listen.done()
        assert first_heartbeat.cancelled() or first_heartbeat.done()


class FlakyQueue(InMemoryAgentQueue):
    """An in-memory queue whose next ``failures`` pops raise — a redis blip, without a redis."""

    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures
        self.closes = 0
        self.recoveries = 0

    async def pop(self, agent_id):
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("redis went away")
        return await super().pop(agent_id)

    async def recover(self, agent_id):
        self.recoveries += 1
        return 0

    async def close(self):
        self.closes += 1


@pytest.mark.asyncio
class TestDrainLoopNeverDiesQuietly:
    """The drain loop is the ONLY way work reaches the agent, while liveness is decided by the
    heartbeat next to it. If it stops on its own the agent keeps looking alive, keeps being
    selected, and never receives anything — every task assigned to it stays QUEUED forever."""

    async def test_queue_failure_is_survived(self):
        queue = FlakyQueue(failures=2)
        protocol, sent, closed, agent = make_protocol(queue=queue)
        await protocol.receive(_register_frame())

        queue.push(str(agent.pk), '{"after": "the-blip"}')

        assert await _wait_for(lambda: any('"the-blip"' in s for s in sent), timeout=5.0)
        assert not protocol.session.listen_task.done()
        assert closed == []  # a queue blip is not the agent's problem
        assert queue.closes == 2  # the broken connection was dropped each time…
        assert queue.recoveries == 3  # …and popped-but-unacked frames recovered on every (re)start
        await protocol.shutdown()

    async def test_send_failure_closes_the_socket(self):
        queue = InMemoryAgentQueue()
        protocol, sent, closed, agent = make_protocol(queue=queue)
        await protocol.receive(_register_frame())

        async def broken_send(text):
            raise RuntimeError("socket is gone")

        protocol.session._send = broken_send
        queue.push(str(agent.pk), '{"undeliverable": 1}')

        from facade.codes import AGENT_TRANSPORT_FAILED_CODE

        assert await _wait_for(lambda: AGENT_TRANSPORT_FAILED_CODE in closed)
        await protocol.shutdown()

    async def test_assign_for_a_finalized_task_is_dropped(self):
        backend = FakeBackend()
        backend.closed_tasks = {"41"}
        queue = InMemoryAgentQueue()
        protocol, sent, closed, agent = make_protocol(queue=queue, backend=backend)
        await protocol.receive(_register_frame())

        def _assign(task):
            return messages.Assign(interface="i", task=task, args={}, user="1", org="o", action="a", implementation="1").model_dump_json()

        queue.push(str(agent.pk), _assign("41"))  # finalized by the server while it waited
        queue.push(str(agent.pk), _assign("42"))

        assert await _wait_for(lambda: any('"42"' in s for s in sent))
        assert not any('"task":"41"' in s for s in sent)
        await protocol.shutdown()


class RecordingKeyQueue(InMemoryAgentQueue):
    """Records the agent key every queue method is called with."""

    def __init__(self):
        super().__init__()
        self.keys: dict[str, set] = {}

    def _note(self, method, agent_id):
        self.keys.setdefault(method, set()).add(repr(agent_id))

    def push(self, agent_id, message_json, *, priority=False):
        self._note("push", agent_id)
        return super().push(agent_id, message_json, priority=priority)

    async def pop(self, agent_id):
        self._note("pop", agent_id)
        return await super().pop(agent_id)

    async def ack(self, agent_id, message):
        self._note("ack", agent_id)
        return await super().ack(agent_id, message)

    async def recover(self, agent_id):
        self._note("recover", agent_id)
        return await super().recover(agent_id)

    async def requeue(self, agent_id, message):
        self._note("requeue", agent_id)
        return await super().requeue(agent_id, message)


@pytest.mark.asyncio
async def test_every_queue_method_gets_the_same_agent_key():
    """One key type, or the in-flight area silently splits in two.

    ``agent.pk`` is an int and the queue is addressed by string. Passing the raw pk to some
    methods and ``str(pk)`` to others works only while the backing store stringifies for you:
    redis does (its keys are built by ``redis_keys.key``), an in-memory queue keyed by the value
    it was handed does not — it would park a frame under ``1`` that ``recover`` then hunts for
    under ``"1"``, which is exactly how a recovered frame goes missing.
    """
    queue = RecordingKeyQueue()
    protocol, sent, closed, agent = make_protocol(queue=queue)
    await protocol.receive(_register_frame())

    queue.push(protocol.session.agent_key, '{"hello": 1}')
    assert await _wait_for(lambda: any('"hello"' in s for s in sent))
    await protocol.shutdown()

    used = {k for keys in queue.keys.values() for k in keys}
    assert len(used) == 1, f"queue methods disagreed on the agent key: {queue.keys}"
    assert used == {repr(str(agent.pk))}, f"the queue must be addressed by str(pk), got {used}"


@pytest.mark.asyncio
class TestAtLeastOnceIsActuallyTestable:
    """These three branches of ``listen_for_tasks`` were unreachable in every unit test until the
    in-memory queue grew a real in-flight area: with a no-op ``ack``, a ``recover`` hardcoded to 0
    and a ``pop`` that blocked forever, there was nothing to recover and no idle tick to do it on."""

    async def test_a_delivered_frame_is_acked_out_of_flight(self):
        queue = InMemoryAgentQueue()
        protocol, sent, closed, agent = make_protocol(queue=queue)
        await protocol.receive(_register_frame())
        key = protocol.session.agent_key

        queue.push(key, '{"delivered": 1}')
        assert await _wait_for(lambda: any('"delivered"' in s for s in sent))
        # Delivered AND acked: nothing is left in flight, so a later recover finds nothing.
        assert await _wait_for(lambda: not queue._inflight[key])
        assert await queue.recover(key) == 0
        await protocol.shutdown()

    async def test_an_idle_tick_recovers_a_frame_stranded_by_a_dead_holder(self):
        """A holder that died between its pop and its ack leaves the frame in flight. The next
        holder has nothing of its own outstanding, so an empty pop is when it looks."""
        queue = InMemoryAgentQueue()
        protocol, sent, closed, agent = make_protocol(queue=queue)
        await protocol.receive(_register_frame())
        key = protocol.session.agent_key

        # Simulate the previous connection's popped-but-unacked frame.
        queue._inflight[key].appendleft('{"stranded": 1}')

        assert await _wait_for(lambda: any('"stranded"' in s for s in sent), timeout=5.0)
        await protocol.shutdown()

    async def test_a_frame_whose_ack_failed_is_recovered_not_lost(self):
        class AckFails(InMemoryAgentQueue):
            def __init__(self):
                super().__init__()
                self.failed = False

            async def ack(self, agent_id, message):
                if not self.failed:
                    self.failed = True
                    raise ConnectionError("ack lost the connection")
                return await super().ack(agent_id, message)

        queue = AckFails()
        protocol, sent, closed, agent = make_protocol(queue=queue)
        await protocol.receive(_register_frame())
        key = protocol.session.agent_key

        queue.push(key, '{"unacked": 1}')

        # Delivered; its ack failed, so at-least-once redelivers it rather than dropping it.
        assert await _wait_for(lambda: len([s for s in sent if '"unacked"' in s]) >= 2, timeout=5.0)
        assert queue.failed
        await protocol.shutdown()

    async def test_requeue_will_not_duplicate_a_frame_a_new_holder_recovered(self):
        """The conditional requeue is the reason redis uses a Lua ``LREM>0 then RPUSH``."""
        queue = InMemoryAgentQueue()
        queue.push("7", '{"frame": 1}')
        frame = await queue.pop("7")
        assert frame is not None and queue._inflight["7"]

        # The new lease holder recovers it first…
        assert await queue.recover("7") == 1
        # …and the displaced holder then tries to hand back the same frame.
        await queue.requeue("7", frame)

        assert len(queue._queues["7"]) == 1, "the frame must exist exactly once"

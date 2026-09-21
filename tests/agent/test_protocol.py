"""Full-stack agent protocol/lifecycle tests.

These drive the real ``AgentConsumer`` (``facade/consumers/async_consumer.py``)
through Django Channels' ``WebsocketCommunicator`` against real Postgres and real
Redis (both started by the ``backend_stack`` dokker fixture in ``conftest.py``).

``default_authenticator`` has ensure semantics: a ``Register`` creates the agent (and its
memory shelve) for the token's identity when none exists. ``open_agent``/``seed_agent`` still
pre-create it for the tests that need a specific hash or a blocked agent; the socket-only
registration path is covered in ``test_registration.py``.
"""

import pytest

from facade import enums, messages
from facade.codes import (
    AGENT_IS_BLOCKED_CODE,
    FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE,
    FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE,
)
from facade.models import Agent, TaskEvent

from tests.agent.helpers import connect_agent, open_agent
from tests.factories import TEST_TOKEN, build_unimplemented_task_for_agent


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestAgentWalkingSkeleton:
    async def test_register_returns_init_for_seeded_agent(self, agent_ws):
        session = await open_agent(agent_ws, "skeleton-agent")
        assert session.init.agent == str(session.agent.pk)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestAgentProtocol:
    async def test_connection_is_accepted(self, agent_ws):
        # The fixture asserts connection success; reaching here proves accept().
        await connect_agent(agent_ws)

    async def test_first_message_must_be_register(self, agent_ws):
        session = await connect_agent(agent_ws)
        await session.send(messages.HeartbeatEvent())  # valid, but not a Register
        await session.expect_close(FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE)

    async def test_invalid_json_closes_socket(self, agent_ws):
        session = await connect_agent(agent_ws)
        await session.send_raw("this is not json")
        await session.expect_close(FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE)

    async def test_schema_mismatch_sends_protocol_error_then_closes(self, agent_ws):
        session = await connect_agent(agent_ws)
        await session.send_raw('{"type": "TOTALLY_UNKNOWN"}')

        error = await session.receive(messages.ProtocolError)
        assert error.error

        await session.expect_close(FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE)

    async def test_successful_register_returns_init_without_inquiries(self, agent_ws):
        session = await open_agent(agent_ws, "protocol-agent")
        assert session.init.inquiries == []

    async def test_blocked_agent_is_rejected(self, agent_ws):
        session = await open_agent(agent_ws, "blocked-agent", blocked=True, register=False)
        await session.send(messages.Register(token=TEST_TOKEN))
        await session.expect_close(AGENT_IS_BLOCKED_CODE)

    async def test_disconnect_marks_agent_disconnected(self, agent_ws):
        session = await open_agent(agent_ws, "disconnect-agent")

        await session.disconnect()

        agent = await Agent.objects.aget(pk=session.agent_pk)
        assert agent.connected is False

    async def test_disconnect_marks_unimplemented_task(self, agent_ws):
        # Regression (B1): an unfinished task owned directly by the agent but with a
        # null implementation must still get a DISCONNECTED event on disconnect. The handler
        # previously filtered by ``implementation__agent`` and silently skipped these rows.
        session = await open_agent(agent_ws, "disconnect-unimpl-agent")
        task = await build_unimplemented_task_for_agent(session.agent_pk, "disc-unimpl")

        await session.disconnect()

        events = [e async for e in TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.DISCONNECTED)]
        assert len(events) == 1

    async def test_register_for_uncreated_agent_creates_it(self, agent_ws):
        # Ensure semantics: the socket is the agent's only control plane, so a Register from
        # an identity without an agent creates one (app/release from the token's client).
        session = await connect_agent(agent_ws)
        await session.send(messages.Register(token=TEST_TOKEN))
        init = await session.receive(messages.Init)
        assert init.hash is None
        assert await Agent.objects.filter(pk=init.agent).aexists()

    async def test_valid_but_unhandled_message_closes_socket(self, agent_ws):
        # A well-formed FromAgentMessage with no handler hits the ``case _:`` branch in the
        # router and closes 3003. A second Register (after the handshake) is such a message —
        # registration only happens on the first frame.
        session = await open_agent(agent_ws, "unhandled-agent")
        await session.send(messages.Register(token=TEST_TOKEN))
        await session.expect_close(FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE)

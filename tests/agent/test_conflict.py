"""Connection-conflict handling — one live connection per agent.

Two communicators share the same token -> same registry -> the SAME single Agent
(agents are unique per registry now). The cross-connection kick runs over the
Channels group layer (``settings_test`` uses the in-process InMemory layer, so a
group_send from one communicator reaches the other in the same process).
"""

import asyncio
from datetime import timedelta

import pytest
from django.utils import timezone

from facade import enums, messages
from facade.codes import AGENT_ALREADY_CONNECTED_CODE, AGENT_REPLACED_CODE
from facade.models import Agent, TaskEvent

from tests.agent.helpers import connect_agent, open_agent
from tests.factories import TEST_TOKEN, build_unimplemented_task_for_agent, seed_agent


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestAgentConnectionConflict:
    async def test_second_connection_without_force_is_rejected(self, agent_ws):
        incumbent = await open_agent(agent_ws, "dup-agent")

        intruder = await connect_agent(agent_ws)
        await intruder.send(messages.Register(token=TEST_TOKEN, force=False))
        assert (await intruder.receive(messages.ProtocolError)).error
        await intruder.expect_close(AGENT_ALREADY_CONNECTED_CODE)

        # The incumbent keeps the agent.
        agent = await Agent.objects.aget(pk=incumbent.agent_pk)
        assert agent.connected is True

    async def test_force_connection_kicks_incumbent(self, agent_ws):
        incumbent = await open_agent(agent_ws, "dup-agent")

        taker = await connect_agent(agent_ws)
        init = await taker.register(force=True)
        assert init.agent == incumbent.init.agent

        # The incumbent connection is force-closed with the replaced code.
        await incumbent.expect_close(AGENT_REPLACED_CODE)

        # The takeover stays connected: the displaced connection's shutdown must NOT have
        # flipped ``connected`` off (generation guard on active_connection_id).
        agent = await Agent.objects.aget(pk=incumbent.agent_pk)
        assert agent.connected is True

    async def test_displaced_connection_does_not_lose_tasks(self, agent_ws):
        # Generation guard, end to end: an in-flight task owned by the agent must NOT
        # end LOST when the incumbent is displaced by a force-register —
        # only a genuine disconnect of the active connection should.
        incumbent = await open_agent(agent_ws, "dup-agent")
        task = await build_unimplemented_task_for_agent(incumbent.agent_pk, "displace-guard")

        taker = await connect_agent(agent_ws)
        await taker.register(force=True)

        await incumbent.expect_close(AGENT_REPLACED_CODE)  # let the displaced connection finish closing
        await asyncio.sleep(0.1)

        lost = [e async for e in TaskEvent.objects.filter(task_id=task.pk, kind=enums.TaskEventKind.LOST)]
        assert lost == []

    async def test_stale_incumbent_reconnects_without_force(self, agent_ws):
        # The ticket, end to end: a crashed connection left ``connected`` stuck True with a stale
        # ``last_seen`` (its disconnect never ran). A brand-new connection with NO force must take
        # over automatically instead of being rejected with 4004 — no more ``--force`` dance.
        agent = await seed_agent("stale-agent")
        await Agent.objects.filter(pk=agent.pk).aupdate(connected=True, last_seen=timezone.now() - timedelta(minutes=5))

        taker = await connect_agent(agent_ws)
        init = await taker.register(force=False)  # plain reconnect, no force
        assert init.agent == str(agent.pk)

        agent = await Agent.objects.aget(pk=agent.pk)
        assert agent.connected is True

"""Redis round-trip: a backend ``broadcast`` is relayed to the connected agent."""

import uuid

import pytest
from asgiref.sync import sync_to_async

from facade import messages
from facade.consumers.agent_queue import processing_key, queue_key
from facade.consumers.async_consumer import AgentConsumer

from tests.agent.helpers import open_agent


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestAgentDelivery:
    async def test_broadcast_is_delivered_to_agent(self, agent_ws):
        session = await open_agent(agent_ws, "delivery-agent")

        assign = messages.Assign(
            interface="iface",
            task=str(uuid.uuid4()),
            args={"a": 1},
            user="1",
            org="test-org",
            action="some-action",
            implementation="impl-1",
        )
        # broadcast() lpushes to the agent's redis queue; listen_for_tasks relays it.
        await sync_to_async(AgentConsumer.broadcast)(session.agent_pk, assign)

        received = await session.receive(messages.Assign)
        assert received.task == assign.task
        assert received.args == {"a": 1}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestAtLeastOnceDelivery:
    """``pop`` parks a frame in the agent's in-flight list until ``ack``. Whatever is still there
    when a connection takes over was popped by one that died (or was cancelled, or fenced)
    before acking — i.e. possibly never delivered. It used to stay there forever."""

    async def test_popped_but_unacked_frame_is_recovered_on_connect(self, agent_ws, agent_ws_redis):
        from django.conf import settings
        import redis

        from tests.factories import seed_agent

        agent = await seed_agent("recover-agent")
        stranded = messages.Assign(interface="iface", task=str(uuid.uuid4()), args={"stranded": True}, user="1", org="o", action="a", implementation="1")
        connection = redis.Redis(host=settings.AGENT_REDIS_HOST, port=settings.AGENT_REDIS_PORT)
        await sync_to_async(connection.lpush)(processing_key(agent.pk), stranded.model_dump_json())

        session = await open_agent(agent_ws, "recover-agent")

        received = await session.receive(messages.Assign)
        assert received.task == stranded.task
        assert await sync_to_async(connection.llen)(processing_key(agent.pk)) == 0  # delivered AND acked

    async def test_assign_for_a_finalized_task_is_not_delivered(self, agent_ws):
        from facade.models import Task
        from tests.factories import build_task

        session = await open_agent(agent_ws, "fence-agent")
        done = await build_task("fence-done", agent_pk=session.agent_pk)
        await Task.objects.filter(pk=done.pk).aupdate(is_done=True)
        live = await build_task("fence-live", agent_pk=session.agent_pk)

        def _assign(task):
            return messages.Assign(interface="iface", task=str(task.pk), args={}, user="1", org="o", action="a", implementation="1")

        await sync_to_async(AgentConsumer.broadcast)(session.agent_pk, _assign(done))
        await sync_to_async(AgentConsumer.broadcast)(session.agent_pk, _assign(live))

        assert (await session.receive(messages.Assign)).task == str(live.pk)


def _frame(marker: str) -> messages.Assign:
    return messages.Assign(interface="iface", task=str(uuid.uuid4()), args={"marker": marker}, user="1", org="o", action="a", implementation="1")


def _redis():
    import redis
    from django.conf import settings

    return redis.Redis(host=settings.AGENT_REDIS_HOST, port=settings.AGENT_REDIS_PORT, decode_responses=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestReplicaSafeDelivery:
    """Several backends share one redis and one queue per agent. These pin what keeps a
    connection on backend A from eating — or duplicating — what belongs to backend B's."""

    async def test_every_key_is_namespaced(self, agent_ws):
        """``42_my_queue`` is one integer away from another deployment's agent 42 on a shared redis."""
        session = await open_agent(agent_ws, "ns-agent")
        await sync_to_async(AgentConsumer.broadcast)(session.agent_pk, _frame("ns"))
        await session.receive(messages.Assign)

        from facade import redis_keys

        keys = await sync_to_async(lambda: [k for k in _redis().keys("*") if not k.startswith("asgi") and ":group:" not in k])()
        stray = [k for k in keys if not k.startswith(redis_keys.prefix() + ":")]
        assert stray == [], f"un-namespaced redis keys: {stray}"

    async def test_frames_queued_by_the_previous_release_are_adopted_in_order(self, agent_ws, agent_ws_redis):
        from tests.factories import seed_agent

        agent = await seed_agent("legacy-agent")
        in_flight, older, newer = _frame("in-flight"), _frame("older"), _frame("newer")
        client = _redis()
        # The pre-namespace shapes: LPUSH = back of the line, in-flight list separate.
        await sync_to_async(client.lpush)(f"{agent.pk}_processing", in_flight.model_dump_json())
        await sync_to_async(client.lpush)(f"{agent.pk}_my_queue", older.model_dump_json())
        await sync_to_async(client.lpush)(f"{agent.pk}_my_queue", newer.model_dump_json())

        session = await open_agent(agent_ws, "legacy-agent")

        received = [(await session.receive(messages.Assign)).args["marker"] for _ in range(3)]
        assert received == ["in-flight", "older", "newer"]

    async def test_a_fenced_connection_returns_the_frame_instead_of_delivering_it(self, agent_ws):
        """The displacement hint rides the channel layer, which drops messages when a process's
        queue is full. Until the old connection's next heartbeat it would keep popping the new
        holder's frames — so ownership is re-checked against the DB before every delivery."""
        from facade.models import Agent

        session = await open_agent(agent_ws, "fenced-agent")
        # Another backend claimed the lease (epoch bump) and its hint never arrived.
        agent = await Agent.objects.aget(pk=session.agent_pk)
        await Agent.objects.filter(pk=agent.pk).aupdate(lease_epoch=agent.lease_epoch + 1)

        frame = _frame("for-the-new-holder")
        await sync_to_async(AgentConsumer.broadcast)(session.agent_pk, frame)

        from facade.codes import AGENT_REPLACED_CODE

        await session.expect_close(AGENT_REPLACED_CODE)  # closed WITHOUT having delivered it
        client = _redis()
        assert await sync_to_async(client.lrange)(queue_key(agent.pk), 0, -1) == [frame.model_dump_json()]
        assert await sync_to_async(client.llen)(processing_key(agent.pk)) == 0

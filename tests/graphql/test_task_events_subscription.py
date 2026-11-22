"""``taskEvents``: one task's events, history then live, and the asset feed a waiting blok listens to.

takt publishes a task's events on ``task_events_{task}`` (its signals contract test proves the
fan-out); here the subscription reads them as a websocket consumer hands them over.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from types import SimpleNamespace

import pytest
from arkitekt_datalayer.models import DatalayerStore
from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer

from facade import channel_events, channels, enums, models
from facade.channels import task_event_channel
from facade.subscriptions.task import TaskEventChange, task_events
from tests.factories import build_task_event, build_task_for_agent_caller, seed_agent
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


def _message(event: models.TaskEvent) -> dict:
    """An event as the channel layer hands it to a subscriber."""
    payload = channel_events.TaskEventCreatedEvent(
        event=channel_events.TaskEventPayload.from_event(event)
    )
    return {
        "type": task_event_channel.message_type,
        "message": payload.model_dump(mode="json"),
    }


class Consumer:
    """The little of a websocket consumer the subscription uses: joining a group and reading its queue."""

    def __init__(
        self, on_join: Callable[["Consumer"], Awaitable[None]] | None = None
    ) -> None:
        self.groups: list[str] = []
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.on_join = on_join

    @contextlib.asynccontextmanager
    async def listen_to_channel(
        self,
        type: str,
        *,
        groups: Sequence[str] = (),
        timeout: float | None = None,
    ) -> AsyncIterator[AsyncIterator[dict]]:
        assert type == task_event_channel.message_type
        self.groups = list(groups)
        if self.on_join:
            await self.on_join(self)

        async def messages() -> AsyncIterator[dict]:
            while (message := await self.queue.get()) is not None:
                yield message

        yield messages()


async def _watch(
    task: models.Task, consumer: Consumer, token: str = "test", after: str | None = None
) -> AsyncGenerator[TaskEventChange, None]:
    context = (await sync_to_async(tenant_context)(token))[0]
    info = SimpleNamespace(
        context=SimpleNamespace(request=context.request, consumer=consumer)
    )
    return task_events(None, info, str(task.pk), after)  # type: ignore[arg-type]


async def _drain(
    feed: AsyncGenerator[TaskEventChange, None], consumer: Consumer
) -> list[TaskEventChange]:
    consumer.queue.put_nowait(None)
    return [message async for message in feed]


class TestTaskEvents:
    async def test_the_history_comes_first_then_each_new_event_once(self) -> None:
        agent = await seed_agent("task-events")
        task = await build_task_for_agent_caller(agent.pk, "te-run")
        started = await build_task_event(task.pk, kind="LOG", message="starting")
        half = await build_task_event(task.pk, kind="PROGRESS", progress=50)

        async def published_meanwhile(consumer: Consumer) -> None:
            # Recorded and published after joining, before the history is read: in both.
            meanwhile = await build_task_event(task.pk, kind="PROGRESS", progress=60)
            consumer.queue.put_nowait(_message(meanwhile))

        feed = await _watch(task, consumer := Consumer(on_join=published_meanwhile))
        history = [await anext(feed) for _ in range(3)]
        assert consumer.groups == [channels.task_events(task.pk)]
        assert [(event.kind, event.progress) for event in history] == [
            (enums.TaskEventKind.LOG, None),
            (enums.TaskEventKind.PROGRESS, 50),
            (enums.TaskEventKind.PROGRESS, 60),
        ]

        done = await build_task_event(task.pk, kind="COMPLETED")
        consumer.queue.put_nowait(_message(done))
        assert [event.id for event in await _drain(feed, consumer)] == [str(done.pk)]
        assert history[0].id == str(started.pk) and history[1].id == str(half.pk)

    async def test_after_skips_what_the_client_already_has(self) -> None:
        agent = await seed_agent("task-events")
        task = await build_task_for_agent_caller(agent.pk, "te-after")
        seen = await build_task_event(task.pk, kind="LOG", message="old")
        newer = await build_task_event(task.pk, kind="YIELD", returns={"n": 1})

        feed = await _watch(task, consumer := Consumer(), after=str(seen.pk))
        assert (await anext(feed)).id == str(newer.pk)
        consumer.queue.put_nowait(_message(seen))  # a late copy of what the client had
        assert await _drain(feed, consumer) == []

    async def test_another_organizations_task_is_not_watched(self) -> None:
        agent = await seed_agent("task-events")
        task = await build_task_for_agent_caller(agent.pk, "te-theirs")

        feed = await _watch(task, Consumer(), token=OTHER_TOKEN)
        with pytest.raises(PermissionError):
            await anext(feed)


class TestAssetFeed:
    async def test_a_stored_asset_is_published_to_its_organization_once(self) -> None:
        mine = (await sync_to_async(tenant_context)("test"))[3]
        theirs = (await sync_to_async(tenant_context)(OTHER_TOKEN))[3]
        layer = get_channel_layer()
        ours, others = await layer.new_channel(), await layer.new_channel()
        await layer.group_add(channels.assets_org(mine.pk), ours)
        await layer.group_add(channels.assets_org(theirs.pk), others)

        def store() -> models.Asset:
            file = DatalayerStore.objects.create(
                organization=mine, key="assets/model.glb", bucket="media"
            )
            asset = models.Asset.objects.create(
                organization=mine,
                ref="sha256:" + "ab" * 32,
                kind="MODEL_3D",
                file=file,
                size=12,
            )
            asset.meta = {"nodes": 1}
            asset.save()  # a later save is no news
            return asset

        asset = await sync_to_async(store)()

        message = (await asyncio.wait_for(layer.receive(ours), timeout=2))["message"]
        assert message == {"create": asset.pk, "ref": asset.ref, "kind": "MODEL_3D"}
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(layer.receive(ours), timeout=0.3)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(layer.receive(others), timeout=0.3)

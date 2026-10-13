"""``latestTask``: the task an agent's action last ran as, as a snapshot and then what changes it."""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from facade import channel_events, enums, models
from facade.channels import implementation_task_events, task_event_channel
from facade.subscriptions.task import LatestTaskSnapshot, TaskChange, TaskEventChange, latest_task, read_latest_task, relay_latest_task
from tests.factories import build_implementation_for_agent, build_task_event, seed_agent
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context


def _run(implementation: models.Implementation, **fields: object) -> models.Task:
    """A task of the implementation that nobody in particular called."""
    defaults = {"latest_event_kind": enums.TaskEventKind.STARTED, "latest_instruct_kind": enums.TaskInstructChoices.ASSIGN}
    return models.Task.objects.create(action=implementation.action, agent=implementation.agent, implementation=implementation, **{**defaults, **fields})


run = sync_to_async(_run)


def _message(*, create: models.Task | None = None, event: models.TaskEvent | None = None) -> dict:
    """A message as the channel layer hands it to a subscriber."""
    payload = channel_events.TaskEventCreatedEvent(
        create=channel_events.TaskChangePayload.from_task(create) if create else None,
        event=channel_events.TaskEventPayload.from_event(event) if event else None,
    )
    return {"type": task_event_channel.message_type, "message": payload.model_dump(mode="json")}


class Consumer:
    """The little of a websocket consumer the subscription uses: joining a group and reading its queue."""

    def __init__(self, on_join=None) -> None:
        self.groups: list[str] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.on_join = on_join

    @contextlib.asynccontextmanager
    async def listen_to_channel(self, type: str, *, groups=(), timeout=None):
        assert type == task_event_channel.message_type
        self.groups = list(groups)
        if self.on_join:
            await self.on_join(self)

        async def messages():
            while (message := await self.queue.get()) is not None:
                yield message

        yield messages()


async def _watch(agent: models.Agent, interface: str, consumer: Consumer, token: str = "test"):
    context = (await sync_to_async(tenant_context)(token))[0]
    info = SimpleNamespace(context=SimpleNamespace(request=context.request, consumer=consumer))
    return latest_task(None, info, str(agent.pk), interface)  # type: ignore[arg-type]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestLatestTask:
    async def test_the_snapshot_is_the_latest_run_with_what_its_record_is_made_of(self) -> None:
        agent = await seed_agent("latest")
        implementation = await build_implementation_for_agent(agent.pk, "blur")
        earlier = await run(implementation, is_done=True, latest_event_kind=enums.TaskEventKind.COMPLETED)
        await build_task_event(earlier.pk, kind="COMPLETED")
        running = await run(implementation)
        await build_task_event(running.pk, kind="LOG", message="noise")
        await build_task_event(running.pk, kind="PROGRESS", progress=20, message="starting")
        await build_task_event(running.pk, kind="YIELD", returns={"count": 3})
        last = await build_task_event(running.pk, kind="PROGRESS", progress=60, message="6 of 10")

        snapshot, seen = await sync_to_async(read_latest_task)(implementation.pk)

        # A run nobody in particular started, still going: not the viewer's own, not only done ones.
        assert snapshot.latest is not None and snapshot.latest.id == str(running.pk) and not snapshot.latest.is_done
        assert [(event.kind.value, event.progress, event.message, event.returns) for event in snapshot.events] == [
            ("YIELD", None, None, {"count": 3}),
            ("PROGRESS", 60, "6 of 10", None),
        ]
        assert seen == last.pk

    async def test_an_action_that_never_ran_has_no_task_and_a_name_that_is_no_action_ends_the_feed(self) -> None:
        agent = await seed_agent("latest")
        implementation = await build_implementation_for_agent(agent.pk, "idle")

        feed = await _watch(agent, "idle-iface", consumer := Consumer())
        first = await anext(feed)
        assert isinstance(first, LatestTaskSnapshot) and first.implementation == str(implementation.pk)
        assert first.latest is None and first.events == []
        assert consumer.groups == [implementation_task_events(implementation.pk)]
        await feed.aclose()

        absent = await _watch(agent, "a-state-not-an-action", Consumer())
        assert [message async for message in absent] == [LatestTaskSnapshot(implementation=None, latest=None, events=[])]

    async def test_another_organizations_agent_is_not_watched(self) -> None:
        agent = await seed_agent("latest")
        await build_implementation_for_agent(agent.pk, "blur")

        feed = await _watch(agent, "blur-iface", Consumer(), token=OTHER_TOKEN)
        with pytest.raises(PermissionError):
            await anext(feed)

    async def test_what_is_published_while_the_snapshot_is_read_arrives_once(self) -> None:
        agent = await seed_agent("latest")
        implementation = await build_implementation_for_agent(agent.pk, "blur")
        running = await run(implementation)

        async def published_meanwhile(consumer: Consumer) -> None:
            # Committed, and published to the group, after joining and before the snapshot is read.
            event = await build_task_event(running.pk, kind="PROGRESS", progress=40)
            consumer.queue.put_nowait(_message(event=event))

        feed = await _watch(agent, "blur-iface", consumer := Consumer(on_join=published_meanwhile))
        snapshot = await anext(feed)
        assert [event.progress for event in snapshot.events] == [40]

        later = await build_task_event(running.pk, kind="PROGRESS", progress=70)
        consumer.queue.put_nowait(_message(event=later))
        consumer.queue.put_nowait(None)
        assert [(type(message), message.progress) for message in [m async for m in feed]] == [(TaskEventChange, 70)]

    async def test_a_newer_run_and_its_reports_are_relayed_and_an_older_ones_are_not(self) -> None:
        agent = await seed_agent("latest")
        implementation = await build_implementation_for_agent(agent.pk, "blur")
        stale = await run(implementation, is_done=True, latest_event_kind=enums.TaskEventKind.COMPLETED)
        held = await run(implementation)
        seen = (await build_task_event(held.pk, kind="PROGRESS", progress=10)).pk

        def relay(**message: object):
            parsed = task_event_channel.model.model_validate(_message(**message)["message"])  # type: ignore[arg-type]
            return relay_latest_task(parsed, task=held.pk, seen=seen)

        newer = await run(implementation)
        created = await sync_to_async(relay)(create=newer)
        assert isinstance(created, TaskChange) and created.id == str(newer.pk) and not created.is_done

        progress = await build_task_event(newer.pk, kind="PROGRESS", progress=5)
        assert (await sync_to_async(relay)(event=progress)).task == str(newer.pk)  # type: ignore[union-attr]

        # Said again when it ends: the second message for the end of a task.
        held.is_done, held.latest_event_kind = True, enums.TaskEventKind.FAILED
        ended = await sync_to_async(relay)(create=held)
        assert isinstance(ended, TaskChange) and ended.is_done and ended.latest_event_kind == enums.TaskEventKind.FAILED

        late = await build_task_event(stale.pk, kind="PROGRESS", progress=99)
        assert await sync_to_async(relay)(event=late) is None
        assert await sync_to_async(relay)(create=stale) is None

"""A guest of the organization, granted a collaboration and nothing else: what they read of rekuest,
and that they do nothing.

rekuest shares tasks (``rekuest.service``). A shared task brings the action it ran (its ports
come with it, never its other tasks, implementations, schedules or triggers), the tasks it caused
as they are now, and the bloks it made to ask or inform with; its events are part of it, live.
Agents, callers and everything that runs things stay the organization's: a guest reads a shared
task's ``agent`` and ``caller`` as null.

A guest's token names their collaborations by key (``collabs``, lok's), minted per test. A full
member ("test") puts tasks in under a fresh key (``addManyToCollaboration``). Outside GraphQL
nothing of rekuest's Django side takes a user's token (the agent endpoint and the caller session
are takt's, which refuses any token carrying ``collabs``:
``takt/crates/authentikate/src/utils.rs``), and over GraphQL a guest's subscriptions and
mutations are refused.
"""

import uuid

import pytest
from asgiref.sync import sync_to_async
from strawberry.types import ExecutionResult

from facade import models
from facade.schema import schema
from tests.conftest import GuestFactory
from tests.sharing import (
    Workflow,
    data_of,
    ids,
    organization,
    reason_of,
    run,
    share,
    token_context,
    workflow,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]

RENDER = """
query ($id: ID!) {
  task(id: $id) {
    id
    action { id name description }
    events { message }
    children { id }
    agent { id }
    caller { id }
    bloks { role }
  }
}
"""
TASKS = "{ tasks { id } }"
ACTIONS = "{ actions { id tasks { id } } }"
ORGANIZATION = (
    "{ agents { id } implementations { id } schedules { id } triggers { id } }"
)
REMOVE = "mutation ($input: CollaborationObjectInput!) { removeFromCollaboration(input: $input) { key } }"
ADD = "mutation ($input: CollaborationObjectInput!) { addToCollaboration(input: $input) { key items { identifier objectId brought } } }"

built = sync_to_async(workflow)


def _rendered(result: ExecutionResult) -> dict[str, object]:
    task = data_of(result)["task"]
    assert isinstance(task, dict)
    return task


async def test_a_guest_renders_a_shared_task_with_its_action_events_and_children(
    guest: GuestFactory,
) -> None:
    flow: Workflow = await built("render")
    ctx = guest({await share(flow.root): []})

    root = _rendered(await run(ctx, RENDER, id=str(flow.root.pk)))
    assert root["action"] == {
        "id": str(flow.action.pk),
        "name": flow.action.name,
        "description": flow.action.description,
    }
    assert root["events"] == [{"message": "started"}]
    assert ids(root["children"]) == ids([{"id": flow.first.pk}, {"id": flow.second.pk}])
    # Who ran it, and for whom, are the organization's.
    assert (root["agent"], root["caller"]) == (None, None)

    # What it caused came along, as it was: a grandchild with its own events and action.
    deep = _rendered(await run(ctx, RENDER, id=str(flow.grandchild.pk)))
    assert deep["events"] == [{"message": "deep"}]
    assert deep["action"]["id"] == str(flow.child_action.pk)  # type: ignore[index]

    assert ids(data_of(await run(ctx, TASKS))["tasks"]) == ids(
        [{"id": task.pk} for task in flow.tree]
    )
    assert (await run(ctx, RENDER, id=str(flow.unrelated.pk))).errors


async def test_an_event_recorded_after_sharing_is_in_with_its_task(
    guest: GuestFactory,
) -> None:
    flow = await built("live")
    ctx = guest({await share(flow.root): []})
    await models.TaskEvent.objects.acreate(task=flow.root, kind="LOG", message="later")

    root = _rendered(await run(ctx, RENDER, id=str(flow.root.pk)))
    assert sorted(event["message"] for event in root["events"]) == [  # type: ignore[union-attr, index]
        "later",
        "started",
    ]


async def test_the_action_brought_along_shows_itself_not_its_other_runs(
    guest: GuestFactory,
) -> None:
    flow = await built("action")

    @sync_to_async
    def run_it_otherwise() -> None:
        """What hangs off the action without being its definition."""
        caller = models.Caller.objects.create(
            client=flow.agent.client,
            user=flow.agent.user,
            organization=flow.agent.organization,
        )
        models.Schedule.objects.create(
            name="hourly",
            caller=caller,
            action=flow.action,
            agent=flow.agent,
            cron="0 * * * *",
            enabled=False,
        )
        models.Implementation.objects.create(
            interface="action-iface", action=flow.action, agent=flow.agent
        )

    await run_it_otherwise()
    ctx = guest({await share(flow.root): []})

    actions = data_of(await run(ctx, ACTIONS))["actions"]
    assert isinstance(actions, list)
    by_id = {row["id"]: row for row in actions}
    assert sorted(by_id) == sorted([str(flow.action.pk), str(flow.child_action.pk)])
    # The unrelated root runs the same action, and is not shown through it.
    assert ids(by_id[str(flow.action.pk)]["tasks"]) == [str(flow.root.pk)]

    assert data_of(await run(ctx, ORGANIZATION)) == {
        "agents": [],
        "implementations": [],
        "schedules": [],
        "triggers": [],
    }


async def test_a_task_shares_the_bloks_it_made_not_those_of_its_implementation(
    guest: GuestFactory,
) -> None:
    flow = await built("bloks")

    @sync_to_async
    def show() -> None:
        org = flow.agent.organization
        module = models.Module.objects.get_or_create(
            name="orkestrator", organization=org
        )[0]
        for name, owner in (("made", flow.root), ("declared", None)):
            blok = models.Blok.objects.create(
                name=f"{name}-{flow.root.pk}",
                organization=org,
                creator=flow.agent.user,
                module=module,
                owner_task=owner,
            )
            models.MaterializedBlok.objects.create(
                blok=blok,
                name=blok.name,
                description="",
                scope="TASK",
                owner_task=flow.root,
                task_role="INFORM",
            )

    await show()
    member = _rendered(await run(token_context("test"), RENDER, id=str(flow.root.pk)))
    assert len(member["bloks"]) == 2  # type: ignore[arg-type]
    ctx = guest({await share(flow.root): []})

    shared = _rendered(await run(ctx, RENDER, id=str(flow.root.pk)))
    assert shared["bloks"] == [{"role": "INFORM"}]


async def test_a_key_nothing_was_put_in_holds_nothing(guest: GuestFactory) -> None:
    flow = await built("nothing")
    await share(flow.root)
    ctx = guest({str(uuid.uuid4()): []})

    assert data_of(await run(ctx, TASKS))["tasks"] == []
    assert (await run(ctx, RENDER, id=str(flow.root.pk))).errors


async def test_a_guest_writes_nothing(guest: GuestFactory) -> None:
    flow = await built("writes")
    key = await share(flow.root)
    ctx = guest({key: ["rekuest_editor"]})

    for mutation, variables in (
        (
            "mutation ($input: CancelInput!) { cancel(input: $input) { id } }",
            {"input": {"task": str(flow.root.pk)}},
        ),
        (
            "mutation ($input: InterruptInput!) { interrupt(input: $input) { id } }",
            {"input": {"task": str(flow.root.pk)}},
        ),
        (
            ADD,
            {
                "input": {
                    "collaboration": key,
                    "identifier": "@rekuest/task",
                    "object": str(flow.unrelated.pk),
                }
            },
        ),
        (
            "mutation ($input: ShareWorkflowInput!) { shareWorkflow(input: $input) { dryRun } }",
            {"input": {"collaboration": key, "rootTask": str(flow.unrelated.pk)}},
        ),
        ("mutation ($key: ID!) { deleteCollaboration(key: $key) }", {"key": key}),
    ):
        refused = await run(ctx, mutation, **variables)
        assert reason_of(refused) == "SCOPED_READ_ONLY", (mutation, refused.errors)

    assert not await models.Task.objects.filter(pk=flow.root.pk, is_done=True).aexists()


async def test_a_guest_subscribes_to_nothing(guest: GuestFactory) -> None:
    flow = await built("subscribe")
    ctx = guest({await share(flow.root): []})

    subscribed = await schema.subscribe(
        "subscription ($task: ID!) { taskEvents(task: $task) { __typename } }",
        context_value=ctx,
        variable_values={"task": str(flow.root.pk)},
    )
    if not isinstance(subscribed, ExecutionResult):
        # strawberry streams even a refusal: its first (and only) result.
        subscribed = await anext(subscribed)
    assert isinstance(subscribed, ExecutionResult)
    assert reason_of(subscribed) == "SCOPED_READ_ONLY"


async def test_a_guest_enters_only_where_objects_are_handed_out(
    guest: GuestFactory,
) -> None:
    flow = await built("roots")
    ctx = guest({await share(flow.root): []})

    assert reason_of(await run(ctx, "{ taskStats { __typename } }")) == (
        "COLLABORATION_DOES_NOT_REACH"
    )
    stats = data_of(await run(ctx, "{ collaborationsStats { count itemCount } }"))
    assert stats["collaborationsStats"] == {
        "count": 1,
        # The tree, and the two actions its tasks ran.
        "itemCount": len(flow.tree) + 2,
    }


async def test_only_a_task_of_the_organization_is_put_in(guest: GuestFactory) -> None:
    flow = await built("theirs", await sync_to_async(organization)("test-other"))
    mine = await built("mine")
    key = str(uuid.uuid4())

    for identifier, target in (
        ("@rekuest/task", flow.root.pk),
        ("@rekuest/agent", mine.agent.pk),
    ):
        refused = await run(
            token_context("test"),
            ADD,
            input={
                "collaboration": key,
                "identifier": identifier,
                "object": str(target),
            },
        )
        assert refused.errors, identifier

    added = await run(
        token_context("test"),
        ADD,
        input={
            "collaboration": key,
            "identifier": "@rekuest/task",
            "object": str(mine.first.pk),
        },
    )
    items = data_of(added)["addToCollaboration"]["items"]  # type: ignore[index]
    assert {
        (item["identifier"], item["objectId"], item["brought"]) for item in items
    } == {
        ("@rekuest/task", str(mine.first.pk), False),
        ("@rekuest/task", str(mine.grandchild.pk), True),
        (None, str(mine.child_action.pk), True),
    }


async def test_taking_the_root_out_takes_what_it_brought(guest: GuestFactory) -> None:
    flow = await built("remove")
    key = await share(flow.root)
    ctx = guest({key: []})
    assert len(data_of(await run(ctx, TASKS))["tasks"]) == len(flow.tree)  # type: ignore[arg-type]

    removed = await run(
        token_context("test"),
        REMOVE,
        input={
            "collaboration": key,
            "identifier": "@rekuest/task",
            "object": str(flow.root.pk),
        },
    )
    assert data_of(removed)["removeFromCollaboration"] == {"key": key}
    assert data_of(await run(ctx, TASKS))["tasks"] == []

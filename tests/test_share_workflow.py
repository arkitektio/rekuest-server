"""Sharing a workflow: a root task someone started, with every task it caused, put in a collaboration
as it is now.

In rekuest the workflow's record is its task tree: the root (``root`` null) and every task whose
``root`` is it. Its id is what a provenance token calls the workflow (``rtk``) in every service,
so ``createdUnder: <root id>`` here finds the same workflow as anywhere else. Only a root of the
request's organization is taken; what the workflow causes later is not in the collaboration.
"""

import uuid

import pytest
from arkitekt_service.collaborations.models import Collaboration, CollaborationItem
from asgiref.sync import sync_to_async

from facade import models
from tests.conftest import GuestFactory
from tests.sharing import (
    Workflow,
    agent,
    data_of,
    ids,
    organization,
    run,
    task,
    token_context,
    workflow,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]

SHARE = """
mutation ($input: ShareWorkflowInput!) {
  shareWorkflow(input: $input) {
    collaboration
    dryRun
    added { identifier object }
    covered
    skipped { identifier object }
    unknown
  }
}
"""
TASKS = "query ($filters: TaskFilter) { tasks(filters: $filters) { id } }"

built = sync_to_async(workflow)


def _tree_ids(flow: Workflow) -> list[str]:
    return ids([{"id": task.pk} for task in flow.tree])


async def _share(
    flow: Workflow, key: str, *, dry_run: bool = False
) -> dict[str, object]:
    shared = await run(
        token_context("test"),
        SHARE,
        input={"collaboration": key, "rootTask": str(flow.root.pk), "dryRun": dry_run},
    )
    found = data_of(shared)["shareWorkflow"]
    assert isinstance(found, dict)
    return found


async def test_a_dry_run_says_what_the_share_puts_in_and_writes_nothing() -> None:
    flow = await built("dry")
    key = str(uuid.uuid4())

    report = await _share(flow, key, dry_run=True)

    assert report["dryRun"] is True
    assert ids(
        [{"id": added["object"]} for added in report["added"]]  # type: ignore[union-attr, index]
    ) == _tree_ids(flow)
    assert {added["identifier"] for added in report["added"]} == {"@rekuest/task"}  # type: ignore[union-attr, index]
    # Tasks are all rekuest shares, and each says which workflow it is of.
    assert (report["covered"], report["skipped"], report["unknown"]) == (0, [], [])
    assert not await Collaboration.objects.filter(key=key).aexists()


async def test_the_share_puts_in_exactly_the_tree(guest: GuestFactory) -> None:
    flow = await built("tree")
    other = await built("other")
    key = str(uuid.uuid4())

    report = await _share(flow, key)

    assert report["dryRun"] is False
    assert ids(
        [{"id": added["object"]} for added in report["added"]]  # type: ignore[union-attr, index]
    ) == _tree_ids(flow)
    by_hand = [
        object_id
        async for object_id in CollaborationItem.objects.filter(
            collaboration__key=key, because_of__isnull=True
        ).values_list("object_id", flat=True)
    ]
    assert sorted(by_hand) == _tree_ids(flow)

    ctx = guest({key: []})
    assert ids(data_of(await run(ctx, TASKS))["tasks"]) == _tree_ids(flow)
    for outside in (flow.unrelated, other.root):
        assert (
            await run(
                ctx, "query ($id: ID!) { task(id: $id) { id } }", id=str(outside.pk)
            )
        ).errors

    # Shared as it was: a task the workflow causes later is not in.
    later = await sync_to_async(task)(flow.child_action, flow.agent, parent=flow.second)
    assert str(later.pk) not in ids(data_of(await run(ctx, TASKS))["tasks"])


async def test_lists_filter_by_workflow_and_by_collaboration() -> None:
    flow = await built("filters")
    key = str(uuid.uuid4())
    member = token_context("test")

    created = await run(member, TASKS, filters={"createdUnder": str(flow.root.pk)})
    assert ids(data_of(created)["tasks"]) == _tree_ids(flow)
    assert (
        data_of(await run(member, TASKS, filters={"createdUnder": "not-a-task"}))[
            "tasks"
        ]
        == []
    )

    assert (
        data_of(await run(member, TASKS, filters={"inCollaboration": key}))["tasks"]
        == []
    )
    await _share(flow, key)
    held = await run(member, TASKS, filters={"inCollaboration": key})
    assert ids(data_of(held)["tasks"]) == _tree_ids(flow)


async def test_human_induced_keeps_the_workflows_someone_started() -> None:
    flow = await built("human")

    @sync_to_async
    def fired_and_planned() -> list[models.Task]:
        org = organization()
        runner = agent(org, "human-auto")
        signal = models.Signal.objects.create(
            service="mikro",
            signal_id=str(uuid.uuid4()),
            kind="CREATED",
            identifier="@mikro/image",
            object="1",
            organization=org,
        )
        return [
            # Fired by a trigger since deleted: its depth still says so.
            task(flow.action, runner, trigger_depth=1),
            task(flow.action, runner, signal=signal, trigger_depth=1),
        ]

    automatic = await fired_and_planned()
    member = token_context("test")
    started = data_of(await run(member, TASKS, filters={"humanInduced": True}))
    assert ids(started["tasks"]) == ids(
        [{"id": flow.root.pk}, {"id": flow.unrelated.pk}]
    )
    rest = ids(
        data_of(await run(member, TASKS, filters={"humanInduced": False}))["tasks"]
    )
    assert rest == ids(
        [{"id": t.pk} for t in (flow.first, flow.second, flow.grandchild, *automatic)]
    )


async def test_only_a_root_of_the_organization_is_shared() -> None:
    flow = await built("refused")
    theirs = await built(
        "refused-theirs", await sync_to_async(organization)("test-other")
    )
    key = str(uuid.uuid4())

    for root in (flow.first, theirs.root):
        refused = await run(
            token_context("test"),
            SHARE,
            input={"collaboration": key, "rootTask": str(root.pk)},
        )
        assert refused.errors, root
    assert not await CollaborationItem.objects.filter(collaboration__key=key).aexists()

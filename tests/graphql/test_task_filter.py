"""GraphQL ``tasks`` filtering and ordering.

Every surviving ``TaskFilter`` key and every ``TaskOrder`` key is executed against the real schema.

This suite exists because ``strawberry_django.order_type`` does NOT validate its annotations
against the model, and ``@filter_field`` bodies only run when the client actually sends that key.
The removed ``status`` filter and the ``status``/``startedAt`` ordering keys named columns that do
not exist on ``facade.models.Task``, so the SDL built cleanly and ``tests/test_print_schema.py``
passed — the ``FieldError`` only ever surfaced on a real query. Hence: every test here does a real
``schema.execute`` and asserts ``not result.errors``.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade import enums, models
from facade.schema import schema
from tests.factories import (
    build_task_event,
    build_task_for_agent_caller,
    build_unimplemented_task_for_agent,
    seed_agent,
)

TASKS = """
    query Tasks($filters: TaskFilter!, $ordering: [TaskOrder!]!) {
        tasks(filters: $filters, ordering: $ordering) {
            id
            reference
            isDone
            argsHash
            latestEventKind
            createdAt
            finishedAt
            implementation { id }
            agent { id }
            caller { id }
            root { id }
            parent { id }
        }
    }
"""

# No variables at all. ``build_prescoped_queryset`` does
# ``info.variable_values.get("filters", {}).get("scope")``, so an explicit ``filters: null``
# variable would call ``.get`` on None and raise — this suite never passes a null $filters.
TASKS_PLAIN = "query { tasks { id implementation { id } } }"


@dataclass(frozen=True)
class Seeded:
    """An agent, its two-level task tree and its client's id."""

    agent: models.Agent
    root: models.Task
    child: models.Task
    client_id: str


async def _seed(prefix: str) -> Seeded:
    """An agent plus a two-level task tree, all owned by the token-"test" identity.

    ``seed_agent`` derives (client, user, organization) through the same authentikate expansion
    the auth extension uses during ``schema.execute``, so the seeded agent's organization is
    exactly the one the ``agent__organization`` prescope restricts to.
    """
    agent = await seed_agent(prefix, token="test")
    root = await build_task_for_agent_caller(agent.pk, f"{prefix}-root")
    child = await build_task_for_agent_caller(
        agent.pk, f"{prefix}-child", parent=root, root=root
    )
    client_id = await sync_to_async(lambda: agent.client.client_id)()
    return Seeded(agent=agent, root=root, child=child, client_id=client_id)


# One entry per TaskFilter key. The point is coverage of the ORM path, not the result set:
# a key naming a column that does not exist raises FieldError right here.
FILTER_CASES: dict[str, Callable[[Seeded], dict[str, object]]] = {
    "ids": lambda s: {"ids": [str(s.root.pk)]},
    "clientId": lambda s: {"clientId": s.client_id},
    "state": lambda s: {"state": ["STARTED", "COMPLETED"]},
    "implementation": lambda s: {"implementation": str(s.root.implementation_id)},
    "action": lambda s: {"action": str(s.root.action_id)},
    "agent": lambda s: {"agent": str(s.agent.pk)},
    "caller": lambda s: {"caller": str(s.root.caller_id)},
    "parent": lambda s: {"parent": str(s.root.pk)},
    "root": lambda s: {"root": str(s.root.pk)},
    "rootIsnull": lambda s: {"rootIsnull": True},
    "isDone": lambda s: {"isDone": False},
    "actedOn": lambda s: {"actedOn": ["@mikro/image:1"]},
    "argsHash": lambda s: {"argsHash": "0" * 64},
    "reference": lambda s: {"reference": str(s.root.reference)},
    "createdBefore": lambda s: {"createdBefore": "2999-01-01T00:00:00+00:00"},
    "createdAfter": lambda s: {"createdAfter": "2000-01-01T00:00:00+00:00"},
}

# TaskOrder is @oneOf: every list entry must carry EXACTLY ONE key. Two keys in one object is a
# GraphQL validation error rather than a FieldError — a green-looking failure mode.
ORDERING_CASES = {
    "createdAt": [{"createdAt": "DESC"}],
    "finishedAt": [{"finishedAt": "ASC_NULLS_LAST"}],
    "both": [{"createdAt": "DESC"}, {"finishedAt": "ASC_NULLS_LAST"}],
}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestTaskFilter:
    @pytest.mark.parametrize("key", sorted(FILTER_CASES))
    async def test_every_filter_key_executes(
        self, authenticated_context: HttpContext, key: str
    ) -> None:
        # Distinct prefix per case: the factory uses App.objects.create, not get_or_create.
        seeded = await _seed(f"tf-{key.lower()}")

        result = await schema.execute(
            TASKS,
            context_value=authenticated_context,
            variable_values={"filters": FILTER_CASES[key](seeded), "ordering": []},
        )

        assert not result.errors, result.errors

    @pytest.mark.parametrize("key", sorted(ORDERING_CASES))
    async def test_every_ordering_key_executes(
        self, authenticated_context: HttpContext, key: str
    ) -> None:
        await _seed(f"to-{key.lower()}")

        result = await schema.execute(
            TASKS,
            context_value=authenticated_context,
            variable_values={"filters": {}, "ordering": ORDERING_CASES[key]},
        )

        assert not result.errors, result.errors

    async def test_filters_actually_narrow(
        self, authenticated_context: HttpContext
    ) -> None:
        """Sanity anchor: ``ids`` returns exactly the one task, so the pipeline is wired rather
        than merely error-free."""
        seeded = await _seed("tf-narrow")

        result = await schema.execute(
            TASKS,
            context_value=authenticated_context,
            variable_values={"filters": {"ids": [str(seeded.root.pk)]}, "ordering": []},
        )

        assert not result.errors, result.errors
        assert [t["id"] for t in result.data["tasks"]] == [str(seeded.root.pk)]

    async def test_root_isnull_separates_roots_from_children(
        self, authenticated_context: HttpContext
    ) -> None:
        seeded = await _seed("tf-rootnull")

        result = await schema.execute(
            TASKS,
            context_value=authenticated_context,
            variable_values={"filters": {"rootIsnull": True}, "ordering": []},
        )

        assert not result.errors, result.errors
        ids = [t["id"] for t in result.data["tasks"]]
        assert str(seeded.root.pk) in ids
        assert str(seeded.child.pk) not in ids

    async def test_nested_event_filter_executes(
        self, authenticated_context: HttpContext
    ) -> None:
        """``Task.events`` is a real strawberry_django field, so it exposes ``TaskEventFilter``
        as a nested argument — unlike ``Task.instructs``, which is a hand-written resolver and
        generates no ``filters`` arg. Both keys of the reachable filter are executed here."""
        seeded = await _seed("tf-events")
        await build_task_event(seeded.root.pk, kind=enums.TaskEventChoices.COMPLETED)

        result = await schema.execute(
            """
            query TaskEvents($filters: TaskEventFilter!) {
                tasks { id events(filters: $filters) { id kind } }
            }
            """,
            context_value=authenticated_context,
            variable_values={"filters": {"kind": ["COMPLETED"]}},
        )

        assert not result.errors, result.errors
        kinds = [e["kind"] for t in result.data["tasks"] for e in t["events"]]
        assert kinds and set(kinds) == {"COMPLETED"}

    async def test_unimplemented_task_is_returned(
        self, authenticated_context: HttpContext
    ) -> None:
        """Regression: a QUEUED/BOUND task has ``implementation = NULL``.

        The old prescope joined ``implementation__action__organization``; because that FK is
        nullable the INNER JOIN silently dropped every such row. The prescope now goes through the
        non-null ``agent__organization``, so they are visible — and selecting ``implementation`` on
        them must not null out the whole list either (``Task.implementation`` has to be Optional).
        """
        agent = await seed_agent("tf-noimpl", token="test")
        orphan = await build_unimplemented_task_for_agent(agent.pk, "tf-noimpl")

        result = await schema.execute(TASKS_PLAIN, context_value=authenticated_context)

        assert not result.errors, result.errors
        returned = {t["id"]: t for t in result.data["tasks"]}
        assert str(orphan.pk) in returned
        assert returned[str(orphan.pk)]["implementation"] is None

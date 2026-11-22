"""The ``myTasks`` query — the caller-scoped, root-only counterpart of the ``mytasks`` subscription."""

import pytest
from kante.context import HttpContext

from facade.schema import schema
from tests.factories import build_task_for_agent_caller, seed_agent

MY_TASKS = "query { myTasks { id root { id } } }"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestMyTasksQuery:
    async def test_returns_caller_root_tasks_only(
        self, authenticated_context: HttpContext
    ) -> None:
        # Everything keys off the token "test" identity, which is also what the request resolves to.
        agent = await seed_agent("myq", token="test")
        root = await build_task_for_agent_caller(agent.pk, "myq-root")
        child = await build_task_for_agent_caller(
            agent.pk, "myq-child", parent=root, root=root
        )

        result = await schema.execute(MY_TASKS, context_value=authenticated_context)

        assert not result.errors, result.errors
        ids = [t["id"] for t in result.data["myTasks"]]
        assert str(root.id) in ids  # the root assignation is mine
        assert str(child.id) not in ids  # the child is not a root → excluded

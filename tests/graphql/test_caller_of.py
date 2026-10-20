"""``callerOf``: the agents that called a task, nearest first, and what they offer.

An app that needs something it cannot make (a person's approval, a window opened) finds it
among its callers with an ordinary query and calls it like any other child. The tree here:
a person starts ``root`` from the UI app; ``root``'s agent calls ``child``; ``child``'s agent
calls ``grandchild``. Asked from ``grandchild``, the callers are ``child``'s agent, ``root``'s
agent, then the UI's.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Client, Device, Organization, Release
from django.utils import timezone
from kante.context import HttpContext

from facade import enums, models
from facade.schema import schema
from rekuest_core.inputs.models import ImplementationInputModel
from tests.registered import create_implementation

AGENTS = "query Agents($filters: AgentFilter) { agents(filters: $filters) { id } }"
IMPLEMENTATIONS = """
    query Implementations($filters: ImplementationFilter) {
        implementations(filters: $filters) { id interface agent { id } }
    }
"""


def _agent(context: HttpContext, name: str, organization: Organization | None = None) -> models.Agent:
    """A live agent of the request's user, on a client of its own, offering ``work``."""
    request = context.request
    device = Device.objects.create(device_id=f"{name}-device")
    client = Client.objects.create(client_id=f"{name}-client", device=device)
    app = App.objects.create(identifier=f"{name}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    agent = models.Agent.objects.create(
        app=app,
        release=release,
        user=request.user,
        client=client,
        organization=organization or request.organization,
        hash=f"{name}-hash",
        name=name,
        connected=True,
        last_seen=timezone.now(),
    )
    create_implementation(
        ImplementationInputModel.model_validate({"interface": "work", "definition": {"key": "work", "version": "1", "name": f"{name} work", "kind": "FUNCTION", "args": [], "returns": []}}),
        agent,
    )
    return agent


def _task(agent: models.Agent, caller_agent: models.Agent, parent: models.Task | None = None) -> models.Task:
    """A task ``agent`` runs, asked for by ``caller_agent``'s identity (its client, user and org)."""
    caller, _ = models.Caller.objects.get_or_create(client=caller_agent.client, user=caller_agent.user, organization=caller_agent.organization)
    implementation = models.Implementation.objects.get(agent=agent, interface="work")
    return models.Task.objects.create(
        caller=caller,
        action=implementation.action,
        agent=agent,
        implementation=implementation,
        parent=parent,
        root=(parent.root or parent) if parent else None,
        latest_event_kind=enums.TaskEventKind.STARTED,
        latest_instruct_kind=enums.TaskInstructChoices.ASSIGN,
    )


def _tree(context: HttpContext) -> dict[str, models.Agent | models.Task]:
    ui, first, second, third = (_agent(context, name) for name in ("caller-ui", "caller-root", "caller-child", "caller-grandchild"))
    root = _task(first, caller_agent=ui)
    child = _task(second, caller_agent=first, parent=root)
    grandchild = _task(third, caller_agent=second, parent=child)
    return {"ui": ui, "first": first, "second": second, "third": third, "root": root, "child": child, "grandchild": grandchild}


async def _data(context: HttpContext, query: str, filters: dict) -> dict:
    result = await schema.execute(query, context_value=context, variable_values={"filters": filters})
    assert result.errors is None, result.errors
    return result.data


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestCallerOf:
    async def test_the_callers_are_the_ancestors_agents_then_the_ui_nearest_first(self, authenticated_context: HttpContext):
        tree = await sync_to_async(_tree)(authenticated_context)

        data = await _data(authenticated_context, AGENTS, {"callerOf": str(tree["grandchild"].id)})

        assert [a["id"] for a in data["agents"]] == [str(tree[k].id) for k in ("second", "first", "ui")]

    async def test_a_root_s_caller_is_the_app_it_was_started_from(self, authenticated_context: HttpContext):
        tree = await sync_to_async(_tree)(authenticated_context)

        data = await _data(authenticated_context, AGENTS, {"callerOf": str(tree["root"].id)})

        assert [a["id"] for a in data["agents"]] == [str(tree["ui"].id)]

    async def test_implementations_of_the_callers_come_nearest_first_and_narrow_with_the_other_filters(self, authenticated_context: HttpContext):
        tree = await sync_to_async(_tree)(authenticated_context)

        data = await _data(authenticated_context, IMPLEMENTATIONS, {"agent": {"callerOf": str(tree["child"].id)}, "interface": {"exact": "work"}, "active": True})

        assert [i["agent"]["id"] for i in data["implementations"]] == [str(tree["first"].id), str(tree["ui"].id)]

    async def test_the_task_s_own_agent_is_never_its_caller(self, authenticated_context: HttpContext):
        def calls_itself() -> tuple[models.Agent, models.Task]:
            agent = _agent(authenticated_context, "caller-self")
            root = _task(agent, caller_agent=agent)
            return agent, _task(agent, caller_agent=agent, parent=root)

        _, child = await sync_to_async(calls_itself)()

        data = await _data(authenticated_context, AGENTS, {"callerOf": str(child.id)})

        assert data["agents"] == []

    async def test_a_task_of_another_organization_has_no_callers_here(self, authenticated_context: HttpContext):
        def elsewhere() -> models.Task:
            other = Organization.objects.create(slug="caller-elsewhere-org")
            ui = _agent(authenticated_context, "caller-elsewhere-ui", organization=other)
            agent = _agent(authenticated_context, "caller-elsewhere", organization=other)
            return _task(agent, caller_agent=ui)

        task = await sync_to_async(elsewhere)()

        data = await _data(authenticated_context, AGENTS, {"callerOf": str(task.id)})

        assert data["agents"] == []

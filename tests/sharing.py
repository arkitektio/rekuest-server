"""What the sharing tests share: a workflow's task tree in the test organization, and how a full
member puts things in a collaboration under a fresh key, as lok hands one out."""

import uuid
from dataclasses import dataclass

from authentikate.models import App, Client, Organization, Release, User
from kante.context import HttpContext, UniversalRequest
from strawberry.http.temporal_response import TemporalResponse
from strawberry.types import ExecutionResult

from facade import enums, models
from facade.schema import schema
from tests.factories import TEST_TOKEN
from tests.graphql.test_cross_tenant_isolation import tenant_context

ADD_MANY = "mutation ($input: AddManyToCollaborationInput!) { addManyToCollaboration(input: $input) { key name } }"


def token_context(token: str) -> HttpContext:
    """A request presenting the static token ``token``, as a client sends it: nothing resolved yet
    (authentikate's extension reads the token, the organization and the user from it)."""
    return HttpContext(
        request=UniversalRequest(_extensions={}),
        response=TemporalResponse(),
        headers={"Authorization": f"Bearer {token}"},
        type="http",
    )


async def run(context: HttpContext, query: str, **variables: object) -> ExecutionResult:
    return await schema.execute(
        query, context_value=context, variable_values=variables or None
    )


def data_of(result: ExecutionResult) -> dict[str, object]:
    assert not result.errors, result.errors
    assert result.data is not None
    return result.data


def reason_of(result: ExecutionResult) -> str | None:
    """Why the operation was refused: the first error's ``reason``."""
    assert result.errors, "it was let through"
    extensions = result.errors[0].extensions or {}
    reason = extensions.get("reason")
    return reason if isinstance(reason, str) else None


def ids(rows: object) -> list[str]:
    assert isinstance(rows, list)
    found: list[str] = []
    for row in rows:
        assert isinstance(row, dict)
        found.append(str(row["id"]))
    return sorted(found)


async def share(*tasks: models.Task, token: str = "test") -> str:
    """A collaboration of the test organization holding ``tasks``, as a full member puts them in:
    under a fresh key, in one ``addManyToCollaboration``. Its key."""
    key = str(uuid.uuid4())
    added = await run(
        token_context(token),
        ADD_MANY,
        input={
            "collaboration": key,
            "name": "Shared",
            "objects": [
                {"identifier": "@rekuest/task", "object": str(task.pk)}
                for task in tasks
            ],
        },
    )
    assert data_of(added)["addManyToCollaboration"] == {"key": key, "name": "Shared"}
    return key


def organization(token: str = TEST_TOKEN) -> Organization:
    """The organization the static token ``token`` acts in (``test`` and ``guest`` share one)."""
    return tenant_context(token)[3]


def agent(org: Organization, prefix: str) -> models.Agent:
    user = User.objects.create(
        username=f"{prefix}-user", password="x", sub=f"{prefix}-sub"
    )
    client = Client.objects.create(client_id=f"{prefix}-client")
    app = App.objects.create(identifier=f"{prefix}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    return models.Agent.objects.create(
        app=app,
        release=release,
        user=user,
        client=client,
        organization=org,
        hash=f"{prefix}-hash",
        name=prefix,
        kind=enums.AgentKind.WEBHOOK.value,
    )


def action(org: Organization, agent: models.Agent, prefix: str) -> models.Action:
    found = models.Action.objects.create(
        app=agent.app,
        key=f"{prefix}-key",
        version="1.0.0",
        name=f"{prefix} action",
        description=f"{prefix} description",
        hash=f"{prefix}-hash",
        organization=org,
    )
    return found


def task(
    action: models.Action,
    agent: models.Agent,
    *,
    parent: models.Task | None = None,
    **fields: object,
) -> models.Task:
    root = None if parent is None else (parent.root or parent)
    return models.Task.objects.create(
        action=action,
        agent=agent,
        parent=parent,
        root=root,
        latest_event_kind=enums.TaskEventKind.STARTED,
        latest_instruct_kind=enums.TaskInstructChoices.ASSIGN,
        **fields,
    )


@dataclass
class Workflow:
    """A root task someone started, the tasks it caused, and a task of another workflow."""

    agent: models.Agent
    action: models.Action
    child_action: models.Action
    root: models.Task
    first: models.Task
    second: models.Task
    grandchild: models.Task
    unrelated: models.Task

    @property
    def tree(self) -> list[models.Task]:
        return [self.root, self.first, self.second, self.grandchild]


def workflow(prefix: str, org: Organization | None = None) -> Workflow:
    """A workflow in ``org`` (the test organization): a root, two children, a grandchild, and an
    unrelated root running the same action as the root."""
    org = org if org is not None else organization()
    runner = agent(org, prefix)
    started = action(org, runner, f"{prefix}-root")
    caused = action(org, runner, f"{prefix}-child")
    root = task(started, runner)
    first = task(caused, runner, parent=root)
    second = task(caused, runner, parent=root)
    grandchild = task(caused, runner, parent=first)
    models.TaskEvent.objects.create(task=root, kind="LOG", message="started")
    models.TaskEvent.objects.create(task=grandchild, kind="LOG", message="deep")
    unrelated = task(started, runner)
    models.TaskEvent.objects.create(task=unrelated, kind="LOG", message="not yours")
    return Workflow(runner, started, caused, root, first, second, grandchild, unrelated)

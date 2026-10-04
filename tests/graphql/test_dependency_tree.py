"""The dependency tree, as GraphQL reads it: a task's frozen snapshot and an assign's dry run.

takt resolves the tree (its tests cover that; the dry run here asks the real one); this pins
the readers of what it answers: a
level is ``{"dependencies": {key: agents}}``, each bound implementation carries its own level,
and a dry run adds ``meta`` (the declared dependency and why it is unmet) beside each level.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade import models
from facade.schema import schema
from rekuest_core.inputs.models import ImplementationInputModel
from tests.factories import create_agent_for_registry, create_registry_bundle
from tests.registered import create_implementation

LEVEL = """
    key
    unmet
    dependency { id key optional }
    mappedAgents {
        agentId
        mappedImplementations {
            key
            implementation { id }
            resolvedDependencies {
                key
                unmet
                dependency { id key optional }
                mappedAgents { agentId }
            }
        }
    }
"""

TREE_QUERY = "query Tree($input: DependencyTreeInput!) { dependencyTree(input: $input) { satisfied dependencies {" + LEVEL + "} } }"
TASK_QUERY = "query Task($id: ID!) { task(id: $id) { resolvedDependencies {" + LEVEL + "} } }"


def _definition(key: str) -> dict:
    return {"key": key, "version": "1", "name": key, "kind": "FUNCTION", "args": [], "returns": []}


def _seed(context: HttpContext):
    """A workflow that depends on a relay, which depends (optionally) on a leaf.

    Both agents are HookAgents: takt only binds an agent that can receive work.
    """
    request = context.request
    org = request.organization

    def agent(prefix: str) -> models.Agent:
        user, _, _, caller = create_registry_bundle(prefix)
        created = create_agent_for_registry(caller, user, org, prefix)
        models.Agent.objects.filter(pk=created.pk).update(kind="WEBHOOK", hook_url="https://hook.example/in")
        return created

    workflow = create_implementation(
        ImplementationInputModel.model_validate({"interface": "workflow", "definition": _definition("workflow"), "dependencies": [{"key": "relay", "action_dependencies": [{"key": "relay"}]}]}),
        agent("tree-workflow"),
    )
    relay = create_implementation(
        ImplementationInputModel.model_validate({"interface": "relay", "definition": _definition("relay"), "dependencies": [{"key": "leaf", "optional": True, "action_dependencies": [{"key": "leaf"}]}]}),
        agent("tree-relay"),
    )
    return workflow, relay


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_dry_run_is_read_level_by_level(authenticated_context: HttpContext, takt: str):
    await schema.execute("query { __typename }", context_value=authenticated_context)
    workflow, relay = await sync_to_async(_seed)(authenticated_context)
    declared = await models.Dependency.objects.aget(implementation=workflow, key="relay")
    below = await models.Dependency.objects.aget(implementation=relay, key="leaf")

    async def dry_run(pins: list[dict] | None) -> dict:
        result = await schema.execute(TREE_QUERY, context_value=authenticated_context, variable_values={"input": {"implementation": str(workflow.pk), "dependencies": pins}})
        assert result.errors is None, result.errors
        return result.data["dependencyTree"]

    # As it stands, the relay is bound to nobody: takt says why an assign would be refused.
    unbound = await dry_run(None)
    assert unbound["satisfied"] is False
    (root,) = unbound["dependencies"]
    assert (root["key"], root["dependency"], root["mappedAgents"]) == ("relay", {"id": str(declared.pk), "key": "relay", "optional": False}, [])
    assert "was not provided with an overwrite" in root["unmet"]

    # Pinned to the relay's agent, it binds that agent's implementation, and below it the leaf:
    # optional and bound to nobody, which is fine.
    tree = await dry_run([{"key": "relay", "mappedAgents": [{"key": "relay", "agent": str(relay.agent_id)}]}])
    assert tree["satisfied"] is True
    (root,) = tree["dependencies"]
    assert (root["key"], root["unmet"], root["dependency"]) == ("relay", None, {"id": str(declared.pk), "key": "relay", "optional": False})
    (bound,) = root["mappedAgents"]
    assert bound["agentId"] == str(relay.agent_id)
    (implementation,) = bound["mappedImplementations"]
    assert (implementation["key"], implementation["implementation"]["id"]) == ("relay", str(relay.pk))
    (leaf,) = implementation["resolvedDependencies"]
    assert leaf == {"key": "leaf", "unmet": None, "dependency": {"id": str(below.pk), "key": "leaf", "optional": True}, "mappedAgents": []}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_tasks_snapshot_is_read_level_by_level(authenticated_context: HttpContext):
    await schema.execute("query { __typename }", context_value=authenticated_context)
    workflow, relay = await sync_to_async(_seed)(authenticated_context)
    request = authenticated_context.request

    def tasks():
        caller, _ = models.Caller.objects.get_or_create(client=request.client, user=request.user, organization=request.organization)
        kinds = {"latest_event_kind": "QUEUED", "latest_instruct_kind": "ASSIGN"}
        frozen = {"relay": [{"agent": str(relay.agent_id), "actions": {"relay": {"implementation": str(relay.pk), "dependencies": {"leaf": []}}}}]}
        nested = models.Task.objects.create(action=workflow.action, implementation=workflow, agent=workflow.agent, caller=caller, reference="tree-nested", dependencies=frozen, **kinds)
        # A task takt never gave a snapshot (a higher-order wrapper's column default, an old row).
        bare = models.Task.objects.create(action=workflow.action, implementation=workflow, agent=workflow.agent, caller=caller, reference="tree-bare", dependencies=None, **kinds)
        return nested, bare

    nested, bare = await sync_to_async(tasks)()

    result = await schema.execute(TASK_QUERY, context_value=authenticated_context, variable_values={"id": str(nested.pk)})
    assert result.errors is None, result.errors
    (root,) = result.data["task"]["resolvedDependencies"]
    # The snapshot carries no notes: the declared dependency is found by key, and nothing is unmet.
    assert (root["key"], root["unmet"], root["dependency"]["key"]) == ("relay", None, "relay")
    (implementation,) = root["mappedAgents"][0]["mappedImplementations"]
    (leaf,) = implementation["resolvedDependencies"]
    assert (leaf["key"], leaf["unmet"], leaf["dependency"]["optional"], leaf["mappedAgents"]) == ("leaf", None, True, [])

    result = await schema.execute(TASK_QUERY, context_value=authenticated_context, variable_values={"id": str(bare.pk)})
    assert result.errors is None, result.errors
    assert result.data["task"]["resolvedDependencies"] == []

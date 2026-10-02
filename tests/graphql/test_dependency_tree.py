"""The dependency tree, as GraphQL reads it: a task's frozen snapshot and an assign's dry run.

takt resolves the tree (its tests cover that); this pins the readers of what it answers: a
level is ``{"dependencies": {key: agents}}``, each bound implementation carries its own level,
and a dry run adds ``meta`` (the declared dependency and why it is unmet) beside each level.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from rekuest_core.inputs.models import ImplementationInputModel

from facade import models, takt
from facade.schema import schema
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
    """A workflow that depends on a relay, which depends (optionally) on a leaf."""
    request = context.request
    org = request.organization

    def agent(prefix: str) -> models.Agent:
        user, _, _, caller = create_registry_bundle(prefix)
        return create_agent_for_registry(caller, user, org, prefix)

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
async def test_a_dry_run_is_read_level_by_level(authenticated_context: HttpContext, monkeypatch: pytest.MonkeyPatch):
    await schema.execute("query { __typename }", context_value=authenticated_context)
    workflow, relay = await sync_to_async(_seed)(authenticated_context)
    declared = await models.Dependency.objects.aget(implementation=workflow, key="relay")
    below = await models.Dependency.objects.aget(implementation=relay, key="leaf")
    asked: list[tuple[str, dict]] = []

    def answer(op: str, payload: dict) -> dict:
        asked.append((op, payload))
        return {
            "satisfied": False,
            "meta": {"relay": {"dependency": str(declared.pk), "unmet": None}},
            "dependencies": {
                "relay": [
                    {
                        "agent": str(relay.agent_id),
                        "actions": {
                            "relay": {
                                "implementation": str(relay.pk),
                                "dependencies": {"leaf": []},
                                "meta": {"leaf": {"dependency": str(below.pk), "unmet": "Dependency leaf is not met"}},
                            }
                        },
                    }
                ]
            },
        }

    monkeypatch.setattr(takt, "call", answer)
    pins = [{"key": "relay", "mappedAgents": [{"key": "relay", "agent": str(relay.agent_id), "dependencies": [{"key": "leaf", "mappedAgents": []}]}]}]
    result = await schema.execute(TREE_QUERY, context_value=authenticated_context, variable_values={"input": {"implementation": str(workflow.pk), "dependencies": pins}})

    assert result.errors is None, result.errors
    # takt is asked exactly what an assign would send it, the pins nested under their agent.
    ((op, payload),) = asked
    assert op == "resolve"
    assert payload["input"] == {
        "implementation": str(workflow.pk),
        "dependencies": [{"key": "relay", "auto_resolve": False, "mapped_agents": [{"key": "relay", "agent": str(relay.agent_id), "dependencies": [{"key": "leaf", "mapped_agents": [], "auto_resolve": False}]}]}],
    }
    tree = result.data["dependencyTree"]
    assert tree["satisfied"] is False
    (root,) = tree["dependencies"]
    assert (root["key"], root["unmet"], root["dependency"]) == ("relay", None, {"id": str(declared.pk), "key": "relay", "optional": False})
    (bound,) = root["mappedAgents"]
    assert bound["agentId"] == str(relay.agent_id)
    (implementation,) = bound["mappedImplementations"]
    assert (implementation["key"], implementation["implementation"]["id"]) == ("relay", str(relay.pk))
    (leaf,) = implementation["resolvedDependencies"]
    assert leaf == {"key": "leaf", "unmet": "Dependency leaf is not met", "dependency": {"id": str(below.pk), "key": "leaf", "optional": True}, "mappedAgents": []}


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

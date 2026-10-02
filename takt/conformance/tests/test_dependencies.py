"""Dependencies that have dependencies: the whole tree is resolved when the root is assigned.

Three raw agents of three apps. The *workflow* depends on the *relay*'s ``echo``, and the relay's
``echo`` depends on the *leaf*'s. Both call their dependency ``peer``: a dependency key is a
parameter name and repeats across levels, so each level has to read its own.
"""

import copy
import uuid
from typing import Any

from conftest import ECHO_DECLARATION, session_id

LEVEL = "key unmet dependency { key } mappedAgents { agentId mappedImplementations { key implementation { interface } %s } }"
TREE = "query ($input: DependencyTreeInput!) { dependencyTree(input: $input) { satisfied dependencies { %s } } }" % (
    LEVEL % ("resolvedDependencies { %s }" % (LEVEL % ""))
)


def depending_on(app: str | None, *, auto: bool) -> dict[str, Any]:
    """The echo declaration, its implementation depending on ``app``'s ``echo`` as ``peer``."""
    declaration = copy.deepcopy(ECHO_DECLARATION)
    if app is not None:
        declaration["implementations"][0]["dependencies"] = [
            {"key": "peer", "app": app, "auto_resolvable": auto, "optional": False, "action_dependencies": [{"key": "echo", "demand": {"key": "echo"}}], "state_dependencies": []}
        ]
    return declaration


async def chain(agents, tokens: tuple[str, str, str], *, auto: bool) -> tuple[Any, Any, Any, str, str, str]:  # noqa: ANN001
    """Leaf, relay and workflow registered, leaves first: the sockets and the agents' ids.

    A token is its own app, so ``tokens`` (leaf, relay, workflow) are also what is depended on.
    """
    leaf_token, relay_token, workflow_token = tokens
    leaf = await agents()
    leaf_init = await leaf.register(leaf_token, session_id=session_id(), **depending_on(None, auto=auto))
    relay = await agents()
    relay_init = await relay.register(relay_token, session_id=session_id(), **depending_on(leaf_token, auto=auto))
    workflow = await agents()
    workflow_init = await workflow.register(workflow_token, session_id=session_id(), **depending_on(relay_token, auto=auto))
    return leaf, relay, workflow, leaf_init["agent"], relay_init["agent"], workflow_init["agent"]


def dependency_call(parent: str) -> dict[str, Any]:
    """What a workflow's proxy sends: its own task, the dependency's key, the method."""
    return {"id": str(uuid.uuid4()), "type": "ASSIGN_REQUEST", "reference": str(uuid.uuid4()), "parent": parent, "dependency": "peer", "method": "echo", "args": {"x": 2}}


async def test_the_tree_resolves_by_itself_and_each_level_calls_its_own_peer(agents, graphql) -> None:  # noqa: ANN001
    leaf, relay, workflow, leaf_id, relay_id, workflow_id = await chain(agents, ("conf_37", "conf_38", "conf_39"), auto=True)
    api = graphql("conf_39")

    root = await api.assign(workflow_id, "echo", {"x": 1})
    assert (await workflow.receive_type("ASSIGN"))["task"] == root

    # The workflow's `peer` is the relay …
    await workflow.send(dependency_call(root))
    child = (await workflow.receive_type("ASSIGN_RESPONSE"))["task"]
    assign = await relay.receive_type("ASSIGN")
    assert (assign["task"], assign["parent"], assign["root"]) == (child, root, root), assign

    # … and the relay's `peer`, asked for as its own task's, is the leaf.
    await relay.send(dependency_call(child))
    response = await relay.receive_type("ASSIGN_RESPONSE")
    assert response.get("error") is None, response
    assign = await leaf.receive_type("ASSIGN")
    assert (assign["task"], assign["parent"], assign["root"]) == (response["task"], child, root), assign

    data = await api("query ($id: ID!) { task(id: $id) { resolvedDependencies { %s } } }" % (LEVEL % ("resolvedDependencies { %s }" % (LEVEL % ""))), id=root)
    (peer,) = data["task"]["resolvedDependencies"]
    (bound,) = peer["mappedAgents"]
    assert (peer["key"], bound["agentId"]) == ("peer", relay_id), peer
    (below,) = bound["mappedImplementations"][0]["resolvedDependencies"]
    assert (below["key"], [agent["agentId"] for agent in below["mappedAgents"]]) == ("peer", [leaf_id]), below


async def test_a_dry_run_shows_what_is_unmet_and_a_pin_one_level_down_meets_it(agents, graphql) -> None:  # noqa: ANN001
    _leaf, _relay, workflow, leaf_id, relay_id, workflow_id = await chain(agents, ("conf_40", "conf_41", "conf_42"), auto=False)
    api = graphql("conf_42")
    implementation = (await api('query ($agent: ID!) { implementationAt(agent: $agent, interface: "echo") { id } }', agent=workflow_id))["implementationAt"]["id"]

    async def tree(pins: list[dict[str, Any]]) -> dict[str, Any]:
        return (await api(TREE, input={"implementation": implementation, "dependencies": pins}))["dependencyTree"]

    def pin(agent: str, below: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"key": "peer", "mappedAgents": [{"key": "peer", "agent": agent, **({"dependencies": below} if below else {})}]}

    # Nothing pinned: the root's own dependency is what is unmet.
    unpinned = await tree([])
    assert unpinned["satisfied"] is False
    (peer,) = unpinned["dependencies"]
    assert peer["mappedAgents"] == [] and peer["unmet"].startswith("Dependency peer was not provided with an overwrite"), peer

    # The root pinned: its pin does not reach the relay's `peer`, which is now the unmet one.
    shallow = await tree([pin(relay_id)])
    assert shallow["satisfied"] is False
    (peer,) = shallow["dependencies"]
    assert peer["unmet"] is None and peer["dependency"] == {"key": "peer"}, peer
    (below,) = peer["mappedAgents"][0]["mappedImplementations"][0]["resolvedDependencies"]
    assert below["mappedAgents"] == [] and below["unmet"].startswith("Dependency peer was not provided with an overwrite"), below

    # An assign refuses for the same reason the dry run gave.
    assign = "mutation ($input: AssignInput!) { assign(input: $input) { id } }"
    refused = await api.client.post(
        api.url,
        json={"query": assign, "variables": {"input": {"agent": workflow_id, "interface": "echo", "args": {"x": 1}, "capture": False, "dependencies": [pin(relay_id)]}}},
        headers={"Authorization": f"Bearer {api.token}"},
    )
    assert below["unmet"] in str(refused.json().get("errors")), refused.json()

    # Pinned one level down, under the agent it is for: met, and the assign goes through.
    nested = [pin(relay_id, [pin(leaf_id)])]
    met = await tree(nested)
    assert met["satisfied"] is True, met
    data = await api(assign, input={"agent": workflow_id, "interface": "echo", "args": {"x": 1}, "capture": False, "reference": str(uuid.uuid4()), "dependencies": nested})
    assert (await workflow.receive_type("ASSIGN"))["task"] == data["assign"]["id"]


async def test_a_dependency_call_outside_the_tree_is_refused(agents, graphql) -> None:  # noqa: ANN001
    """The relay's task carries its own subtree, not its parent's: only what it declares is there."""
    leaf, relay, workflow, _leaf_id, _relay_id, workflow_id = await chain(agents, ("conf_43", "conf_44", "conf_45"), auto=True)
    root = await graphql("conf_45").assign(workflow_id, "echo", {"x": 1})
    await workflow.receive_type("ASSIGN")
    await workflow.send(dependency_call(root))
    child = (await workflow.receive_type("ASSIGN_RESPONSE"))["task"]
    await relay.receive_type("ASSIGN")
    await relay.send(dependency_call(child))
    grandchild = (await relay.receive_type("ASSIGN_RESPONSE"))["task"]
    await leaf.receive_type("ASSIGN")

    # The leaf declares nothing: its task has no `peer` to call.
    await leaf.send(dependency_call(grandchild))
    response = await leaf.receive_type("ASSIGN_RESPONSE")
    assert "not found in parent task dependencies" in (response.get("error") or ""), response

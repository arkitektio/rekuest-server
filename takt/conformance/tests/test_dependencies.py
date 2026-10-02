"""Dependencies that have dependencies: the whole tree is resolved when the root is assigned.

Three raw agents of three apps. The *workflow* depends on the *relay*'s ``echo``, and the relay's
``echo`` depends on the *leaf*'s. Both call their dependency ``peer``: a dependency key is a
parameter name and repeats across levels, so each level has to read its own.
"""

import asyncio
import copy
import uuid
from typing import Any

import pytest

from conftest import ECHO_DECLARATION, Closed, session_id
from test_assign import mirrors_of

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


THREE_LEVELS = LEVEL % ("resolvedDependencies { %s }" % (LEVEL % ("resolvedDependencies { %s }" % (LEVEL % ""))))
ASSIGN = "mutation ($input: AssignInput!) { assign(input: $input) { id } }"


async def started(agent, task: str) -> None:  # noqa: ANN001
    """The agent reports the task started, and the report is acknowledged."""
    await agent.send({"type": "STARTED", "task": task, "seq": 1})
    await agent.receive_type("EVENT_ACK")


async def running_chain(agents, graphql, tokens: tuple[str, str, str]) -> tuple[Any, Any, Any, str, str, str]:  # noqa: ANN001
    """The whole tree at work: every level started and waiting on the one below.

    The sockets (leaf, relay, workflow) and the tasks (root, child, grandchild).
    """
    leaf, relay, workflow, _leaf_id, _relay_id, workflow_id = await chain(agents, tokens, auto=True)
    root = await graphql(tokens[2]).assign(workflow_id, "echo", {"x": 1})
    assert (await workflow.receive_type("ASSIGN"))["task"] == root
    await started(workflow, root)

    await workflow.send(dependency_call(root))
    child = (await workflow.receive_type("ASSIGN_RESPONSE"))["task"]
    assert (await relay.receive_type("ASSIGN"))["task"] == child
    await started(relay, child)

    await relay.send(dependency_call(child))
    grandchild = (await relay.receive_type("ASSIGN_RESPONSE"))["task"]
    assert (await leaf.receive_type("ASSIGN"))["task"] == grandchild
    await started(leaf, grandchild)
    return leaf, relay, workflow, root, child, grandchild


async def test_a_nested_workflow_runs_down_and_its_results_come_back_up(agents, graphql) -> None:  # noqa: ANN001
    leaf, relay, workflow, root, child, grandchild = await running_chain(agents, graphql, ("conf_46", "conf_47", "conf_48"))

    # The leaf answers; the relay, its caller, sees every event of the call it made.
    await leaf.send({"type": "YIELD", "task": grandchild, "returns": {"return0": 2}, "seq": 2})
    await leaf.send({"type": "COMPLETED", "task": grandchild, "seq": 3})
    seen = await mirrors_of(relay, grandchild, "COMPLETED_EVENT")
    assert [m["type"] for m in seen] == ["STARTED_EVENT", "YIELD_EVENT", "COMPLETED_EVENT"], seen
    assert seen[1]["returns"] == {"return0": 2}, seen[1]

    # The relay passes the result on; the workflow sees its own call, not the relay's.
    await relay.send({"type": "YIELD", "task": child, "returns": {"return0": 2}, "seq": 2})
    await relay.send({"type": "COMPLETED", "task": child, "seq": 3})
    seen = await mirrors_of(workflow, child, "COMPLETED_EVENT")
    assert [m["type"] for m in seen] == ["STARTED_EVENT", "YIELD_EVENT", "COMPLETED_EVENT"], seen
    assert seen[1]["returns"] == {"return0": 2}, seen[1]

    await workflow.send({"type": "YIELD", "task": root, "returns": {"return0": 2}, "seq": 2})
    await workflow.send({"type": "COMPLETED", "task": root, "seq": 3})
    await workflow.receive_type("EVENT_ACK")

    # What is left behind: three finished tasks, chained by parent and by the dependency called.
    level = "id latestEventKind isDone dependency dependencyMethod parent { id } root { id }"
    data = await graphql("conf_48")("query ($id: ID!) { task(id: $id) { %s children { %s children { %s } } } }" % (level, level, level), id=root)
    top = data["task"]
    assert (top["latestEventKind"], top["isDone"], top["dependency"], top["parent"]) == ("COMPLETED", True, None, None), top
    (middle,) = top["children"]
    assert middle == {**middle, "id": child, "latestEventKind": "COMPLETED", "isDone": True, "dependency": "peer", "dependencyMethod": "echo", "parent": {"id": root}, "root": {"id": root}}, middle
    (bottom,) = middle["children"]
    assert bottom == {**bottom, "id": grandchild, "latestEventKind": "COMPLETED", "dependency": "peer", "dependencyMethod": "echo", "parent": {"id": child}, "root": {"id": root}}, bottom


async def test_an_interrupt_of_the_root_reaches_every_level(agents, graphql) -> None:  # noqa: ANN001
    leaf, relay, workflow, root, child, grandchild = await running_chain(agents, graphql, ("conf_49", "conf_50", "conf_51"))

    await graphql("conf_51")("mutation ($task: ID!) { interrupt(input: {task: $task}) { id } }", task=root)

    assert (await workflow.receive_type("INTERRUPT"))["task"] == root
    assert (await relay.receive_type("INTERRUPT"))["task"] == child
    assert (await leaf.receive_type("INTERRUPT"))["task"] == grandchild


async def test_the_tree_is_frozen_an_agent_that_left_is_not_replaced(agents, graphql) -> None:  # noqa: ANN001
    tokens = ("conf_52", "conf_53", "conf_54")
    leaf, relay, workflow, leaf_id, _relay_id, workflow_id = await chain(agents, tokens, auto=True)
    api = graphql(tokens[2])
    root = await api.assign(workflow_id, "echo", {"x": 1})
    await workflow.receive_type("ASSIGN")
    await workflow.send(dependency_call(root))
    child = (await workflow.receive_type("ASSIGN_RESPONSE"))["task"]
    await relay.receive_type("ASSIGN")

    # The leaf was bound when the root was assigned. It leaves before the relay calls it.
    await leaf.close()
    for _ in range(50):
        if not (await api("query ($id: ID!) { agent(id: $id) { connected } }", id=leaf_id))["agent"]["connected"]:
            break
        await asyncio.sleep(0.1)

    await relay.send(dependency_call(child))
    response = await relay.receive_type("ASSIGN_RESPONSE")
    assert response.get("task") is None, response
    assert "No agent resolved for dependency peer is available right now" in (response.get("error") or ""), response


async def test_two_instances_are_both_bound_and_a_pin_below_picks_one(agents, graphql) -> None:  # noqa: ANN001
    # conf_55 and conf_56 are two agents of one app.
    first = await agents()
    first_id = (await first.register("conf_55", session_id=session_id(), **depending_on(None, auto=True)))["agent"]
    second = await agents()
    second_id = (await second.register("conf_56", session_id=session_id(), **depending_on(None, auto=True)))["agent"]
    assert first_id != second_id
    relay = await agents()
    relay_id = (await relay.register("conf_57", session_id=session_id(), **depending_on("conf_55", auto=True)))["agent"]
    workflow = await agents()
    workflow_id = (await workflow.register("conf_58", session_id=session_id(), **depending_on("conf_57", auto=True)))["agent"]
    api = graphql("conf_58")
    implementation = (await api('query ($agent: ID!) { implementationAt(agent: $agent, interface: "echo") { id } }', agent=workflow_id))["implementationAt"]["id"]

    def below(tree: dict[str, Any]) -> list[str]:
        (peer,) = tree["dependencies"]
        (inner,) = peer["mappedAgents"][0]["mappedImplementations"][0]["resolvedDependencies"]
        return sorted(agent["agentId"] for agent in inner["mappedAgents"])

    resolved = (await api(TREE, input={"implementation": implementation, "dependencies": []}))["dependencyTree"]
    assert resolved["satisfied"] is True and below(resolved) == sorted([first_id, second_id]), resolved

    pins = [{"key": "peer", "mappedAgents": [{"key": "peer", "agent": relay_id, "dependencies": [{"key": "peer", "mappedAgents": [{"key": "peer", "agent": second_id}]}]}]}]
    pinned = (await api(TREE, input={"implementation": implementation, "dependencies": pins}))["dependencyTree"]
    assert pinned["satisfied"] is True and below(pinned) == [second_id], pinned

    # Assigned with that pin, the relay's call can only land on the chosen instance.
    data = await api(ASSIGN, input={"agent": workflow_id, "interface": "echo", "args": {"x": 1}, "capture": False, "reference": str(uuid.uuid4()), "dependencies": pins})
    root = data["assign"]["id"]
    assert (await workflow.receive_type("ASSIGN"))["task"] == root
    await workflow.send(dependency_call(root))
    child = (await workflow.receive_type("ASSIGN_RESPONSE"))["task"]
    await relay.receive_type("ASSIGN")
    await relay.send(dependency_call(child))
    grandchild = (await relay.receive_type("ASSIGN_RESPONSE"))["task"]
    assert (await second.receive_type("ASSIGN"))["task"] == grandchild
    with pytest.raises((asyncio.TimeoutError, TimeoutError, Closed)):
        await first.receive_type("ASSIGN", timeout=1.0)


async def test_a_rerun_pins_the_tree_the_task_ran_with(agents, graphql) -> None:  # noqa: ANN001
    tokens = ("conf_59", "conf_60", "conf_61")
    _leaf, _relay, workflow, _leaf_id, _relay_id, workflow_id = await chain(agents, tokens, auto=True)
    api = graphql(tokens[2])
    frozen = "query ($id: ID!) { task(id: $id) { resolvedDependencies { %s } } }" % (LEVEL % ("resolvedDependencies { %s }" % (LEVEL % "")))

    first = await api.assign(workflow_id, "echo", {"x": 1})
    await workflow.receive_type("ASSIGN")
    ran_with = (await api(frozen, id=first))["task"]["resolvedDependencies"]

    def pins(level: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """A task's bindings as overwrites, the way a rerun sends them."""
        return [
            {
                "key": dependency["key"],
                "mappedAgents": [
                    {
                        "key": dependency["key"],
                        "agent": agent["agentId"],
                        "dependencies": pins([inner for bound in agent["mappedImplementations"] for inner in bound.get("resolvedDependencies", [])]),
                    }
                    for agent in dependency["mappedAgents"]
                ],
            }
            for dependency in level
        ]

    data = await api(ASSIGN, input={"agent": workflow_id, "interface": "echo", "args": {"x": 1}, "capture": False, "reference": str(uuid.uuid4()), "dependencies": pins(ran_with)})
    again = data["assign"]["id"]
    assert again != first and (await workflow.receive_type("ASSIGN"))["task"] == again
    assert (await api(frozen, id=again))["task"]["resolvedDependencies"] == ran_with

    # Another organization may not look into this tree.
    implementation = (await api('query ($agent: ID!) { implementationAt(agent: $agent, interface: "echo") { id } }', agent=workflow_id))["implementationAt"]["id"]
    stranger = graphql("conf_65")  # the one token of another organization
    refused = await stranger.client.post(stranger.url, json={"query": TREE, "variables": {"input": {"implementation": implementation, "dependencies": []}}}, headers={"Authorization": f"Bearer {stranger.token}"})
    assert "not in your organization" in str(refused.json().get("errors")), refused.json()


async def test_a_cycle_is_refused_and_the_dry_run_says_where(agents, graphql) -> None:  # noqa: ANN001
    # workflow → relay → leaf → workflow
    leaf = await agents()
    await leaf.register("conf_62", session_id=session_id(), **depending_on("conf_64", auto=True))
    relay = await agents()
    await relay.register("conf_63", session_id=session_id(), **depending_on("conf_62", auto=True))
    workflow = await agents()
    workflow_id = (await workflow.register("conf_64", session_id=session_id(), **depending_on("conf_63", auto=True)))["agent"]
    api = graphql("conf_64")

    refused = await api.client.post(
        api.url,
        json={"query": ASSIGN, "variables": {"input": {"agent": workflow_id, "interface": "echo", "args": {"x": 1}, "capture": False}}},
        headers={"Authorization": f"Bearer {api.token}"},
    )
    assert "Dependency cycle" in str(refused.json().get("errors")), refused.json()

    implementation = (await api('query ($agent: ID!) { implementationAt(agent: $agent, interface: "echo") { id } }', agent=workflow_id))["implementationAt"]["id"]
    query = "query ($input: DependencyTreeInput!) { dependencyTree(input: $input) { satisfied dependencies { %s } } }" % THREE_LEVELS
    tree = (await api(query, input={"implementation": implementation, "dependencies": []}))["dependencyTree"]
    assert tree["satisfied"] is False
    # The level that would close the circle is the one marked: the leaf's own dependency.
    (peer,) = tree["dependencies"]
    (second,) = peer["mappedAgents"][0]["mappedImplementations"][0]["resolvedDependencies"]
    (third,) = second["mappedAgents"][0]["mappedImplementations"][0]["resolvedDependencies"]
    assert (peer["unmet"], second["unmet"]) == (None, None), tree
    assert third["mappedAgents"] == [] and third["unmet"].startswith("Dependency cycle"), third

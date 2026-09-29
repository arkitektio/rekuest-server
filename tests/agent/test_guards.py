"""A workflow's guard: did anything other than the workflow change the state since it saw it?

The workflow's own calls change the state too (its dispense fills the plate), which is not
news. A state set up again (its agent restarted) has changed. Only the guarded paths count.
"""

import pytest

from facade import messages, models
from facade.guards import state_revision_sync

from tests.factories import _build_state_for_agent, _build_task, _seed_throwaway_agent_graph

pytestmark = pytest.mark.django_db(transaction=True)


class World:
    """A workflow depending on a robot with a plate state, in the robot's session "s1"."""

    def __init__(self) -> None:
        self.workflow = _build_task("guard-wf")
        self.robot = _seed_throwaway_agent_graph("guard-robot")
        models.Agent.objects.filter(pk=self.robot.pk).update(active_session_id="s1")
        self.state = _build_state_for_agent(self.robot.pk, "plate", "guard")
        models.State.objects.filter(pk=self.state.pk).update(key="plate")
        self.session = models.Session.objects.create(agent=self.robot, session_id="s1")
        models.Task.objects.filter(pk=self.workflow.pk).update(dependencies={"handler": [{"agent": self.robot.pk}]})
        self.rev = 0

    def ask(self, since=None, paths=()):  # noqa: ANN001, ANN201
        request = messages.StateRevisionRequest(parent=str(self.workflow.pk), dependency="handler", state="plate", since=since, paths=list(paths))
        return state_revision_sync(self.workflow.agent_id, request)

    def patch(self, path: str, task=None, session=None) -> None:  # noqa: ANN001
        self.rev += 1
        models.Patch.objects.create(
            state=self.state, agent=self.robot, interface="plate", session=session or self.session,
            op="replace", path=path, value=1, global_rev=self.rev, task=task,
        )


def test_a_fresh_guard_gets_the_revision_to_record() -> None:
    world = World()
    world.patch("/barcode")

    revision, changed, _ = world.ask()

    assert revision == {"session": "s1", "global_rev": 1} and changed is None


def test_the_workflows_own_calls_do_not_count() -> None:
    world = World()
    since, _, _ = world.ask()
    child = _build_task("guard-child")
    models.Task.objects.filter(pk=child.pk).update(parent=world.workflow)
    grandchild = _build_task("guard-grandchild")
    models.Task.objects.filter(pk=grandchild.pk).update(parent=child)

    world.patch("/dispensed_ul/A1", task=child)
    world.patch("/barcode", task=grandchild)

    _, changed, _ = world.ask(since=since)
    assert changed is False


def test_another_task_changing_a_guarded_path_counts() -> None:
    world = World()
    since, _, _ = world.ask(paths=["barcode"])
    stranger = _build_task("guard-stranger")

    world.patch("/barcode", task=stranger)

    _, changed, detail = world.ask(since=since, paths=["barcode"])
    assert changed is True and "/barcode" in detail and f"task {stranger.pk}" in detail


def test_a_change_off_the_guarded_paths_does_not_count() -> None:
    world = World()
    since, _, _ = world.ask(paths=["barcode"])

    world.patch("/temperature")  # the agent's own background writer
    world.patch("/barcodes_seen")  # a sibling path, not a child of /barcode

    _, changed, _ = world.ask(since=since, paths=["barcode"])
    assert changed is False


def test_the_agent_itself_changing_a_guarded_path_counts() -> None:
    world = World()
    since, _, _ = world.ask(paths=["barcode"])

    world.patch("/barcode")

    _, changed, detail = world.ask(since=since, paths=["barcode"])
    assert changed is True and "the agent itself" in detail


def test_a_state_set_up_again_has_changed() -> None:
    world = World()
    since, _, _ = world.ask()
    models.Agent.objects.filter(pk=world.robot.pk).update(active_session_id="s2")
    models.Session.objects.create(agent=world.robot, session_id="s2")

    _, changed, detail = world.ask(since=since)
    assert changed is True and "set up again" in detail


def test_a_dependency_on_several_agents_cannot_be_guarded() -> None:
    world = World()
    other = _seed_throwaway_agent_graph("guard-other")
    models.Task.objects.filter(pk=world.workflow.pk).update(dependencies={"handler": [{"agent": world.robot.pk}, {"agent": other.pk}]})

    with pytest.raises(ValueError, match="one agent"):
        world.ask()

"""rekuest as a service whose objects a collaboration can hold: what a guest may be shown.

A collaboration is lok's (one key across the hub); rekuest keeps what was put in it here
(``arkitekt_service.collaborations``). What rekuest shares is its tasks, and nothing else: a task
is the record of a run, and a workflow (what someone started, with everything it caused) is a
root task with its tree. Everything that *runs* things stays the organization's alone — agents,
their implementations, sessions, state and memory, schedules, triggers, wiregrams, bloks a
person made, keys — and no fetch below hands any of it to a collaboration.

What a task brings to be seen (``depends``): the action it ran (its definition: name,
description and ports, read-only), the tasks it caused, as they are now, and the bloks it made
from its BSX to ask or inform with. What is part of a task (its events and instructs, by their
required, cascading key) is in a collaboration with it, live.

A task is not part of its action or its agent, though it cascades with both: one action runs
as many tasks, of many people, and so does an agent. The same holds for whatever else hangs off
an action without being its definition (implementations, schedules, triggers, test cases, its
own bloks): an action brought along for a task shows its ports, never these. Only the action's
ports are part of it.

``@rekuest/agent`` is declared in the contract (actions take agents as structures) but not bound
here: an agent is never put in a collaboration, so a guest reads a shared task's ``agent`` as
null. rekuest does not mount its own manifest (``service.urls``): it catalogues itself.
"""

from collections.abc import Iterable

from arkitekt_service.service import Service
from django.db.models import Model, Q

from facade import models
from rekuest.contract import HOSTS

service = Service(
    "rekuest",
    hosts=HOSTS,
    description="Assigns work to agents and keeps the record of it.",
)

# A task runs an action on an agent; it is no part of either (``depends`` brings the action).
service.not_part_of(models.Task, "action", "agent")
# What hangs off an action without being its definition: an action brought along for a task
# shows its ports, not who implements it, what runs it on its own, or how it is tested and drawn.
service.not_part_of(models.Implementation, "action")
service.not_part_of(models.Schedule, "action")
service.not_part_of(models.Trigger, "action")
service.not_part_of(models.TestCase, "action", "tester")
service.not_part_of(models.ActionBlok, "action")


def _task_needs(task: models.Task) -> Iterable[Model]:
    """What a task needs to be seen besides its events: the action it ran, the tasks it caused
    (each with what it needs in turn), and the bloks it made to ask or inform with."""
    return [
        task.action,
        *models.Task.objects.filter(parent_id=task.pk),
        *models.Blok.objects.filter(owner_task_id=task.pk),
    ]


def _tree_of(root_task_id: str) -> Q:
    """The tasks of the workflow ``root_task_id``: the root itself and every task it caused.

    A provenance token's ``rtk`` is the root task's id here, so this is what every service calls
    the workflow. Held to an organization by the caller: ``shareWorkflow`` only takes a root task
    of the request's organization, and a list filtered by it is the organization's already.
    """
    if not root_task_id.isdigit():
        return Q(pk__in=[])
    root = int(root_task_id)
    return Q(pk=root) | Q(root_id=root)


task = service.structure(
    models.Task, "@rekuest/task", depends=_task_needs, created_under=_tree_of
)

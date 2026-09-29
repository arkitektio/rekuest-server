"""What a workflow's guard asks about a state it depends on (``task.guard``).

A workflow that was down (its agent died, it was resumed) may come back to a world that
moved: someone loaded another plate. The guard recorded the state's revision when the
workflow first entered it; on the resumed run it asks whether anything changed since.

"Anything" excludes the workflow's own calls: its ``dispense`` changes the plate too, and
that is not news. And a state set up again (the agent restarted, a new session) has changed,
whatever its values.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Max, Q

from facade import messages, models


def state_revision_sync(agent_id: int, message: messages.StateRevisionRequest) -> tuple[dict[str, Any], bool | None, str | None]:
    """``(revision now, changed since, what changed it)`` for the guarded state."""
    parent = models.Task.objects.select_related("implementation").get(pk=message.parent)
    if parent.agent_id != agent_id:
        raise PermissionError("A guard is asked by the agent running the workflow.")

    state, owner = _guarded_state(parent, message.dependency, message.state)
    session = owner.active_session_id
    revision = {"session": session, "global_rev": _latest_rev(state, session)}
    if message.since is None:
        return revision, None, None

    if message.since.get("session") != session:
        return revision, True, f"{message.state!r} was set up again: its agent restarted since."

    changes = models.Patch.objects.filter(state=state, session__session_id=session, global_rev__gt=message.since.get("global_rev", 0)).exclude(task_id__in=_call_tree(parent))
    if message.paths:
        wanted = Q()
        for path in message.paths:
            pointer = "/" + path.strip("/").replace(".", "/")
            wanted |= Q(path=pointer) | Q(path__startswith=pointer + "/")
        changes = changes.filter(wanted)
    change = changes.order_by("global_rev").first()
    if change is None:
        return revision, False, None
    by = f"task {change.task_id}" if change.task_id else "the agent itself"
    return revision, True, f"{message.state!r} changed at {change.path} (by {by}) since the workflow last saw it."


def _guarded_state(parent: models.Task, dependency: str, slot: str) -> tuple[models.State, models.Agent]:
    entries = (parent.dependencies or {}).get(dependency)
    if not entries:
        raise ValueError(f"The workflow has no dependency {dependency!r} to guard.")
    agents = {entry.get("agent") for entry in entries if entry.get("agent")}
    if len(agents) != 1:
        raise ValueError(f"A guard needs a dependency resolved to one agent; {dependency!r} was resolved to {len(agents)}.")
    owner = models.Agent.objects.get(pk=next(iter(agents)))

    declared = models.Dependency.objects.filter(implementation=parent.implementation, key=dependency).first()
    demand = next((d for d in (declared.get_state_dependencies() if declared else []) if d.key == slot), None)
    identity = (demand.demand.key if demand is not None and demand.demand is not None and demand.demand.key else None) or slot
    state = models.State.objects.filter(agent=owner, key=identity).first() or models.State.objects.filter(agent=owner, interface=identity).first()
    if state is None:
        raise ValueError(f"The agent of {dependency!r} has no state {identity!r} to guard.")
    return state, owner


def _latest_rev(state: models.State, session: str | None) -> int:
    if session is None:
        return 0
    patch = models.Patch.objects.filter(state=state, session__session_id=session).aggregate(rev=Max("global_rev"))["rev"]
    snapshot = models.Snapshot.objects.filter(state=state, session__session_id=session).aggregate(rev=Max("global_rev"))["rev"]
    return max(patch or 0, snapshot or 0)


def _call_tree(root: models.Task) -> set[int]:
    """The workflow and every task it caused, however deep: their changes are its own."""
    tree, frontier = {root.pk}, [root.pk]
    while frontier:
        frontier = list(models.Task.objects.filter(parent_id__in=frontier).exclude(pk__in=tree).values_list("pk", flat=True))
        tree.update(frontier)
    return tree

"""The agents that called a task: who it can turn to for what it cannot do itself.

A task's callers are the agents of its ancestors, nearest first, and then the agent of the
root's caller (the app a person started the run from, e.g. orkestrator). An app finds an action
it needs among them with an ordinary implementations query (``agent: {callerOf: <task>}``) and
calls it like any other child; nothing about the call itself is special.
"""

from __future__ import annotations

from django.db.models import Case, IntegerField, Model, QuerySet, Value, When

from facade import models

#: Ancestors walked at most: a deeper tree is cut off, never an error.
MAX_DEPTH = 32


def caller_agent_ids(task_id: object, organization_id: int) -> list[int]:
    """The ids of the agents that called ``task_id``, nearest first.

    Empty for a task of another organization (or none). The task's own agent is never one of
    them: an agent does not call itself back.
    """
    task = models.Task.objects.filter(id=task_id, agent__organization_id=organization_id).only("id", "agent_id", "parent_id", "root_id").first()
    if task is None:
        return []

    found: list[int] = []
    parent_id = task.parent_id
    for _ in range(MAX_DEPTH):
        if parent_id is None:
            break
        parent = models.Task.objects.only("agent_id", "parent_id").get(id=parent_id)
        found.append(parent.agent_id)
        parent_id = parent.parent_id

    # The run's caller: its root's, or the task's own when it is the root.
    root_caller = models.Caller.objects.filter(tasks__id=task.root_id or task.id).first()
    if root_caller is not None:
        found.extend(
            models.Agent.objects.filter(
                client_id=root_caller.client_id,
                user_id=root_caller.user_id,
                organization_id=root_caller.organization_id,
            ).values_list("id", flat=True)
        )

    return [agent_id for agent_id in dict.fromkeys(found) if agent_id != task.agent_id]


def nearest_first[M: Model](queryset: QuerySet[M], ids: list[int], field: str) -> QuerySet[M]:
    """``queryset`` narrowed to ``ids`` (agent ids in ``field``) and ordered as they are listed."""
    if not ids:
        return queryset.none()
    rank = Case(*(When(**{field: agent_id}, then=Value(index)) for index, agent_id in enumerate(ids)), output_field=IntegerField())
    return queryset.filter(**{f"{field}__in": ids}).annotate(caller_rank=rank).order_by("caller_rank")

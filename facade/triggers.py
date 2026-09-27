"""Matching signals to triggers and firing them (see :mod:`facade.models.signal`).

``fire_triggers_sync`` is a reaper step: it claims unprocessed signals under ``skip_locked`` —
any number of reapers may run it — and for each one assigns every matching trigger's action.

A trigger matches a signal when kind, structure identifier and organization agree and the
signal's descriptors satisfy BOTH the trigger's own conditions and the target port's own
``requires`` (its ``compiled_jsonpath``): a trigger can never feed an action an object the
action has declared it cannot take. Both are checked in one query with the same
``jsonb_path_match`` the action search uses (``facade.managers``); a path that errors (a type
mismatch against the object) reads as NULL, which is not a match.

A run caused by a task is that task's child (``parent`` = the causing task), so it joins the
causing tree, and its provenance token names the tree's human at the root. Each trigger ×
signal pair runs at most once: the run's reference is ``trigger:<id>:<signal>``, unique per
caller. ``trigger_depth`` bounds chains of triggers feeding each other.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from facade import inputs, models
from facade.caller_context import CallerContext
from facade.descriptors import compile_descriptors_to_jsonpath

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 3


def max_depth() -> int:
    return int(getattr(settings, "TRIGGER_MAX_DEPTH", DEFAULT_MAX_DEPTH))


def compile_conditions(conditions: list[Any]) -> tuple[list[dict], str | None]:
    """``(stored conditions, compiled JSONPath)``; raises ``ValueError`` on a malformed one."""
    from rekuest_core.inputs.models import RequiresInputModel

    parsed = [c if isinstance(c, RequiresInputModel) else RequiresInputModel.model_validate(c) for c in conditions or []]
    return [c.model_dump(mode="json") for c in parsed], compile_descriptors_to_jsonpath(parsed)


def target_port(action: models.Action, port: str, identifier: str) -> models.ArgPort:
    """The top-level STRUCTURE arg ``port`` of ``action`` taking ``identifier``, or ``ValueError``."""
    found = models.ArgPort.objects.filter(action=action, parent__isnull=True, key=port).first()
    if found is None:
        raise ValueError(f"The action has no argument {port!r}")
    if found.kind != "STRUCTURE" or found.identifier != identifier:
        raise ValueError(f"Argument {port!r} takes {found.kind} {found.identifier or ''}, not a {identifier} structure")
    return found


_MATCH_SQL = """
SELECT t.id
FROM facade_trigger t
LEFT JOIN facade_argport p ON p.action_id = t.action_id AND p.parent_id IS NULL AND p.key = t.port
WHERE t.id = ANY(%(ids)s)
  AND (t.compiled_jsonpath IS NULL OR jsonb_path_match(%(descriptors)s::jsonb, t.compiled_jsonpath::jsonpath, '{}'::jsonb, true) IS TRUE)
  AND (p.compiled_jsonpath IS NULL OR jsonb_path_match(%(descriptors)s::jsonb, p.compiled_jsonpath::jsonpath, '{}'::jsonb, true) IS TRUE)
ORDER BY t.id
"""


def matching_triggers(signal: models.Signal) -> list[models.Trigger]:
    """The enabled triggers of the signal's organization this signal fires."""
    import json

    candidates = list(
        models.Trigger.objects.filter(enabled=True, kind=signal.kind, identifier=signal.identifier, caller__organization_id=signal.organization_id).values_list("pk", flat=True)
    )
    if not candidates:
        return []
    with connection.cursor() as cursor:
        cursor.execute(_MATCH_SQL, {"ids": candidates, "descriptors": json.dumps(signal.descriptors or {})})
        ids = [row[0] for row in cursor.fetchall()]
    by_id = {t.pk: t for t in models.Trigger.objects.filter(pk__in=ids).select_related("caller__user", "caller__client", "caller__organization")}
    return [by_id[pk] for pk in ids if pk in by_id]


def _assign_input(trigger: models.Trigger, signal: models.Signal) -> inputs.AssignInputModel:
    target = {"agent": str(trigger.agent_id), "interface": trigger.interface} if trigger.agent_id else {"action": str(trigger.action_id)}
    args = {**(trigger.args or {}), trigger.port: {"__identifier": signal.identifier, "object": signal.object}}
    return inputs.AssignInputModel(
        **target,
        args=args,
        reference=f"trigger:{trigger.pk}:{signal.pk}",
        parent=str(signal.causing_task_id) if signal.causing_task_id else None,
    )


def _record(trigger: models.Trigger, error: str | None) -> None:
    if error is None:
        if trigger.consecutive_failures or trigger.last_error:
            trigger.consecutive_failures, trigger.last_error = 0, None
            trigger.save(update_fields=["consecutive_failures", "last_error", "updated_at"])
        return
    trigger.consecutive_failures += 1
    trigger.last_error = error
    trigger.save(update_fields=["consecutive_failures", "last_error", "updated_at"])
    logger.warning("Trigger %s did not fire: %s", trigger.pk, error)


def fire_one(signal_id: int) -> int:
    """Match and fire one unprocessed signal; the number of runs created."""
    from facade.backend import controll_backend  # lazy: backend imports the models graph

    with transaction.atomic():
        signal = (
            models.Signal.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("causing_task", "organization")
            .filter(pk=signal_id, processed_at__isnull=True)
            .first()
        )
        if signal is None:
            return 0
        depth = signal.causing_task.trigger_depth + 1 if signal.causing_task is not None else 1
        created_runs = 0
        for trigger in matching_triggers(signal):
            if depth > max_depth():
                _record(trigger, f"Not fired for signal {signal.pk}: {depth} triggers deep (limit {max_depth()}) — a trigger loop?")
                continue
            caller = trigger.caller
            context = CallerContext(user=caller.user, client=caller.client, organization=caller.organization, roles=[])
            try:
                with transaction.atomic():
                    _, created = controll_backend.assign_with_status(context, _assign_input(trigger, signal), signal=signal, trigger=trigger, trigger_depth=depth)
            except Exception as error:  # the target is broken, not the signal: record, move on
                _record(trigger, f"Could not run for signal {signal.pk}: {error}")
                continue
            created_runs += int(created)
            _record(trigger, None)
        signal.processed_at = timezone.now()
        signal.save(update_fields=["processed_at"])
        return created_runs


def fire_triggers_sync(limit: int = 100) -> int:
    """One pass over the unprocessed signals, oldest first. Returns the runs created."""
    pending = list(models.Signal.objects.filter(processed_at__isnull=True).order_by("received_at").values_list("pk", flat=True)[:limit])
    return sum(fire_one(pk) for pk in pending)

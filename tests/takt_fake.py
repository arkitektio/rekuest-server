"""A stand-in for takt's internal API in the Python test suite.

takt owns agent rows, registration and drawers; its behaviour is tested in rekuest-takt.
The Python tests only need what the Python code around those calls relies on: that an ensure
yields an agent row with the requested transport fields, and that an implement yields one
Action + Implementation per declared implementation. Anything else is refused loudly, so a test
that starts depending on more of takt shows up here instead of passing on a fiction.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Dict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from authentikate.models import Client, Organization, User

from django.db import IntegrityError, transaction
from django.utils import timezone

from facade import enums, models

#: Every request the fake served, in order: what the Python side asked takt to do.
calls: list[tuple[str, Dict[str, Any]]] = []


def _identity(principal: Dict[str, Any]) -> tuple[Client, User, Organization]:
    return (
        Client.objects.select_related("release__app").get(pk=principal["client"]),
        User.objects.get(pk=principal["user"]),
        Organization.objects.get(pk=principal["organization"]),
    )


def _ensure(principal: Dict[str, Any], name: str | None = None) -> models.Agent:
    client, user, organization = _identity(principal)
    agent, _ = models.Agent.objects.get_or_create(
        client=client,
        user=user,
        organization=organization,
        defaults={"name": name or client.client_id, "app": client.release.app, "release": client.release},
    )
    models.MemoryShelve.objects.get_or_create(agent=agent, defaults={"name": f"{agent.name} memory shelve", "creator": user, "organization": organization})
    return agent


def _assign(payload: Dict[str, Any]) -> Dict[str, Any]:
    """A Task row for the request, once per (caller, reference); an unknown target is refused."""
    client, user, organization = _identity(payload["principal"])
    caller, _ = models.Caller.objects.get_or_create(client=client, user=user, organization=organization)
    request = payload["input"]
    reference = request.get("reference")
    if reference:
        existing = models.Task.objects.filter(caller=caller, reference=reference).first()
        if existing is not None:
            return {"task": str(existing.pk), "reference": reference, "created": False}
    if request.get("agent"):
        implementation = models.Implementation.objects.select_related("action", "agent").filter(agent_id=request["agent"], interface=request.get("interface")).first()
    else:
        implementation = models.Implementation.objects.select_related("action", "agent").filter(action_id=request.get("action")).first()
    if implementation is None:
        raise ValueError("No implementation found for this assignment")
    parent = models.Task.objects.filter(pk=request["parent"]).first() if request.get("parent") else None
    schedule = models.Schedule.objects.filter(pk=payload.get("schedule")).first() if payload.get("schedule") else None
    try:
        with transaction.atomic():
            task = models.Task.objects.create(
                action=implementation.action,
                implementation=implementation,
                agent=implementation.agent,
                caller=caller,
                args=request.get("args") or {},
                reference=reference or "",
                parent=parent,
                root=(parent.root or parent) if parent is not None else None,
                not_before=request.get("not_before"),
                schedule=schedule,
                signal_id=payload.get("signal"),
                trigger_id=payload.get("trigger"),
                trigger_depth=payload.get("trigger_depth", 0),
                ephemeral=bool(schedule and schedule.ephemeral_runs),
                latest_event_kind=enums.TaskEventKind.QUEUED,
                latest_instruct_kind=enums.TaskInstructKind.ASSIGN,
            )
    except IntegrityError:  # the same reference, raced in
        existing = models.Task.objects.get(caller=caller, reference=reference)
        return {"task": str(existing.pk), "reference": reference, "created": False}
    return {"task": str(task.pk), "reference": task.reference, "created": True}


def _cancel(payload: Dict[str, Any]) -> Dict[str, Any]:
    """A run never handed over is settled CANCELLED at once, as takt does for a delayed task."""
    task = models.Task.objects.get(pk=payload["task"])
    if not task.is_done and task.dispatch_attempts == 0:
        models.Task.objects.filter(pk=task.pk).update(is_done=True, latest_event_kind=enums.TaskEventKind.CANCELLED, finished_at=timezone.now())
    return {"task": str(task.pk)}


def _validate_timing(payload: Dict[str, Any]) -> None:
    """takt's judgment of a timing, as far as its wording goes: its cron parser is not here."""
    interval, cron, zone = payload.get("interval_seconds"), payload.get("cron"), payload.get("timezone", "UTC")
    if (interval is None) == (cron is None):
        raise ValueError("A schedule needs exactly one of interval_seconds or cron")
    if interval is not None and interval < 1:
        raise ValueError("interval_seconds must be at least 1")
    if cron is not None and not re.fullmatch(r"\s*([\d*/,\-A-Za-z#L]+\s+){4}[\d*/,\-A-Za-z#L]+\s*", cron):
        raise ValueError(f'Not a valid cron line: "{cron}"')
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f'Unknown timezone: "{zone}"') from error


def _waiting_run(schedule: models.Schedule) -> "models.Task | None":
    return models.Task.objects.filter(schedule=schedule, is_done=False).order_by("pk").first()


def _plan(schedule: models.Schedule) -> bool:
    """One waiting run a minute from now, as takt's refill would leave it."""
    if not schedule.enabled or _waiting_run(schedule) is not None:
        return False
    caller = schedule.caller
    target = {"agent": str(schedule.agent_id), "interface": schedule.interface} if schedule.agent_id else {"action": str(schedule.action_id)}
    slot = timezone.now() + timedelta(minutes=1)
    request = {**target, "args": schedule.args or {}, "reference": f"schedule:{schedule.pk}:{slot.isoformat()}", "not_before": slot}
    principal = {"user": caller.user_id, "client": caller.client_id, "organization": caller.organization_id, "roles": []}
    return bool(_assign({"principal": principal, "input": request, "schedule": schedule.pk})["created"])


def _schedule_op(op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    schedule = models.Schedule.objects.select_related("caller").get(pk=payload["schedule"])
    waiting = _waiting_run(schedule)
    if op == "schedule/trigger":
        if waiting is None:
            return {"task": _assign({"principal": {"user": schedule.caller.user_id, "client": schedule.caller.client_id, "organization": schedule.caller.organization_id}, "input": {**({"agent": str(schedule.agent_id), "interface": schedule.interface} if schedule.agent_id else {"action": str(schedule.action_id)}), "args": schedule.args or {}, "reference": f"schedule:{schedule.pk}:manual:{len(calls)}"}, "schedule": schedule.pk})["task"]}
        if waiting.dispatch_attempts != 0:
            raise ValueError("A run of this schedule is already executing")
        models.Task.objects.filter(pk=waiting.pk).update(not_before=timezone.now())
        return {"task": str(waiting.pk)}
    if op == "schedule/cancel-waiting" or payload.get("replan"):
        if waiting is not None:
            _cancel({"task": waiting.pk})
    if op == "schedule/plan":
        return {"planned": _plan(schedule)}
    return {}


def call(op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Serve one internal API request as takt would, as far as the Python tests need."""
    calls.append((op, payload))
    if op == "assign":
        return _assign(payload)
    if op == "cancel":
        return _cancel(payload)
    if op == "schedule/validate":
        _validate_timing(payload)
        return {}
    if op.startswith("schedule/"):
        return _schedule_op(op, payload)
    if op == "agent/ensure":
        agent = _ensure(payload["principal"], payload.get("name"))
        fields = [f for f in ("description", "kind", "hook_url", "hook_url_secret") if f in payload]
        for field in fields:
            setattr(agent, field, payload[field])
        if fields:
            agent.save(update_fields=fields)
        if payload.get("clear_drawers"):
            models.MemoryDrawer.objects.filter(shelve__agent=agent).delete()
        return {"agent": str(agent.pk)}
    if op == "agent/delete":
        models.Agent.objects.get(pk=payload["agent"], organization_id=payload["principal"]["organization"]).delete()
        return {"agent": str(payload["agent"])}
    if op == "implementation/delete":
        models.Implementation.objects.get(pk=payload["implementation"], agent__organization_id=payload["principal"]["organization"]).delete()
        return {"implementation": str(payload["implementation"])}
    if op == "action/cleanup":
        unreferenced = models.Action.objects.filter(organization_id=payload["principal"]["organization"], implementations__isnull=True)
        if payload.get("actions"):
            unreferenced = unreferenced.filter(pk__in=payload["actions"])
        ids = list(unreferenced.values_list("pk", flat=True))
        models.Action.objects.filter(pk__in=ids).delete()
        return {"deleted": len(ids)}
    if op == "agent/implement":
        agent = _ensure(payload["principal"])
        declaration = payload["input"]
        for implementation in declaration.get("implementations") or []:
            definition = implementation["definition"]
            action, _ = models.Action.objects.get_or_create(
                app=agent.app,
                organization=agent.organization,
                key=definition["key"],
                version=definition.get("version", "1"),
                defaults={"name": definition["name"], "hash": f"fake-{definition['key']}", "kind": definition.get("kind", "FUNCTION"), "description": definition.get("description") or ""},
            )
            models.Implementation.objects.update_or_create(agent=agent, interface=implementation["interface"], defaults={"action": action, "needs_token": implementation.get("needs_token", True)})
        if declaration.get("name"):
            agent.name = declaration["name"]
            agent.save(update_fields=["name"])
        return {"agent": str(agent.pk), "diagnostics": []}
    raise NotImplementedError(f"the takt fake does not serve {op}")

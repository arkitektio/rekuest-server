"""This hub's services as HookAgents: provisioned by rekuest itself, scheduled by the reaper.

Each entry of ``rekuest.service_agents`` becomes:

* an **agent** (``kind=WEBHOOK``) with its own identity (user, app, client) in the
  ``service_agents_organization`` — minted here, because lok has no service identity to offer;
* its **actions**, read from the service's signed manifest (``GET <hook_url>/manifest``,
  served by the vendored ``rekuest_service`` package) and registered through the ordinary
  :func:`facade.registration.implement_agent`, so they are real actions like any agent's;
* one **schedule** per action that declares a default interval or cron line, owned by the
  scheduler identity. Its runs are ephemeral: housekeeping, not history.

Provisioning is a reaper step (:meth:`ReconcileMixin.provision_service_agents`), idempotent,
and updates everything in place — an agent is never deleted and recreated, since its schedules
cascade with it. An operator's changes to a schedule (``enabled``) survive re-provisioning; only
its timing follows the manifest. The scheduler's caller has its OWN client, distinct from every
service's: the caller-event mirror POSTs a task's events to a HookAgent sharing its caller's
identity, and a service must not receive echoes of its own runs.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from authentikate.models import App, Client, Membership, Organization, Release, User
from django.conf import settings
from django.db import transaction

from facade import enums, hooks, models, registration, schedules

logger = logging.getLogger(__name__)

ISSUER = "rekuest"
_TIMEOUT = 5.0

# Re-read every service's manifest this often. A reaper ticks every few seconds; a manifest
# changes with a deploy. The throttle is process-local on purpose — it only saves requests;
# whichever reaper provisions, the result is the same.
PROVISION_EVERY_SECONDS = 300
# ...but a service that could not be reached (still booting, restarting) is retried sooner.
PROVISION_RETRY_SECONDS = 30
_next_provision_at = 0.0


def _identity(name: str, organization: Organization) -> tuple[User, Client]:
    """A rekuest-minted identity (user + app + release + client), member of ``organization``."""
    user, _ = User.objects.get_or_create(username=f"rekuest-{name}", defaults=dict(sub=f"rekuest:{name}", iss=ISSUER))
    app, _ = App.objects.get_or_create(identifier=f"rekuest.{name}")
    release, _ = Release.objects.get_or_create(app=app, version="1")
    client, _ = Client.objects.get_or_create(client_id=f"rekuest:{name}", defaults=dict(release=release, iss=ISSUER, name=name))
    Membership.objects.get_or_create(user=user, organization=organization)
    return user, client


def _organization() -> Organization:
    organization, _ = Organization.objects.get_or_create(slug=settings.SERVICE_AGENTS_ORGANIZATION)
    return organization


def scheduler_caller() -> models.Caller:
    """The caller every service schedule's runs are assigned as."""
    organization = _organization()
    user, client = _identity("scheduler", organization)
    caller, _ = models.Caller.objects.get_or_create(client=client, user=user, organization=organization)
    return caller


def fetch_manifest(agent: models.Agent) -> dict[str, Any]:
    """The service's manifest: its actions and the signals it emits. Signed like a delivery, so the service can refuse strangers."""
    from facade import service_trust

    url = agent.hook_url.rstrip("/") + "/manifest"
    entry = service_trust.entry_for_agent(agent)
    if entry is None:
        raise ValueError(f"Agent {agent.pk} is not one of rekuest.service_agents")
    headers = {hooks.AGENT_HEADER: str(agent.pk), "Authorization": service_trust.sign_to(entry, "GET", url, b"")}
    response = httpx.get(url, headers=headers, timeout=_TIMEOUT)
    response.raise_for_status()
    manifest = response.json()
    # A service on an older rekuest-service copy declares no signals: that reads as "none".
    return {"actions": list(manifest.get("actions") or []), "signals": list(manifest.get("signals") or [])}


def _implementations(actions: list[dict[str, Any]]):
    from facade.mutations.agent import ImplementAgentInputModel
    from rekuest_core.enums import ActionKind
    from rekuest_core.inputs.models import DefinitionInputModel, ImplementationInputModel

    return [
        ImplementationInputModel(
            interface=action["interface"],
            # Nobody is behind a scheduled run to mint provenance for (see facade.provenance).
            needs_token=False,
            definition=DefinitionInputModel(
                key=action["interface"],
                name=action.get("name") or action["interface"],
                description=action.get("description"),
                kind=ActionKind.FUNCTION,
                # A service's sweep must be safe to run twice (rekuest-service's contract), which
                # also lets rekuest redeliver it after an ambiguous loss.
                idempotent=True,
            ),
        )
        for action in actions
    ], ImplementAgentInputModel


def provision(entry: dict[str, Any]) -> models.Agent:
    """Bring one service's agent, actions and default schedules in line with its manifest."""
    organization = _organization()
    service = entry["service"]
    user, client = _identity(f"service-{service}", organization)

    with transaction.atomic():
        agent = registration.ensure_agent(client, user, organization, name=service)
        # No secret: requests both ways are signed with instance keys (facade.service_trust).
        wanted = {"kind": enums.AgentKind.WEBHOOK.value, "hook_url": entry["hook_url"], "hook_url_secret": None}
        changed = [field for field, value in wanted.items() if getattr(agent, field) != value]
        for field in changed:
            setattr(agent, field, wanted[field])
        if changed:
            agent.save(update_fields=changed)

    manifest = fetch_manifest(agent)
    actions = manifest["actions"]
    implementations, payload_model = _implementations(actions)
    registration.implement_agent(client, user, organization, payload_model(name=service, description=f"The {service} service of this hub.", implementations=implementations))
    _sync_schedules(agent, actions)
    _sync_signals(agent, manifest["signals"])
    return agent


def _sync_schedules(agent: models.Agent, actions: list[dict[str, Any]]) -> None:
    caller = scheduler_caller()
    declared = set()
    for action in actions:
        interval, cron = action.get("default_interval"), action.get("default_cron")
        if interval is None and cron is None:
            continue
        declared.add(action["interface"])
        implementation = models.Implementation.objects.select_related("action").get(agent=agent, interface=action["interface"])
        schedule = models.Schedule.objects.filter(caller=caller, agent=agent, interface=action["interface"]).first()
        if schedule is None:
            models.Schedule.objects.create(
                name=f"{agent.name}.{action['interface']}",
                caller=caller,
                action=implementation.action,
                agent=agent,
                interface=action["interface"],
                interval_seconds=interval,
                cron=cron,
                ephemeral_runs=True,
            )
            continue
        if (schedule.interval_seconds, schedule.cron, schedule.action_id) != (interval, cron, implementation.action_id):
            schedule.interval_seconds, schedule.cron, schedule.action = interval, cron, implementation.action
            schedule.save(update_fields=["interval_seconds", "cron", "action", "updated_at"])
            schedules.cancel_waiting_run(schedule)  # re-planned with the new timing on the next refill

    # An action the service stopped declaring a default for: its schedule stops, but stays —
    # with its history — should the default come back.
    for schedule in models.Schedule.objects.filter(caller=caller, agent=agent, enabled=True).exclude(interface__in=declared):
        schedule.enabled = False
        schedule.save(update_fields=["enabled", "updated_at"])
        schedules.cancel_waiting_run(schedule)


def _sync_signals(agent: models.Agent, signals: list[dict[str, Any]]) -> None:
    """Make the agent's SignalDeclarations exactly what its manifest declares (one row per kind)."""
    declared = set()
    for signal in signals:
        for kind in signal.get("kinds") or ["CREATED"]:
            declared.add((signal["identifier"], kind))
            models.SignalDeclaration.objects.update_or_create(
                agent=agent,
                identifier=signal["identifier"],
                kind=kind,
                defaults={"descriptor_keys": list(signal.get("descriptors") or []), "description": signal.get("description")},
            )
    for row in models.SignalDeclaration.objects.filter(agent=agent):
        if (row.identifier, row.kind) not in declared:
            row.delete()


def provision_all(force: bool = False) -> int:
    """Provision every configured service (throttled to PROVISION_EVERY_SECONDS). Returns how many succeeded."""
    global _next_provision_at
    entries = getattr(settings, "SERVICE_AGENTS", None) or []
    if not entries or (not force and time.monotonic() < _next_provision_at):
        return 0
    provisioned = 0
    for entry in entries:
        try:
            provision(entry)
            provisioned += 1
        except Exception as error:  # one unreachable service must not keep the others unprovisioned
            logger.warning("Could not provision service agent %r: %s", entry.get("service"), error)
    _next_provision_at = time.monotonic() + (PROVISION_EVERY_SECONDS if provisioned == len(entries) else PROVISION_RETRY_SECONDS)
    return provisioned

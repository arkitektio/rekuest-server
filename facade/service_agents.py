"""This hub's services and their HookAgents: provisioned by rekuest itself, scheduled by takt.

Two different things come out of each entry of ``rekuest.service_agents``, both read from the
service's signed manifest (``GET <hook_url>/manifest``, served by the vendored
``rekuest_service`` package):

* the **service** (:class:`facade.models.Service`): the structures it hosts, with their
  descriptors, and the signals it emits. Catalog rows, hub-wide — the same for every
  organization, and no agent is involved;
* its **HookAgent**, when the service offers actions: an agent (``kind=WEBHOOK``) with an
  identity rekuest mints (user, app, client — lok has no service identity to offer), its
  actions registered through takt's ordinary registration, and one **schedule** per action
  that declares a default interval or cron line.

Agents and schedules are an organization's own: every organization gets each service's
HookAgent — when the organization is created, and on every provisioning pass for those that
came to exist some other way — and its members see it, run its actions and switch its
schedules on and off like any other agent's. There is no internal organization. A schedule's
runs are assigned as the organization's scheduler identity and are ephemeral: housekeeping, not
history.

Provisioning is an upkeep job takt asks for (:mod:`facade.upkeep`), idempotent, and updates
everything in place — an agent is never deleted and recreated, since its schedules
cascade with it. An organization's changes to a schedule (``enabled``) survive re-provisioning;
only its timing follows the manifest. The scheduler's caller has its OWN client, distinct from
every service's: the caller-event mirror POSTs a task's events to a HookAgent sharing its
caller's identity, and a service must not receive echoes of its own runs.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx
from authentikate.models import App, Client, Membership, Organization, Release, User
from django.conf import settings
from django.db import connection

from facade import takt, enums, models, schedules
from facade.caller_context import CallerContext

logger = logging.getLogger(__name__)

ISSUER = "rekuest"
_TIMEOUT = 5.0

# One provisioning pass at a time, across every replica: a schedule is found-or-created, and two
# passes at once would both create it. A session-level advisory lock, not a transaction: a pass
# asks takt to write rows that must see what this one already committed.
PROVISION_LOCK_KEY = int.from_bytes(hashlib.sha256(b"rekuest:service-agents").digest()[:8], "big", signed=True)


def _identity(name: str, organization: Organization) -> tuple[User, Client]:
    """A rekuest-minted identity (user + app + release + client), member of ``organization``."""
    user, _ = User.objects.get_or_create(username=f"rekuest-{name}", defaults=dict(sub=f"rekuest:{name}", iss=ISSUER))
    app, _ = App.objects.get_or_create(identifier=f"rekuest.{name}")
    release, _ = Release.objects.get_or_create(app=app, version="1")
    client, _ = Client.objects.get_or_create(client_id=f"rekuest:{name}", defaults=dict(release=release, iss=ISSUER, name=name))
    Membership.objects.get_or_create(user=user, organization=organization)
    return user, client


def scheduler_caller(organization: Organization) -> models.Caller:
    """The caller an organization's service schedules run as."""
    user, client = _identity("scheduler", organization)
    caller, _ = models.Caller.objects.get_or_create(client=client, user=user, organization=organization)
    return caller


def fetch_manifest(entry: dict[str, Any]) -> dict[str, Any]:
    """A service's manifest: the structures it hosts, the signals it emits, and its HookAgent's
    actions. Signed, so the service can refuse strangers."""
    from facade import service_trust

    url = entry["hook_url"].rstrip("/") + "/manifest"
    response = httpx.get(url, headers={"Authorization": service_trust.sign_to(entry, "GET", url, b"")}, timeout=_TIMEOUT)
    response.raise_for_status()
    manifest = response.json()
    # A service on an older rekuest-service copy declares no signals: that reads as "none". It
    # cannot say what it hosts either, and that reads as "unknown" (None), not as "nothing":
    # rolling a service back must not empty the catalog.
    structures = manifest.get("structures")
    return {
        "identifier": manifest.get("identifier"),
        "description": manifest.get("description"),
        "actions": list(manifest.get("actions") or []),
        "signals": list(manifest.get("signals") or []),
        "structures": None if structures is None else list(structures),
    }


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


def provision_service(entry: dict[str, Any], manifest: dict[str, Any]) -> models.Service:
    """Bring the service's catalog rows — itself, its structures, its signals — in line with its manifest."""
    service, _ = models.Service.objects.update_or_create(name=entry["service"], defaults={"identifier": manifest["identifier"], "description": manifest["description"]})
    _sync_structures(service, manifest["structures"])
    _sync_signals(service, manifest["signals"])
    return service


def provision_agent(entry: dict[str, Any], manifest: dict[str, Any], organization: Organization) -> models.Agent | None:
    """Bring ``organization``'s HookAgent of this service, its actions and default schedules in
    line with the manifest. A service that offers no actions has no agent."""
    service = entry["service"]
    user, client = _identity(f"service-{service}", organization)
    actions = manifest["actions"]
    if not actions and not models.Agent.objects.filter(client=client, user=user, organization=organization).exists():
        return None

    principal = takt._principal(CallerContext(user=user, client=client, organization=organization))
    # No secret: requests both ways are signed with instance keys (facade.service_trust).
    ensured = takt.call(
        "agent/ensure",
        {"principal": principal, "name": service, "kind": enums.AgentKind.WEBHOOK.value, "hook_url": entry["hook_url"], "hook_url_secret": None},
    )
    agent = models.Agent.objects.select_related("client").get(pk=ensured["agent"])
    implementations, payload_model = _implementations(actions)
    payload = payload_model(name=service, description=f"The agent of the {service} service: the work it can be asked to do.", implementations=implementations)
    takt.call("agent/implement", {"principal": principal, "input": payload.model_dump(mode="json", exclude_none=True)})
    _sync_schedules(agent, actions)
    return agent


#: Where every service agent used to live, before organizations got their own. Its agents are
#: retired (their schedules go with them) and it gets no new ones.
RETIRED_ORGANIZATION = "rekuest-system"


def _retire_internal_agents() -> None:
    """Delete the service agents of the former internal organization; a no-op once they are gone."""
    from facade import service_trust

    stale = models.Agent.objects.filter(organization__slug=RETIRED_ORGANIZATION, client__client_id__startswith=service_trust.SERVICE_CLIENT_PREFIX)
    for agent in stale.select_related("user", "client", "organization"):
        principal = takt._principal(CallerContext(user=agent.user, client=agent.client, organization=agent.organization))
        takt.call("agent/delete", {"principal": principal, "agent": str(agent.pk)})
        logger.info("Retired the %s agent of the internal organization", agent.name)


def provision(entry: dict[str, Any], organizations: list[Organization] | None = None) -> models.Service:
    """One service: its catalog rows, and its HookAgent in every organization (or just ``organizations``)."""
    manifest = fetch_manifest(entry)
    service = provision_service(entry, manifest)
    for organization in Organization.objects.exclude(slug=RETIRED_ORGANIZATION) if organizations is None else organizations:
        provision_agent(entry, manifest, organization)
    return service


def _sync_schedules(agent: models.Agent, actions: list[dict[str, Any]]) -> None:
    caller = scheduler_caller(agent.organization)
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


def _sync_signals(service: models.Service, signals: list[dict[str, Any]]) -> None:
    """Make the service's SignalDeclarations exactly what its manifest declares (one row per kind)."""
    declared = set()
    for signal in signals:
        for kind in signal.get("kinds") or ["CREATED"]:
            declared.add((signal["identifier"], kind))
            models.SignalDeclaration.objects.update_or_create(
                service=service,
                identifier=signal["identifier"],
                kind=kind,
                defaults={"descriptor_keys": list(signal.get("descriptors") or []), "description": signal.get("description")},
            )
    for row in models.SignalDeclaration.objects.filter(service=service):
        if (row.identifier, row.kind) not in declared:
            row.delete()


def _sync_structures(service: models.Service, structures: list[dict[str, Any]] | None) -> None:
    """Make the structures the service hosts exactly what its manifest declares.

    An identifier another service already hosts stays that service's: the claim is skipped with
    a warning, and the rest of the manifest still applies.
    """
    if structures is None:
        return
    declared = set()
    for structure in structures:
        identifier = structure["identifier"]
        holder = models.StructureDeclaration.objects.filter(identifier=identifier).exclude(service=service).select_related("service").first()
        if holder is not None:
            logger.warning("%s declares the structure %s, which %s already hosts; ignored", service.name, identifier, holder.service.name)
            continue
        declared.add(identifier)
        models.StructureDeclaration.objects.update_or_create(
            identifier=identifier,
            defaults={"service": service, "label": structure.get("label"), "description": structure.get("description"), "descriptors": list(structure.get("descriptors") or [])},
        )
    models.StructureDeclaration.objects.filter(service=service).exclude(identifier__in=declared).delete()


def provision_all(organizations: list[Organization] | None = None) -> list[str] | None:
    """Provision every configured service once; the services that could not be.

    Each service's catalog rows, and its HookAgent in every organization — or only in
    ``organizations`` (one that was just created). ``None`` when another replica is provisioning
    right now (nothing was done here). How often this runs is takt's to decide
    (:mod:`facade.upkeep`).
    """
    entries = getattr(settings, "SERVICE_AGENTS", None) or []
    if not entries:
        return []
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [PROVISION_LOCK_KEY])
        if not cursor.fetchone()[0]:
            return None
    try:
        failed = []
        try:
            _retire_internal_agents()
        except Exception as error:  # noqa: BLE001  retried on the next pass
            logger.warning("Could not retire the internal organization's service agents: %s", error)
        for entry in entries:
            try:
                provision(entry, organizations)
            except Exception as error:  # one unreachable service must not keep the others unprovisioned
                logger.warning("Could not provision the service %r: %s", entry.get("service"), error)
                failed.append(str(entry.get("service")))
        return failed
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [PROVISION_LOCK_KEY])


def provision_new_organization(organization: Organization) -> None:
    """Give an organization that was just created its HookAgents now, not at the next pass.

    Best effort, off the request: an unreachable service, or a pass already running, leaves it
    to the next provisioning pass, which covers every organization anyway.
    """
    import threading

    from django.db import close_old_connections

    if not (getattr(settings, "SERVICE_AGENTS", None) or []) or organization.slug == RETIRED_ORGANIZATION:
        return

    def run() -> None:
        try:
            provision_all([organization])
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not provision the HookAgents of the new organization %s: %s", organization.slug, error)
        finally:
            close_old_connections()

    threading.Thread(target=run, name=f"provision-{organization.slug}", daemon=True).start()

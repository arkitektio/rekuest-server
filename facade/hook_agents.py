"""This hub's HookAgents: agents reached over HTTP, provisioned by rekuest itself.

A hook agent (``rekuest.hook_agents``: a name and its hook URL) is an agent like any app's,
except that rekuest POSTs it its Assigns and mints its identity (user, app, client — lok has
none to offer). What it offers comes from its signed manifest (``GET <hook_url>/manifest``,
served by the ``rekuest_hook`` package): its actions, registered through takt's ordinary
registration.

It is not a service and is not tied to one. A service says what exists
(:mod:`facade.service_catalog`); an agent says what can be done. A hook agent may run in a
service's process or anywhere else on the hub, and this pass reads and writes nothing of the
catalog.

Agents are an organization's own: every organization gets each hook agent — when the
organization is created, and on every provisioning pass for those that came to exist some other
way — and its members see it and run its actions like any other agent's. There is no internal
organization.

Nothing is wired. Provisioning registers actions and stops there: it creates no schedule and no
trigger. When an action runs is the organization's own automation, set up by its users.

The pass is idempotent and updates everything in place — an agent is never deleted and
recreated, since what users attached to it (schedules, triggers, pins) cascades with it.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import httpx
from authentikate.models import App, Client, Membership, Organization, Release, User
from django.conf import settings

from facade import enums, models, takt
from facade.caller_context import CallerContext

logger = logging.getLogger(__name__)

ISSUER = "rekuest"
_TIMEOUT = 5.0

#: An organization that used to hold every service's agent, before organizations got their own.
#: It gets no agents.
RETIRED_ORGANIZATION = "rekuest-system"

#: How rekuest named the clients it minted while a hook agent was taken to be "the agent of a
#: service". Agents under such a client are retired; what replaces them is a hook agent's own.
FORMER_CLIENT_PREFIX = "rekuest:service-"


def _identity(name: str, organization: Organization) -> tuple[User, Client]:
    """A rekuest-minted identity (user + app + release + client), member of ``organization``."""
    user, _ = User.objects.get_or_create(username=f"rekuest-{name}", defaults=dict(sub=f"rekuest:{name}", iss=ISSUER))
    app, _ = App.objects.get_or_create(identifier=f"rekuest.{name}")
    release, _ = Release.objects.get_or_create(app=app, version="1")
    client, _ = Client.objects.get_or_create(client_id=f"rekuest:{name}", defaults=dict(release=release, iss=ISSUER, name=name))
    Membership.objects.get_or_create(user=user, organization=organization)
    return user, client


def fetch_manifest(entry: dict[str, Any]) -> dict[str, Any]:
    """A hook agent's manifest: what it says it is, and its actions. Signed, so the agent can
    refuse strangers."""
    from facade import service_trust

    url = entry["hook_url"].rstrip("/") + "/manifest"
    response = httpx.get(url, headers={"Authorization": service_trust.sign_to(entry, "GET", url, b"")}, timeout=_TIMEOUT)
    response.raise_for_status()
    manifest = response.json()
    return {"description": manifest.get("description"), "actions": list(manifest.get("actions") or [])}


def _implementations(actions: list[dict[str, Any]]):
    from facade.mutations.agent import ImplementAgentInputModel
    from rekuest_core.enums import ActionKind
    from rekuest_core.inputs.models import DefinitionInputModel, ImplementationInputModel

    return [
        ImplementationInputModel(
            interface=action["interface"],
            needs_token=False,
            definition=DefinitionInputModel(
                key=action["interface"],
                name=action.get("name") or action["interface"],
                description=action.get("description"),
                kind=ActionKind.FUNCTION,
                # A hook action must be safe to run twice (rekuest-hook's contract), which also
                # lets rekuest redeliver it after an ambiguous loss.
                idempotent=True,
            ),
        )
        for action in actions
    ], ImplementAgentInputModel


def provision_agent(entry: dict[str, Any], manifest: dict[str, Any], organization: Organization) -> models.Agent | None:
    """Bring ``organization``'s agent and its actions in line with the manifest. An agent that
    offers nothing (and never did) is not created."""
    from facade import service_trust

    name = entry["name"]
    user, client = _identity(f"{service_trust.HOOK_IDENTITY_PREFIX}{name}", organization)
    actions = manifest["actions"]
    if not actions and not models.Agent.objects.filter(client=client, user=user, organization=organization).exists():
        return None

    principal = takt._principal(CallerContext(user=user, client=client, organization=organization))
    # No secret: requests both ways are signed with instance keys (facade.service_trust).
    ensured = takt.call(
        "agent/ensure",
        {"principal": principal, "name": name, "kind": enums.AgentKind.WEBHOOK.value, "hook_url": entry["hook_url"], "hook_url_secret": None},
    )
    implementations, payload_model = _implementations(actions)
    payload = payload_model(name=name, description=manifest.get("description") or f"The {name} hook agent.", implementations=implementations)
    takt.call("agent/implement", {"principal": principal, "input": payload.model_dump(mode="json", exclude_none=True)})
    return models.Agent.objects.get(pk=ensured["agent"])


def provision(entry: dict[str, Any], organizations: list[Organization] | None = None) -> None:
    """One hook agent, in every organization (or just ``organizations``)."""
    manifest = fetch_manifest(entry)
    for organization in Organization.objects.exclude(slug=RETIRED_ORGANIZATION) if organizations is None else organizations:
        provision_agent(entry, manifest, organization)


def retire_former_agents() -> None:
    """Delete the agents rekuest minted as "a service's agent"; a no-op once they are gone.

    What was attached to them goes with them — the schedules rekuest used to create by itself
    among it. Nothing replaces those: wiring is the organization's.
    """
    stale = models.Agent.objects.filter(client__client_id__startswith=FORMER_CLIENT_PREFIX)
    for agent in stale.select_related("user", "client", "organization"):
        principal = takt._principal(CallerContext(user=agent.user, client=agent.client, organization=agent.organization))
        takt.call("agent/delete", {"principal": principal, "agent": str(agent.pk)})
        logger.info("Retired the former service agent %s of %s", agent.name, agent.organization.slug)


def provision_all(organizations: list[Organization] | None = None) -> list[str]:
    """Provision every configured hook agent once; the names of those that could not be."""
    failed = []
    try:
        retire_former_agents()
    except Exception as error:  # noqa: BLE001  retried on the next pass
        logger.warning("Could not retire the former service agents: %s", error)
    for entry in getattr(settings, "HOOK_AGENTS", None) or []:
        try:
            provision(entry, organizations)
        except Exception as error:  # one unreachable agent must not keep the others unprovisioned
            logger.warning("Could not provision the hook agent %r: %s", entry.get("name"), error)
            failed.append(str(entry.get("name")))
    return failed


def provision_new_organization(organization: Organization) -> None:
    """Give an organization that was just created its hook agents now, not at the next pass.

    Best effort, off the request: an unreachable agent, or a pass already running, leaves it
    to the next provisioning pass, which covers every organization anyway.
    """
    from django.db import close_old_connections

    from facade import provisioning

    if not (getattr(settings, "HOOK_AGENTS", None) or []) or organization.slug == RETIRED_ORGANIZATION:
        return

    def run() -> None:
        try:
            provisioning.provision_hook_agents([organization])
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not provision the hook agents of the new organization %s: %s", organization.slug, error)
        finally:
            close_old_connections()

    threading.Thread(target=run, name=f"provision-{organization.slug}", daemon=True).start()

"""A stand-in for agentd's internal API in the Python test suite.

agentd owns agent rows, registration and drawers; its behaviour is tested in rekuest-agentd.
The Python tests only need what the Python code around those calls relies on: that an ensure
yields an agent row with the requested transport fields, and that an implement yields one
Action + Implementation per declared implementation. Anything else is refused loudly, so a test
that starts depending on more of agentd shows up here instead of passing on a fiction.
"""

from __future__ import annotations

from typing import Any, Dict

from authentikate.models import Client, Organization, User

from facade import models


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


def call(op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
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
            models.Implementation.objects.update_or_create(agent=agent, interface=implementation["interface"], defaults={"action": action, "release": agent.release, "needs_token": implementation.get("needs_token", True)})
        if declaration.get("name"):
            agent.name = declaration["name"]
            agent.save(update_fields=["name"])
        return {"agent": str(agent.pk), "diagnostics": []}
    raise NotImplementedError(f"the agentd fake does not serve {op}")

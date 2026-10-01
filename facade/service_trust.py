"""Trust between rekuest and this hub's services: instance keys, no shared secrets.

Every request between rekuest and a service agent (``rekuest.service_agents``) — Assign
deliveries, manifest fetches, the service's reports, its signals — carries a short-lived JWT
signed with the sender's instance key (:mod:`rekuest_service.trust`, vendored). The receiver
checks it against the hub's trust bundle: the coord-vouched public keys of every instance,
each listed under its service's identifier. A service can therefore only speak as itself, and
only rekuest can speak as rekuest.

Third-party HookAgents (registered through ``ensureAgent`` with a ``hook_url_secret``) keep the
HMAC scheme (agentd's ``hooks``); only the hub's own services use keys.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from rekuest_service import trust

#: Service agents' clients are minted by rekuest as ``rekuest:service-<name>`` (see
#: :mod:`facade.service_agents`); that is how an agent is recognised as one.
SERVICE_CLIENT_PREFIX = "rekuest:service-"


def rekuest_identifier() -> str:
    return getattr(settings, "REKUEST_IDENTIFIER", None) or "live.arkitekt.rekuest"


def entry_for(service: str) -> dict[str, Any] | None:
    """The ``service_agents`` entry named ``service``."""
    for entry in getattr(settings, "SERVICE_AGENTS", None) or []:
        if entry.get("service") == service:
            return entry
    return None


def identifier_of(entry: dict[str, Any]) -> str:
    """The identifier a service signs as (and is listed under in the trust bundle)."""
    return entry.get("identifier") or f"live.arkitekt.{entry['service']}"


def entry_for_agent(agent: Any) -> dict[str, Any] | None:
    """The ``service_agents`` entry of a service agent; None for any other agent."""
    client = getattr(agent, "client", None)
    client_id = getattr(client, "client_id", "") or ""
    if not client_id.startswith(SERVICE_CLIENT_PREFIX):
        return None
    return entry_for(client_id[len(SERVICE_CLIENT_PREFIX) :])


def sign_to(entry: dict[str, Any], method: str, url: str, body: bytes) -> str:
    """The ``Authorization`` value of a request from rekuest to this service."""
    return trust.sign(method, urlparse(url).path, body, issuer=rekuest_identifier(), audience=identifier_of(entry))

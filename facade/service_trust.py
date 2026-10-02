"""Trust between rekuest and this hub's services: instance keys, no shared secrets.

Every request between rekuest and a service agent (``rekuest.service_agents``) — Assign
deliveries, manifest fetches, the service's reports, its signals — carries a short-lived JWT
signed with the sender's instance key (:mod:`rekuest_service.trust`, vendored). The receiver
checks it against the hub's trust bundle: the coord-vouched public keys of every instance,
each listed under its service's identifier. A service can therefore only speak as itself, and
only rekuest can speak as rekuest.

Third-party HookAgents (registered through ``ensureAgent`` with a ``hook_url_secret``) keep the
HMAC scheme (takt's ``hooks``); only the hub's own services use keys.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from joserfc import jwt
from joserfc.jwk import KeySet
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


def verify_own(method: str, path: str, body: bytes, authorization: str | None, max_skew: int = 30) -> trust.Verified:
    """Check a request takt signed as this rekuest, to this rekuest; ``TrustError`` otherwise.

    takt reads the same ``config.yaml`` and so signs with this instance's own key. The check is
    against that key itself, not the hub's trust bundle (takt's ``service_trust::verify`` does the
    same for the requests this server sends it): the pair works before the coord has vouched for
    the key, and no other instance can pass.
    """
    key = trust.instance_key()
    if key is None:
        raise trust.TrustError("No instance key configured (settings.INSTANCE['PRIVATE_KEY'])")
    if not authorization or not authorization.startswith(f"{trust.SCHEME} "):
        raise trust.TrustError("No service token")
    token = authorization[len(trust.SCHEME) + 1 :].strip()
    try:
        decoded = jwt.decode(token, KeySet.import_key_set({"keys": [trust.public_jwk(key)]}), algorithms=[trust.ALGORITHM])
    except Exception as error:  # noqa: BLE001
        raise trust.TrustError(f"Bad service token signature: {error}") from None
    if decoded.header.get("typ") != trust.TYP or decoded.header.get("kid") != key.thumbprint():
        raise trust.TrustError("Not a service token of this instance")
    claims = decoded.claims
    identity = rekuest_identifier()
    if claims.get("iss") != identity or claims.get("aud") != identity:
        raise trust.TrustError("Not a token from rekuest to itself")
    now = int(time.time())
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int) or exp < now - max_skew or iat > now + max_skew or exp - iat > trust.LIFETIME_SECONDS + max_skew:
        raise trust.TrustError("Service token expired or not yet valid")
    if claims.get("htm") != method.upper() or claims.get("htu") != path:
        raise trust.TrustError("Service token was signed for another request")
    if claims.get("bh") != trust.body_hash(body):
        raise trust.TrustError("Service token was signed for another body")
    if not claims.get("jti"):
        raise trust.TrustError("Service token without an id")
    return trust.Verified(issuer=identity, jti=str(claims["jti"]), expires_at=exp)

"""Trust between rekuest and the other instances of this hub: instance keys, no shared secrets.

Every request between rekuest and a configured service (``rekuest.services``: its manifest, its
signals) or hook agent (``rekuest.hook_agents``: Assign deliveries, its manifest, its reports)
carries a short-lived JWT signed with the sender's instance key (:mod:`arkitekt_service.trust`,
vendored). The receiver checks it against the hub's trust bundle: the coord-vouched public keys
of every instance, each listed under its identifier. An instance can therefore only speak as
itself, and only rekuest can speak as rekuest.

Services and hook agents are separate lists, looked up separately; an entry of either says which
instance it is (``identifier``, default ``live.arkitekt.<name>``).

Third-party HookAgents (registered through ``ensureAgent`` with a ``hook_url_secret``) keep the
HMAC scheme (takt's ``hooks``); only the hub's configured ones use keys.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

from django.conf import settings
from joserfc import jwt
from joserfc.jwk import KeySet

from rekuest.configuration import HookAgentEntry, ServiceEntry
from arkitekt_service import trust

#: The identities rekuest mints for configured hook agents are named ``hook-<name>``, so their
#: clients are ``rekuest:hook-<name>``: that is how an agent is recognised as one.
HOOK_IDENTITY_PREFIX = "hook-"
HOOK_CLIENT_PREFIX = f"rekuest:{HOOK_IDENTITY_PREFIX}"


def rekuest_identifier() -> str:
    return settings.REKUEST_IDENTIFIER


def identifier_of(entry: ServiceEntry | HookAgentEntry) -> str:
    """The identifier an entry's instance signs as (and is listed under in the trust bundle)."""
    return entry.identifier or f"live.arkitekt.{entry.name}"


def sign_to(entry: ServiceEntry | HookAgentEntry, method: str, url: str, body: bytes) -> str:
    """The ``Authorization`` value of a request from rekuest to this entry's instance (a service or a hook agent)."""
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

"""HTTP-POST transport for HookAgents (``Agent.kind == WEBHOOK``).

A HookAgent speaks the same message protocol as a websocket agent, but over HTTP: the
backend POSTs downstream messages to ``agent.hook_url`` and the agent POSTs upstream
messages to the intake endpoint. Both directions are authenticated by an HMAC over the
shared ``agent.hook_url_secret`` — there is no JWT on the HTTP path.

Delivery is persist-then-POST: callers persist the Task/event row *before* calling
out here, so a failed POST is logged (not raised) and the persisted row remains the durable
record from which a later redelivery sweep can re-POST.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from facade import models

logger = logging.getLogger(__name__)


def signature_mode() -> str:
    """``"compat"`` (accept and send both signature versions) or ``"strict"`` (V1 only)."""
    from django.conf import settings

    return str(getattr(settings, "HOOK_SIGNATURE_MODE", "compat") or "compat").lower()


def max_skew_seconds() -> int:
    from django.conf import settings

    return int(getattr(settings, "HOOK_MAX_SKEW", 300))

SIGNATURE_HEADER = "X-Rekuest-Signature"
#: The replay-protected signature. ``t=<unix seconds>,v1=<hex>`` over
#: ``v1:{agent_id}:{t}:`` + body — the timestamp bounds how long a captured request stays
#: usable, and binding the agent id stops a body signed for one HookAgent from being replayed
#: against another that happens to share the secret.
SIGNATURE_V1_HEADER = "X-Rekuest-Signature-V1"
_TIMEOUT = 10.0

# Module-level client: connection pooling across many deliveries.
_client = httpx.Client(timeout=_TIMEOUT)


def sign(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex digest of ``body`` under ``secret``."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(secret: str | None, body: bytes, signature: str | None) -> bool:
    """Constant-time check that ``signature`` is a valid HMAC of ``body`` under ``secret``."""
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign(secret, body), signature)


def signed_payload_v1(agent_id: object, timestamp: int, body: bytes) -> bytes:
    """What a V1 signature is computed over. The version tag makes it unambiguous."""
    return f"v1:{agent_id}:{timestamp}:".encode("utf-8") + body


def sign_v1(secret: str, agent_id: object, body: bytes, timestamp: int | None = None) -> str:
    """The ``t=…,v1=…`` header value for ``body`` addressed to ``agent_id``."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    return f"t={timestamp},v1={sign(secret, signed_payload_v1(agent_id, timestamp, body))}"


def parse_v1(header: str | None) -> tuple[int, str] | None:
    """``(timestamp, hex digest)`` from a V1 header, or None when it is absent/malformed."""
    if not header:
        return None
    parts = dict(piece.split("=", 1) for piece in header.split(",") if "=" in piece)
    timestamp, digest = parts.get("t"), parts.get("v1")
    if timestamp is None or digest is None:
        return None
    try:
        return int(timestamp), digest
    except ValueError:
        return None


def verify_v1(secret: str | None, agent_id: object, body: bytes, header: str | None, max_skew: int) -> tuple[bool, str | None]:
    """``(ok, digest)`` for a V1-signed request. ``digest`` identifies it for the replay guard.

    A request outside ``max_skew`` is refused even with a valid signature: the timestamp is what
    bounds a captured request's usefulness, and it is inside the signed payload so it cannot be
    edited. Skew is checked in both directions — a clock ahead of ours is as suspicious as one
    behind, and only a bounded window can be de-duplicated with a finite memory.
    """
    parsed = parse_v1(header)
    if not secret or parsed is None:
        return False, None
    timestamp, digest = parsed
    if abs(int(time.time()) - timestamp) > max_skew:
        return False, digest
    expected = sign(secret, signed_payload_v1(agent_id, timestamp, body))
    return hmac.compare_digest(expected, digest), digest


def deliver_to_hook(agent: "models.Agent", body: str) -> bool:
    """POST ``body`` (a JSON message) to ``agent.hook_url``, HMAC-signed. Never raises.

    Returns True on a 2xx response. Failures are logged — the persisted Task/event
    row is the durable record, so a failed delivery is recoverable, not lost.
    """
    url = getattr(agent, "hook_url", None)
    if not url:
        logger.error("HookAgent %s has no hook_url; dropping message", getattr(agent, "pk", "?"))
        return False

    raw = body.encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = getattr(agent, "hook_url_secret", None)
    if secret:
        headers[SIGNATURE_V1_HEADER] = sign_v1(secret, getattr(agent, "pk", ""), raw)
        if signature_mode() != "strict":
            # Sent alongside V1 during the compatibility window so a receiver that only knows
            # the body-only signature keeps working. ``strict`` drops it.
            headers[SIGNATURE_HEADER] = sign(secret, raw)

    try:
        response = _client.post(url, content=raw, headers=headers)
        response.raise_for_status()
        return True
    except Exception:
        logger.error("Failed to deliver message to HookAgent %s at %s", getattr(agent, "pk", "?"), url, exc_info=True)
        return False

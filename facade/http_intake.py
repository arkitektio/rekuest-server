"""HTTP POST intake — the upstream transport for HookAgents.

A HookAgent POSTs the same FromAgent messages a websocket agent would send (reporting events
like Done/Yield/Progress, and ``AssignRequest`` for server-to-server origination) to
``POST /agi/http/<agent_id>``, HMAC-signed with the agent's ``hook_url_secret``. The request
is verified, parsed, and routed through the SAME :func:`route_from_agent_message` the socket
uses; the reply (``EventAck`` / ``AssignResponse``) is returned in the HTTP response.

Unlike the websocket, HTTP has no session: every request must stand on its own, and a captured
one could be replayed forever against any backend. So a request carries a *timestamped*
signature (see :data:`facade.hooks.SIGNATURE_V1_HEADER`) and its digest is claimed once in redis
— shared by every backend, because a per-process memory of seen requests would just move the
replay to the next replica. The guard is not only about attackers: it also makes an ordinary
HTTP retry idempotent for the fire-and-forget events (Yield/Log/Progress) that carry no ack.
"""

from __future__ import annotations

import json
import logging

from asgiref.sync import sync_to_async
from django.http import HttpRequest, HttpResponse, JsonResponse

from facade import enums, hooks, models, redis_keys
from facade.consumers.agent_protocol import FromAgentPayload
from facade.hooks import SIGNATURE_HEADER, SIGNATURE_V1_HEADER
from facade.message_router import UnknownAgentMessage, log_refusal, reply_for_duplicate, route_from_agent_message
from facade.persist_backend import persist_backend

logger = logging.getLogger(__name__)


def _replay_store():
    """The redis client the replay guard claims digests in (shared across backends)."""
    import redis
    from django.conf import settings

    from facade.consumers.agent_queue import _sync_pool

    return redis.Redis(connection_pool=_sync_pool(settings.AGENT_REDIS_HOST, settings.AGENT_REDIS_PORT))


def _authenticate(agent, body: bytes, headers) -> tuple[bool, str | None]:
    """``(ok, digest)``. ``digest`` is None for a legacy (unreplayable-guarded) signature.

    ``strict`` mode accepts only the timestamped V1 signature. ``compat`` also accepts the
    legacy body-only one when no V1 header is present, so a third-party HookAgent keeps working
    for one release — it cannot be used to *downgrade* a V1 request, because a V1 signature
    cannot be turned into a legacy one without the secret.
    """
    v1_header = headers.get(SIGNATURE_V1_HEADER)
    if v1_header is not None:
        ok, digest = hooks.verify_v1(agent.hook_url_secret, agent.pk, body, v1_header, hooks.max_skew_seconds())
        return ok, digest
    if hooks.signature_mode() == "strict":
        return False, None
    if hooks.verify(agent.hook_url_secret, body, headers.get(SIGNATURE_HEADER)):
        logger.warning("HookAgent %s used the legacy body-only signature; it is replayable and goes away next release", agent.pk)
        return True, None
    return False, None


async def hook_intake(request: HttpRequest, agent_id: str) -> HttpResponse:
    """Authenticate (HMAC), validate, and route one FromAgent message from a HookAgent."""
    if request.method != "POST":
        return HttpResponse(status=405)

    body = request.body  # raw bytes — the exact bytes the HMAC was computed over

    agent = await models.Agent.objects.filter(id=agent_id, kind=enums.AgentKind.WEBHOOK.value).afirst()
    if agent is None:
        return JsonResponse({"error": "Unknown hook agent"}, status=404)
    if agent.blocked:
        return JsonResponse({"error": "Agent is blocked"}, status=403)
    authenticated, digest = await sync_to_async(_authenticate)(agent, body, request.headers)
    if not authenticated:
        return JsonResponse({"error": "Invalid signature"}, status=401)

    try:
        payload = FromAgentPayload(message=json.loads(body))
    except Exception as e:
        return JsonResponse({"error": f"Invalid message: {e}"}, status=400)

    claimed = True
    if digest is not None:
        try:
            claimed = await sync_to_async(_claim_request)(agent.pk, digest)
        except Exception:
            # Fail CLOSED. Without the guard an attacker only has to knock redis over to replay
            # freely, and a retry the sender will make anyway is the cheaper failure.
            logger.error("Hook replay guard unavailable", exc_info=True)
            return JsonResponse({"error": "Replay guard unavailable"}, status=503)

    if not claimed:
        logger.info("HookAgent %s replayed a request (%s)", agent.pk, type(payload.message).__name__)
        answer = reply_for_duplicate(payload.message)
        if answer is not None:  # None ⇒ idempotent on its own, route it as usual
            reply, status = answer
            return JsonResponse(reply.model_dump() if reply is not None else {"duplicate": True}, status=status)

    try:
        reply = await route_from_agent_message(persist_backend, agent.pk, payload.message)
    except UnknownAgentMessage as e:
        return JsonResponse({"error": f"Unhandled message: {e}"}, status=400)
    except Exception as e:
        log_refusal("Hook intake", e)
        # Release the claim: the request never took effect, so the sender's retry must not be
        # mistaken for a replay.
        if digest is not None:
            await sync_to_async(_release_request)(agent.pk, digest)
        return JsonResponse({"error": str(e)}, status=400)

    return JsonResponse(reply.model_dump() if reply is not None else {})


def _replay_key(agent_pk: object, digest: str) -> str:
    return redis_keys.key("hook-replay", agent_pk, digest)


def _claim_request(agent_pk: object, digest: str) -> bool:
    """Claim this exact request once. False means it has been seen before.

    The key outlives the accepted skew window on both sides, which is the whole window in which
    a replay could still pass the signature check.
    """
    ttl = max(1, hooks.max_skew_seconds() * 2)
    return bool(_replay_store().set(_replay_key(agent_pk, digest), "1", nx=True, ex=ttl))


def _release_request(agent_pk: object, digest: str) -> None:
    try:
        _replay_store().delete(_replay_key(agent_pk, digest))
    except Exception:
        logger.error("Could not release a hook replay claim", exc_info=True)

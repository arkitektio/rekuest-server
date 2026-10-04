"""The server's upkeep jobs, run when takt asks: provisioning and embeddings.

Everything periodic in this deployment is on takt's clock. Two jobs need this server — service
manifests are registered through its models, and embeddings run the model, which only this
image carries — so takt calls them here, signed with the instance key (it reads the same
``config.yaml``, so it holds the same key):

==========================  ===============================  =================================
``POST _rekuest/upkeep/…``  does                             takt calls it
==========================  ===============================  =================================
``provision``               ``provisioning.provision_all``     at start, then every 5 minutes
                                                             (sooner after a failure)
``reembed``                 ``reembed_stale(Action)``         every 30 seconds, and again at
                                                             once while there is more
==========================  ===============================  =================================

Nothing here loops and nothing is remembered between requests: a job runs once per call, any
replica may answer, and two calls at once are safe (provisioning is serialized by an advisory
lock, the re-embed claims its rows). ``_rekuest`` paths are not routed at the edge.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import redis
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from pydantic import BaseModel

from embeddings import healer
from facade import models, provisioning, redis_keys, service_trust
from rekuest_service import trust

logger = logging.getLogger(__name__)

#: Batches one ``reembed`` call drains at most, so a request stays short.
REEMBED_MAX_BATCHES = 5


def _claim(verified: trust.Verified) -> bool:
    """Claim the token's ``jti`` once, across every replica (takt's ``service_trust::claim``)."""
    connection = redis.Redis(host=settings.AGENT_REDIS_HOST, port=settings.AGENT_REDIS_PORT)
    ttl = max(1, verified.expires_at + trust.LIFETIME_SECONDS - int(time.time()))
    return bool(connection.set(redis_keys.key("service-jti", verified.jti), 1, nx=True, ex=ttl))


class Provisioned(BaseModel):
    """What a provisioning pass came to (``takt/crates/facade/src/upkeep.rs`` reads it)."""

    ok: bool
    skipped: bool
    failed: list[str]


class Reembedded(BaseModel):
    embedded: int
    more: bool


def provision() -> Provisioned:
    """One provisioning pass — the service catalog, then the hook agents; which of them failed."""
    outcome = provisioning.provision_all()
    if outcome is None:
        return Provisioned(ok=True, skipped=True, failed=[])
    failed = [*(f"service {name}" for name in outcome.services), *(f"hook agent {name}" for name in outcome.hook_agents)]
    return Provisioned(ok=not failed, skipped=False, failed=failed)


def reembed() -> Reembedded:
    """A bounded pass over the actions without a current vector; how many it embedded."""
    embedded = healer.reembed_stale(models.Action, max_batches=REEMBED_MAX_BATCHES)
    # ``more`` only after progress: with embeddings off (or the model unreachable) the stale
    # rows stay, and takt must not call again at once for them.
    return Reembedded(embedded=embedded, more=embedded > 0 and healer.stale_queryset(models.Action).exists())


JOBS: dict[str, Callable[[], Provisioned | Reembedded]] = {"provision": provision, "reembed": reembed}


@csrf_exempt
def upkeep_view(request: HttpRequest, job: str) -> HttpResponse:
    """Run one upkeep job for takt. Only this instance's own key may ask."""
    run = JOBS.get(job)
    if run is None:
        return JsonResponse({"error": f"No upkeep job {job!r}"}, status=404)
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    try:
        verified = service_trust.verify_own(request.method, request.path, request.body, request.headers.get("Authorization"))
    except trust.TrustError as error:
        logger.info("Refused an upkeep request to %s: %s", request.path, error)
        return JsonResponse({"error": str(error)}, status=401)
    try:
        if not _claim(verified):
            return JsonResponse({"error": "Replayed service token"}, status=409)
    except redis.RedisError as error:
        # Fail closed: without the guard, knocking redis over would allow replays.
        logger.error("Upkeep replay guard unavailable: %s", error)
        return JsonResponse({"error": "Replay guard unavailable"}, status=503)
    return JsonResponse(run().model_dump(mode="json"))

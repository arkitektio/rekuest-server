"""``POST /agi/signal/<service>`` — a service announces that something happened.

The service is one of ``rekuest.service_agents``: the request carries a service JWT signed with
that service's instance key and bound to this endpoint's path and body
(``facade.service_trust``), so it cannot be replayed as agent traffic or vice versa. It is keyed
by the service NAME: a service can signal before rekuest ever told it its agent id.

The body names the object (structure identifier + id), its organization, its descriptors, and
— when the service was called inside a task — the raw provenance token of that task. rekuest
verifies that token against its own key (:mod:`facade.provenance.verify`); only then does the
signal carry a ``causing_task``. A signal is stored and acknowledged at once (202); matching and
firing are the reaper's ``fire_triggers``. Best-effort by design: nothing redelivers a signal
that never arrived, and a resend with the same ``id`` is a no-op.
"""

from __future__ import annotations

import datetime
import json
import logging

from asgiref.sync import sync_to_async
from authentikate.models import Organization
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse, JsonResponse
from pydantic import BaseModel, Field, ValidationError

from rekuest_service.trust import TrustError

from facade import enums, models, service_trust
from facade.http_intake import _claim_request, _release_request
from facade.provenance.verify import verify_own_token

logger = logging.getLogger(__name__)


class SignalMessage(BaseModel):
    """What a service sends. Descriptors are flat ``key → value`` (the ports' requires vocabulary)."""

    id: str = Field(min_length=1, max_length=200)
    kind: enums.SignalKind
    identifier: str = Field(min_length=1, max_length=1000)
    object: str = Field(min_length=1, max_length=1000)
    organization: str = Field(min_length=1)
    descriptors: dict = Field(default_factory=dict)
    provenance: str | None = None
    occurred_at: datetime.datetime | None = None


def signed_id(service: str) -> str:
    """The replay-guard namespace of a service's signals (distinct from any agent id)."""
    return f"signal:{service}"


def _store(service: str, message: SignalMessage) -> tuple[models.Signal | None, bool]:
    """``(signal, created)``; ``(None, False)`` when the organization is unknown here."""
    organization = Organization.objects.filter(slug=message.organization).first()
    if organization is None:
        logger.warning("Signal %s from %s names unknown organization %r; dropped", message.id, service, message.organization)
        return None, False

    existing = models.Signal.objects.filter(service=service, signal_id=message.id).first()
    if existing is not None:
        return existing, False

    causing_task = None
    causing_root = None
    provenance = verify_own_token(message.provenance)
    if provenance is not None:
        # The token proves the service was called in that task; the organization check proves
        # the task belongs where the object does — a service cannot graft runs onto another
        # organization's tree by forwarding a token it holds for it.
        causing_task = models.Task.objects.filter(pk=provenance.task, agent__organization=organization).first()
        if causing_task is None:
            logger.warning("Signal %s from %s: provenance task %s is not in %s; stored without a cause", message.id, service, provenance.task, organization.slug)
        else:
            causing_root = provenance.root

    try:
        signal = models.Signal.objects.create(
            service=service,
            signal_id=message.id,
            kind=message.kind.value,
            identifier=message.identifier,
            object=message.object,
            organization=organization,
            descriptors=message.descriptors,
            causing_task=causing_task,
            causing_root=causing_root,
            occurred_at=message.occurred_at,
        )
    except IntegrityError:  # the same signal, raced in from a concurrent resend
        return models.Signal.objects.get(service=service, signal_id=message.id), False
    return signal, True


async def signal_intake(request: HttpRequest, service: str) -> HttpResponse:
    """Authenticate (HMAC), validate and store one signal."""
    if request.method != "POST":
        return HttpResponse(status=405)
    entry = service_trust.entry_for(service)
    if entry is None:
        return JsonResponse({"error": "Unknown service"}, status=404)

    body = request.body
    try:
        verified = service_trust.verify_from(entry, "POST", request.path, body, request.headers.get("Authorization"))
    except TrustError as error:
        logger.info("Refused a signal from %s: %s", service, error)
        return JsonResponse({"error": "Invalid signature"}, status=401)
    digest = f"jwt:{verified.jti}"

    try:
        message = SignalMessage.model_validate(json.loads(body))
    except (ValueError, ValidationError) as error:
        return JsonResponse({"error": f"Invalid signal: {error}"}, status=400)

    try:
        claimed = await sync_to_async(_claim_request)(signed_id(service), digest)
    except Exception:
        logger.error("Signal replay guard unavailable", exc_info=True)
        return JsonResponse({"error": "Replay guard unavailable"}, status=503)
    if not claimed:
        return JsonResponse({"duplicate": True}, status=202)

    try:
        signal, created = await sync_to_async(_store)(service, message)
    except Exception as error:
        await sync_to_async(_release_request)(signed_id(service), digest)
        logger.error("Could not store signal %s from %s: %s", message.id, service, error)
        return JsonResponse({"error": "Could not store the signal"}, status=500)

    if signal is None:
        return JsonResponse({"dropped": "unknown organization"}, status=202)
    return JsonResponse({"signal": str(signal.pk), "created": created, "caused_by": str(signal.causing_task_id) if signal.causing_task_id else None}, status=202)

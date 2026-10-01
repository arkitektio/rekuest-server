from kante.types import Info
import strawberry
from facade import models
from django.db.models import Count
import logging

logger = logging.getLogger(__name__)


def cleanup_actions(info: Info, action_ids: list[strawberry.ID] | None = None) -> int:
    """Delete the caller's organization's Actions that no implementation references.

    Always organization-scoped. The no-argument form used to fall through to the bare manager,
    which deleted every unreferenced Action in *every* organization — and, because
    ``Task.action`` cascades, their task history with them.
    """
    organization = info.context.request.organization

    actions_to_check = models.Action.objects.filter(organization=organization)
    if action_ids:
        actions_to_check = actions_to_check.filter(id__in=action_ids)

    unreferenced_actions = actions_to_check.annotate(num_implementations=Count("implementations")).filter(num_implementations=0)

    # Delete them in bulk (efficiently)
    deleted_count, _ = unreferenced_actions.delete()

    logger.info(f"Deleted {deleted_count} unreferenced actions for organization {organization.slug}.")

    return deleted_count

import logging
from typing import cast

import strawberry
from django.db.models import QuerySet
from kante.types import Info
from pgvector.django import CosineDistance
from strawberry_django.filters import apply as apply_filters

from embeddings import engine
from facade import filters, managers, models, types
from facade.types.base import scoped_get
from rekuest_core import scalars as rscalars
from rekuest_core.inputs import types as rinputs

logger = logging.getLogger(__name__)


def action(
    info: Info,
    id: strawberry.ID | None = None,
    task: strawberry.ID | None = None,
    implementation: strawberry.ID | None = None,
    agent: strawberry.ID | None = None,
    interface: str | None = None,
    hash: rscalars.ActionHash | None = None,
    matching: rinputs.ActionDemandInput | None = None,
) -> types.Action:
    if task:
        return models.Task.objects.get(id=task).action
    if implementation:
        return models.Implementation.objects.get(id=implementation).action

    if hash:
        return cast("types.Action", models.Action.objects.get(hash=hash, organization=info.context.request.organization))

    if matching:
        ids = managers.get_action_ids_by_action_demands(
            [matching.to_pydantic()],
            organization_id=info.context.request.organization.id,
        )[0]

        return cast("types.Action", models.Action.objects.get(id=ids[0]))

    if agent:
        if interface:
            found = models.Implementation.objects.filter(action__hash=hash, interface=interface).first()
            if found is None:
                raise ValueError(f"No implementation of {hash} under the interface {interface!r}")
            return cast("types.Action", found.action)
        else:
            raise ValueError("You need to provide either, action_hash or action_id, if you want to inspect the action of an agent")

    return cast("types.Action", models.Action.objects.get(id=id))


def similar_actions_queryset(info: Info, origin: models.Action, filters_: "filters.ActionFilter | None" = None, limit: int = 10, max_distance: float | None = None) -> QuerySet[models.Action]:
    """The org's other actions ranked by how close their name + description is to ``origin``'s.

    Cosine distance between ``origin``'s stored embedding and every other action's, nearest
    first; ``origin`` itself and actions without a vector are left out.
    ``filters`` narrows the candidates with the same ``ActionFilter`` the ``actions`` list
    takes, so "similar actions in this collection" or "similar actions that are not dev" is
    one query. ``max_distance`` cuts the tail (0 identical, 1 unrelated); by default the
    nearest ``limit`` come back whatever their distance -- this is a browse, not a search.

    Empty when embeddings are off or ``origin`` has no vector (it was saved while the model
    was unreachable): there is nothing honest to rank by.
    """
    if not engine.enabled() or origin.embedding is None:
        return models.Action.objects.none()
    queryset = models.Action.objects.filter(organization=origin.organization, embedding__isnull=False).exclude(pk=origin.pk)
    queryset = apply_filters(filters_, queryset, info)
    queryset = queryset.annotate(_similar_distance=CosineDistance("embedding", origin.embedding))
    if max_distance is not None:
        queryset = queryset.filter(_similar_distance__lt=max_distance)
    return queryset.order_by("_similar_distance", "pk")[: max(1, limit)]


def similar_actions(
    info: Info,
    action: strawberry.ID,
    filters: "filters.ActionFilter | None" = None,
    limit: int = 10,
    max_distance: float | None = None,
) -> list[types.Action]:
    """Actions semantically similar to ``action``, nearest first (see ``similar_actions_queryset``)."""
    origin = scoped_get(models.Action, info, action)
    return cast("list[types.Action]", list(similar_actions_queryset(info, origin, filters, limit, max_distance)))

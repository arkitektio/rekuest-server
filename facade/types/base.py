"""Shared helpers used across the facade GraphQL types."""

from __future__ import annotations

from typing import TypeVar

from arkitekt_service.collaborations.guests import object_scope
from django.db.models import Model, QuerySet
from kante.types import Info

M = TypeVar("M", bound=Model)


def row_of(self: object, model: type[M]) -> M:
    """The row behind a strawberry-django type.

    A resolver's ``self`` is declared as the GraphQL type and is, when it runs, the Django row
    the type was made from. This says so, and checks it.
    """
    if not isinstance(self, model):
        raise TypeError(f"Expected a {model.__name__} row, got {type(self).__name__}")
    return self


def build_prescoped_queryset(
    info: Info, queryset: QuerySet[M], field: str = "organization"
) -> QuerySet[M]:
    """``queryset`` held to the request's organization and, at a root field, for a guest of it,
    to what is in their collaborations (``rekuest.service``). Below a root field the collaboration
    gate holds a guest to them."""
    # ``filters`` may be absent, or given as null: both mean "no custom scope".
    if (info.variable_values.get("filters") or {}).get("scope") is None:
        queryset = queryset.filter(**{field: info.context.request.organization})
        return object_scope(queryset, info=info)

    else:
        raise Exception("Custom scopes not implemented yet")


def scoped_get(
    model: type[M], info: Info, pk: str | int, *, field: str = "organization"
) -> M:
    """Fetch one row by id, scoped to the caller's organization.

    ``get_queryset`` only runs for queryset-producing fields, so a root resolver that returns a
    single model instance (``Model.objects.get(id=id)``) bypasses type-level scoping entirely.
    Those resolvers must scope themselves; this is the one place that happens.

    ``field`` is a lookup path to the organization (e.g. ``"agent__organization"``). Raises
    ``PermissionError`` rather than ``DoesNotExist`` so a wrong-tenant id is indistinguishable
    from a missing one. A guest of the organization is held to what is in their collaborations:
    anything else is as missing to them.
    """
    try:
        return object_scope(
            model._default_manager.filter(**{field: info.context.request.organization}),
            info=info,
        ).get(id=pk)
    except model.DoesNotExist:
        raise PermissionError(
            f"No {model.__name__} {pk} in this organization."
        ) from None

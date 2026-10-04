"""Shared helpers used across the facade GraphQL types."""

from __future__ import annotations

from django.db.models import Model, QuerySet
from kante.types import Info


def row_of[M: Model](self: object, model: type[M]) -> M:
    """The row behind a strawberry-django type.

    A resolver's ``self`` is declared as the GraphQL type and is, when it runs, the Django row
    the type was made from. This says so, and checks it.
    """
    if not isinstance(self, model):
        raise TypeError(f"Expected a {model.__name__} row, got {type(self).__name__}")
    return self


def build_prescoped_queryset[M: Model](info: Info, queryset: QuerySet[M], field: str = "organization") -> QuerySet[M]:
    # ``filters`` may be absent, or given as null: both mean "no custom scope".
    if (info.variable_values.get("filters") or {}).get("scope") is None:
        queryset = queryset.filter(**{field: info.context.request.organization})
        return queryset

    else:
        raise Exception("Custom scopes not implemented yet")


def scoped_get[M: Model](model: type[M], info: Info, pk: str | int, *, field: str = "organization") -> M:
    """Fetch one row by id, scoped to the caller's organization.

    ``get_queryset`` only runs for queryset-producing fields, so a root resolver that returns a
    single model instance (``Model.objects.get(id=id)``) bypasses type-level scoping entirely.
    Those resolvers must scope themselves; this is the one place that happens.

    ``field`` is a lookup path to the organization (e.g. ``"agent__organization"``). Raises
    ``PermissionError`` rather than ``DoesNotExist`` so a wrong-tenant id is indistinguishable
    from a missing one.
    """
    try:
        return model._default_manager.get(**{"id": pk, field: info.context.request.organization})
    except model.DoesNotExist:
        raise PermissionError(f"No {model.__name__} {pk} in this organization.")

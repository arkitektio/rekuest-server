"""Shared helpers used across the facade GraphQL types."""

from __future__ import annotations


def build_prescoped_queryset(info, queryset, field="organization"):
    # ``filters`` may be absent, or given as null: both mean "no custom scope".
    if (info.variable_values.get("filters") or {}).get("scope") is None:
        queryset = queryset.filter(**{field: info.context.request.organization})
        return queryset

    else:
        raise Exception("Custom scopes not implemented yet")


def scoped_get(model, info, pk, *, field="organization"):
    """Fetch one row by id, scoped to the caller's organization.

    ``get_queryset`` only runs for queryset-producing fields, so a root resolver that returns a
    single model instance (``Model.objects.get(id=id)``) bypasses type-level scoping entirely.
    Those resolvers must scope themselves; this is the one place that happens.

    ``field`` is a lookup path to the organization (e.g. ``"agent__organization"``). Raises
    ``PermissionError`` rather than ``DoesNotExist`` so a wrong-tenant id is indistinguishable
    from a missing one.
    """
    try:
        return model.objects.get(**{"id": pk, field: info.context.request.organization})
    except model.DoesNotExist:
        raise PermissionError(f"No {model.__name__} {pk} in this organization.")


def build_prescoper(field="organization"):
    def prescoper(queryset, info):
        return build_prescoped_queryset(info, queryset, field=field)

    return prescoper

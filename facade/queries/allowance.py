"""The caller's auto list: the actions that run under their runs without asking them."""

from typing import cast

import strawberry
from kante.types import Info

from facade import models, types
from facade.types.base import scoped_get


def my_allowances(info: Info, action: strawberry.ID | None = None) -> list[types.ActionAllowance]:
    """The caller's standing allowances in this organization, newest first; with ``action``, those for it.

    An allowance of one run is not listed: it ends with its run.
    """
    request = info.context.request
    allowances = models.ActionAllowance.objects.filter(user=request.user, organization=request.organization, run__isnull=True)
    if action is not None:
        row = scoped_get(models.Action, info, action)
        allowances = allowances.filter(app_id=row.app_id, key=row.key)
    return cast("list[types.ActionAllowance]", list(allowances.select_related("app", "asker_app").order_by("-created_at", "-id")))

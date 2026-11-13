"""Writing the caller's auto list by hand. A decision writes one too (``decide``'s ``remember``, in takt)."""

from typing import cast

import strawberry
from kante.types import Info

from facade import inputs, models, types
from facade.types.base import scoped_get


def allow_action(info: Info, input: inputs.AllowActionInput) -> types.ActionAllowance:
    """Put an action on the caller's auto list, for every run of theirs. Allowing it again changes nothing."""
    data = input.to_pydantic()
    request = info.context.request
    action = scoped_get(models.Action, info, data.action)
    asker = scoped_get(models.Action, info, data.asker) if data.asker else None
    allowance, _ = models.ActionAllowance.objects.get_or_create(
        user=request.user,
        organization=request.organization,
        app_id=action.app_id,
        key=action.key,
        asker_app_id=asker.app_id if asker else None,
        asker_key=asker.key if asker else None,
        run=None,
    )
    return cast("types.ActionAllowance", allowance)


def revoke_allowance(info: Info, input: inputs.RevokeAllowanceInput) -> strawberry.ID:
    """Take one of the caller's own allowances off their auto list. Someone else's is not found."""
    data = input.to_pydantic()
    request = info.context.request
    try:
        allowance = models.ActionAllowance.objects.get(id=data.id, user=request.user, organization=request.organization)
    except models.ActionAllowance.DoesNotExist:
        raise PermissionError(f"No allowance {data.id} on your auto list.")
    allowance.delete()
    return strawberry.ID(data.id)

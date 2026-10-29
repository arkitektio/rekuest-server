import logging
from types import SimpleNamespace
from typing import cast

import strawberry
from authentikate.base_models import JWTToken
from authentikate.expand import token_to_username
from authentikate.models import User
from authentikate.strawberry.directives import has_org_role
from django.db import transaction
from graphql import GraphQLError
from kante.types import Info

from facade import inputs, models, types
from facade.types.base import scoped_get
from rekuest.contract import ADMIN

logger = logging.getLogger(__name__)


def _user_of(sub: str, issuer: str | None) -> User:
    """The user ``sub`` of the admin's issuer names.

    Someone who never called rekuest has no row yet. It is made the way their first request
    would make it, looked up by ``(sub, iss)`` and named by authentikate's own scheme, so that
    request finds this row instead of making a second one.
    """
    named = cast("JWTToken", SimpleNamespace(iss=issuer or "", sub=sub))
    user, _ = User.objects.get_or_create(sub=sub, iss=issuer, defaults={"username": token_to_username(named)})
    return user


def _bind(info: Info, shortcut: models.Shortcut, rule: inputs.ShortcutBindingsInputModel) -> None:
    """Pin ``shortcut`` for whoever ``rule`` matches: one binding per user (one for every user when
    it names none), each sharing its app, client and device. Binding again changes nothing."""
    admin = info.context.request.user
    where = dict(app=rule.app or None, client=rule.client or None, device=rule.device or None)
    users = [_user_of(sub, admin.iss) for sub in dict.fromkeys(rule.users or [])] or [None]
    for user in users:
        models.ShortcutBinding.objects.get_or_create(shortcut=shortcut, user=user, **where, defaults={"bound_by": admin})


def create_shortcut(info: Info, input: inputs.CreateShortcutInput) -> types.Shortcut:
    data = input.to_pydantic()
    if data.bindings is not None and not has_org_role(info, ADMIN):
        raise GraphQLError("Only an admin may pin a shortcut for others.")
    toolbox = (
        scoped_get(models.Toolbox, info, data.toolbox)
        if data.toolbox
        else models.Toolbox.objects.get_or_create(
            name="default",
            organization=info.context.request.organization,
            defaults=dict(
                description="Default toolbox",
                creator=info.context.request.user,
                client=info.context.request.client,
            ),
        )[0]
    )

    action = models.Action.objects.get(id=data.action)

    args = [arg for arg in action.args if arg["key"] not in data.args]
    returns = [arg for arg in action.returns if arg["key"] not in []]

    with transaction.atomic():
        shortcut = models.Shortcut.objects.create(
            name=data.name,
            description=data.description,
            action_id=data.action,
            creator=info.context.request.user,
            saved_args=data.args,
            toolbox=toolbox,
            args=args,
            returns=returns,
            allow_quick=data.allow_quick,
            use_returns=data.use_returns,
            bind_number=data.bind_number,
        )
        if data.bindings is not None:
            _bind(info, shortcut, data.bindings)

    logger.debug("Shortcut created: %s", shortcut)

    return cast("types.Shortcut", shortcut)


def delete_shortcut(info: Info, input: inputs.DeleteShortcutInput) -> strawberry.ID:
    data = input.to_pydantic()
    shortcut = scoped_get(models.Shortcut, info, data.id, field="toolbox__organization")
    shortcut.delete()
    return strawberry.ID(data.id)


def bind_shortcut(info: Info, input: inputs.BindShortcutInput) -> types.Shortcut:
    """Pin a shortcut of the caller's organization for whoever the rule matches (users, app, client, device)."""
    data = input.to_pydantic()
    shortcut = scoped_get(models.Shortcut, info, data.shortcut, field="toolbox__organization")
    with transaction.atomic():
        _bind(info, shortcut, data)
    return cast("types.Shortcut", shortcut)


def unbind_shortcut(info: Info, input: inputs.UnbindShortcutInput) -> strawberry.ID:
    """Remove one binding; the shortcut stays."""
    data = input.to_pydantic()
    binding = scoped_get(models.ShortcutBinding, info, data.id, field="shortcut__toolbox__organization")
    binding.delete()
    return strawberry.ID(data.id)

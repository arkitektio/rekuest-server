"""What an implementation's tasks may do besides computing: ask a person, show them something,
call other actions — declared with it, or at run time when it declares the dynamic flag.

Shared by ``ImplementationFilter`` and ``ActionFilter``: an action does what at least one of its
implementations does. Every condition is a subquery returned as the filter's ``Q`` (never applied
to the queryset), so the fields compose under ``AND`` / ``OR`` / ``NOT`` and a match is never
duplicated by a join.
"""

from __future__ import annotations

from typing import Literal

from django.db.models import Q, QuerySet

from facade import models

Capability = Literal["asks", "informs", "calls", "dynamic_asks", "dynamic_informs", "dynamic_calls"]


def _declares(role: models.InteractionRole) -> Q:
    return Q(id__in=models.ImplementationInteraction.objects.filter(role=role).values("implementation_id"))


def _condition(capability: Capability) -> Q:
    match capability:
        case "asks":
            return Q(dynamic_asks=True) | _declares(models.InteractionRole.ASK)
        case "informs":
            return Q(dynamic_informs=True) | _declares(models.InteractionRole.INFORM)
        case "calls":
            return Q(dynamic_calls=True) | Q(id__in=models.Dependency.objects.values("implementation_id"))
        case "dynamic_asks" | "dynamic_informs" | "dynamic_calls":
            return Q(**{capability: True})


def implementations_that(capability: Capability) -> QuerySet[models.Implementation]:
    """The implementations whose tasks may do ``capability``."""
    return models.Implementation.objects.filter(_condition(capability))


def implementation_q(capability: Capability, value: bool, prefix: str) -> Q:
    """Implementation rows (under ``prefix``) that may do ``capability`` (``value``), or never do."""
    q = Q(**{f"{prefix}id__in": implementations_that(capability).values("id")})
    return q if value else ~q


def action_q(capability: Capability, value: bool, prefix: str) -> Q:
    """Action rows (under ``prefix``) with an implementation that may do ``capability`` (``value``),
    or with none that does."""
    q = Q(**{f"{prefix}id__in": implementations_that(capability).values("action_id")})
    return q if value else ~q


DESCRIPTIONS: dict[Capability, tuple[str, str]] = {
    # (implementation, action)
    "asks": (
        "Whether its tasks may ask a person (hold until answered): it declares an ask, or `dynamicAsks`. False: never asks.",
        "Whether any of its implementations may ask a person (a declared ask, or `dynamicAsks`). False: none ever asks.",
    ),
    "informs": (
        "Whether its tasks may show a person a blok while they go on: it declares an inform, or `dynamicInforms`. False: never informs.",
        "Whether any of its implementations may show a person a blok (a declared inform, or `dynamicInforms`). False: none ever informs.",
    ),
    "calls": (
        "Whether its tasks may call other actions: it declares a dependency, or `dynamicCalls`. False: never calls.",
        "Whether any of its implementations may call other actions (a declared dependency, or `dynamicCalls`). False: none ever calls.",
    ),
    "dynamic_asks": (
        "Whether it declares `dynamicAsks`: its tasks may ask through a blok made at run time.",
        "Whether any of its implementations declares `dynamicAsks`.",
    ),
    "dynamic_informs": (
        "Whether it declares `dynamicInforms`: its tasks may show a blok made at run time.",
        "Whether any of its implementations declares `dynamicInforms`.",
    ),
    "dynamic_calls": (
        "Whether it declares `dynamicCalls`: its tasks may call actions found at run time.",
        "Whether any of its implementations declares `dynamicCalls`.",
    ),
}

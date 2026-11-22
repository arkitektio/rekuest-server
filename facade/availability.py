"""Whether an action can run, as one rank the database computes: what lists group, order and filter by.

0 RUNNABLE: ``Action.assignable`` (an agent that can take it now).
1 RECENT: an implementing agent sent a heartbeat within ``liveness.RECENTLY_SEEN``.
2 OFFLINE: implemented, but no implementing agent heard from for longer.
3 NONE: no implementation at all.
"""

from django.db.models import Case, Exists, IntegerField, OuterRef, Value, When

from facade import enums, liveness, models

ORDER: tuple[enums.ActionAvailability, ...] = (
    enums.ActionAvailability.RUNNABLE,
    enums.ActionAvailability.RECENT,
    enums.ActionAvailability.OFFLINE,
    enums.ActionAvailability.NONE,
)


def rank(prefix: str = "") -> Case:
    """The rank of the action at ``prefix`` (``""`` for an action queryset itself)."""
    implementations = models.Implementation.objects.filter(action=OuterRef(f"{prefix}pk"))
    return Case(
        When(**{f"{prefix}assignable": True}, then=Value(0)),
        When(Exists(implementations.filter(liveness.agent_recent_q("agent__"))), then=Value(1)),
        When(Exists(implementations), then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )


def of_rank(value: int) -> enums.ActionAvailability:
    """The availability a rank stands for."""
    return ORDER[value]


def ranks(values: list[enums.ActionAvailability]) -> list[int]:
    """The ranks these availabilities stand for."""
    return [ORDER.index(value) for value in values]

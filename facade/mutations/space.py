"""Keeping where agents stand in a space (a room), within the caller's organization.

Nothing here makes a space or decides what it shows: an action's ``<Space room=...>`` blok
places the run's dependencies, and the first pose someone keeps makes the space its ``room``
names. A pose is per agent, the same for every viewer.
"""

import math
from typing import cast

import strawberry
from kante.types import Info

from facade import inputs, models, types


def _affine(matrix: list[list[float]]) -> list[list[float]]:
    """A pose: a 4x4 matrix of finite numbers."""
    if len(matrix) != 4 or any(len(row) != 4 for row in matrix) or not all(math.isfinite(value) for row in matrix for value in row):
        raise ValueError("A pose is 4 rows of 4 finite numbers, row-major, in meters with Y up.")
    return matrix


def _room(room: str) -> str:
    reference = room.strip()
    if not reference:
        raise ValueError("A room has a reference (a blok's `room`).")
    return reference


def save_placement(info: Info, input: inputs.SavePlacementInput) -> types.Placement:
    """Keep where an agent stands in the space ``room`` names, making that space if none has it yet."""
    data = input.to_pydantic()
    organization = info.context.request.organization
    reference = _room(data.room)
    try:
        agent = models.Agent.objects.get(id=data.agent, organization=organization)
    except models.Agent.DoesNotExist:
        raise PermissionError(f"No agent {data.agent} in your organization.") from None
    space, _ = models.Space.objects.get_or_create(
        organization=organization,
        reference=reference,
        defaults=dict(name=reference, creator=info.context.request.user),
    )
    placement, _ = models.Placement.objects.update_or_create(
        space=space,
        agent=agent,
        defaults=dict(role=data.role, affine_matrix=_affine(data.affine_matrix)),
    )
    space.save(update_fields=["updated_at"])
    return cast("types.Placement", placement)


def reset_placement(info: Info, input: inputs.ResetPlacementInput) -> strawberry.ID | None:
    """Forget where an agent was left in a space; the id of the forgotten placement, if there was one."""
    data = input.to_pydantic()
    placement = models.Placement.objects.filter(
        space__organization=info.context.request.organization,
        space__reference=_room(data.room),
        agent_id=data.agent,
    ).first()
    if placement is None:
        return None
    placement_id = placement.pk
    placement.delete()
    return strawberry.ID(str(placement_id))

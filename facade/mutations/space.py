"""Spaces, the agents placed in them, and the 3D bloks placed in them -- all within the caller's organization."""

import math
from typing import cast

import strawberry
from kante.types import Info

from facade import inputs, models, types


def _space(info: Info, space_id: str) -> models.Space:
    """The caller's organization's space; another organization's is not found."""
    try:
        return models.Space.objects.get(id=space_id, organization=info.context.request.organization)
    except models.Space.DoesNotExist:
        raise PermissionError(f"No space {space_id} in your organization.") from None


def _affine(matrix: list[list[float]] | None) -> list[list[float]] | None:
    """A placement's transform: a 4x4 matrix of finite numbers, or None for the identity."""
    if matrix is None:
        return None
    if len(matrix) != 4 or any(len(row) != 4 for row in matrix) or not all(math.isfinite(value) for row in matrix for value in row):
        raise ValueError("An affine matrix is 4 rows of 4 finite numbers, row-major, from the blok's frame into the space's.")
    return matrix


def create_space(info: Info, input: inputs.CreateSpaceInput) -> types.Space:
    data = input.to_pydantic()
    x, _ = models.Space.objects.update_or_create(
        name=data.name,
        organization=info.context.request.organization,
        defaults=dict(
            creator=info.context.request.user,
        ),
    )

    for placement_input in data.placements or []:
        if not placement_input.agent:
            raise ValueError("An agent placement names its agent; place a blok without one with `createSpacePlacement`.")

        models.Placement.objects.update_or_create(
            space=x,
            agent_id=placement_input.agent,
            blok_id=placement_input.blok,
            defaults=dict(
                role=placement_input.role or "just a member",
                affine_matrix=_affine(placement_input.affine_matrix),
            ),
        )

    return cast("types.Space", x)


def update_space(info: Info, input: inputs.UpdateSpaceInput) -> types.Space:
    data = input.to_pydantic()
    x = _space(info, data.id)
    if data.name is not None:
        x.name = data.name
    if data.description is not None:
        x.description = data.description
    x.save()
    return cast("types.Space", x)


def delete_space(info: Info, input: inputs.DeleteSpaceInput) -> strawberry.ID:
    data = input.to_pydantic()
    _space(info, data.id).delete()
    return strawberry.ID(data.id)


def create_placement(info: Info, input: inputs.CreatePlacementInput) -> types.Placement:
    data = input.to_pydantic()
    space = _space(info, data.space)

    placement, _ = models.Placement.objects.update_or_create(
        space=space,
        agent_id=data.agent,
        defaults=dict(
            role="just a member",
            blok_id=data.materialized_blok,
            affine_matrix=_affine(data.affine_matrix),
        ),
    )

    return cast("types.Placement", placement)


def update_placement(info: Info, input: inputs.UpdatePlacementInput) -> types.Placement:
    data = input.to_pydantic()
    x = models.Placement.objects.get(id=data.id, space__organization=info.context.request.organization)
    if data.role is not None:
        x.role = data.role
    if data.affine_matrix is not None:
        x.affine_matrix = _affine(data.affine_matrix)
    x.save()
    return cast("types.Placement", x)


def delete_placement(info: Info, input: inputs.DeletePlacementInput) -> strawberry.ID:
    data = input.to_pydantic()
    models.Placement.objects.get(id=data.id, space__organization=info.context.request.organization).delete()
    return strawberry.ID(data.id)


def create_space_placement(info: Info, input: inputs.CreateSpacePlacementInput) -> types.SpacePlacement:
    """Place a 3D blok in a space at an affine transform."""
    data = input.to_pydantic()
    space = _space(info, data.space)
    try:
        blok = models.MaterializedBlok.objects.get(id=data.blok, blok__organization=info.context.request.organization)
    except models.MaterializedBlok.DoesNotExist:
        raise PermissionError(f"No materialized blok {data.blok} in your organization.") from None
    placement = models.SpacePlacement.objects.create(space=space, blok=blok, name=data.name, affine_matrix=_affine(data.affine_matrix))
    return cast("types.SpacePlacement", placement)


def update_space_placement(info: Info, input: inputs.UpdateSpacePlacementInput) -> types.SpacePlacement:
    """Move or rename a placed 3D blok."""
    data = input.to_pydantic()
    placement = models.SpacePlacement.objects.get(id=data.id, space__organization=info.context.request.organization)
    if data.name is not None:
        placement.name = data.name
    if data.affine_matrix is not None:
        placement.affine_matrix = _affine(data.affine_matrix)
    placement.save()
    return cast("types.SpacePlacement", placement)


def delete_space_placement(info: Info, input: inputs.DeleteSpacePlacementInput) -> strawberry.ID:
    """Remove a placed 3D blok from its space."""
    data = input.to_pydantic()
    models.SpacePlacement.objects.get(id=data.id, space__organization=info.context.request.organization).delete()
    return strawberry.ID(data.id)

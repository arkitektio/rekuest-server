from typing import cast

import strawberry
from kante.types import Info

from facade import inputs, models, types


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
        if not placement_input.agent or not placement_input.model:
            raise ValueError("Both agent and model must be provided for each placement.")

        membership, _ = models.Placement.objects.update_or_create(
            space=x,
            agent_id=placement_input.agent,
            model_id=placement_input.model,
            blok_id=placement_input.blok,
            defaults=dict(
                role=placement_input.role or "just a member",
                affine_matrix=placement_input.affine_matrix,
                model_id=placement_input.model,
            ),
        )

    return cast("types.Space", x)


def update_space(info: Info, input: inputs.UpdateSpaceInput) -> types.Space:
    data = input.to_pydantic()
    x = models.Space.objects.get(id=data.id)
    if data.name is not None:
        x.name = data.name
    if data.description is not None:
        x.description = data.description
    x.save()
    return cast("types.Space", x)


def delete_space(info: Info, input: inputs.DeleteSpaceInput) -> strawberry.ID:
    data = input.to_pydantic()
    x = models.Space.objects.get(id=data.id)
    x.delete()
    return strawberry.ID(data.id)


def create_placement(info: Info, input: inputs.CreatePlacementInput) -> types.Placement:
    data = input.to_pydantic()
    space = models.Space.objects.get(id=data.space)

    placement, _ = models.Placement.objects.update_or_create(
        space=space,
        agent_id=data.agent,
        defaults=dict(
            role="just a member",
            model_id=data.model,
            blok_id=data.materialized_blok,
            affine_matrix=data.affine_matrix,
        ),
    )

    return cast("types.Placement", placement)


def update_placement(info: Info, input: inputs.UpdatePlacementInput) -> types.Placement:
    data = input.to_pydantic()
    x = models.Placement.objects.get(id=data.id)
    if data.role is not None:
        x.role = data.role
    if data.affine_matrix is not None:
        x.affine_matrix = data.affine_matrix
    if data.model is not None:
        x.model = data.model
    x.save()
    return cast("types.Placement", x)


def delete_placement(info: Info, input: inputs.DeletePlacementInput) -> strawberry.ID:
    data = input.to_pydantic()
    x = models.Placement.objects.get(id=data.id)
    x.delete()
    return strawberry.ID(data.id)

from kante.types import Info
from facade import types, models, inputs
import strawberry


def create_space(info: Info, input: inputs.CreateSpaceInput) -> types.Space:
    x, _ = models.Space.objects.update_or_create(
        name=input.name,
        organization=info.context.request.organization,
        defaults=dict(
            creator=info.context.request.user,
        ),
    )

    for placement_input in input.placements or []:
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

    return x


def update_space(info: Info, input: inputs.UpdateSpaceInput) -> types.Space:
    x = models.Space.objects.get(id=input.id)
    if input.name is not None:
        x.name = input.name
    if input.description is not None:
        x.description = input.description
    x.save()
    return x


def delete_space(info: Info, input: inputs.DeleteSpaceInput) -> strawberry.ID:
    x = models.Space.objects.get(id=input.id)
    x.delete()
    return input.id


def create_placement(info: Info, input: inputs.CreatePlacementInput) -> types.Placement:
    space = models.Space.objects.get(id=input.space)

    placement, _ = models.Placement.objects.update_or_create(
        space=space,
        agent_id=input.agent,
        defaults=dict(
            role="just a member",
            model_id=input.model,
            blok_id=input.materialized_blok,
            affine_matrix=input.affine_matrix,
        ),
    )

    return placement


def update_placement(info: Info, input: inputs.UpdatePlacementInput) -> types.Placement:
    x = models.Placement.objects.get(id=input.id)
    if input.role is not None:
        x.role = input.role
    if input.affine_matrix is not None:
        x.affine_matrix = input.affine_matrix
    if input.model is not None:
        x.model = input.model
    x.save()
    return x


def delete_placement(info: Info, input: inputs.DeletePlacementInput) -> strawberry.ID:
    x = models.Placement.objects.get(id=input.id)
    x.delete()
    return input.id

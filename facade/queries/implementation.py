import strawberry
from facade import inputs, models, types, managers
from rekuest_core.inputs import types as ritypes
from kante.types import Info


def implementation_at(
    info: Info,
    agent: strawberry.ID,
    interface: str | None = None,
    action_hash: str | None = None,
    demand: ritypes.ActionDemandInput | None = None,
) -> types.Implementation:
    if action_hash:
        return models.Implementation.objects.get(agent_id=agent, action__hash=action_hash)

    if demand:
        action_ids = managers.get_action_ids_by_action_demands([demand])[0]
        return models.Implementation.objects.get(agent_id=agent, action_id__in=action_ids)

    return models.Implementation.objects.get(agent_id=agent, interface=interface)


async def my_implementation_at(
    info: Info,
    action_id: strawberry.ID | None = None,
    interface: str | None = None,
) -> types.Implementation:
    # TODO: Hasch this

    agent, _ = await models.Agent.objects.aget_or_create(
        client=info.context.request.client,
        user=info.context.request.user,
        organization=info.context.request.organization,
    )

    if action_id:
        return await models.Implementation.objects.aget(agent=agent, action_id=action_id)

    if interface:
        return await models.Implementation.objects.aget(agent=agent, interface=interface)

    raise ValueError("Either action_id or interface must be provided")


def resolved_implementations(
    info: Info,
    resolution: strawberry.ID,
    dependency_key: str | None = None,
    method_key: str | None = None,
) -> list[types.Implementation]:
    resolved_dependencies = models.ResolvedDependency.objects.filter(
        resolution_id=resolution,
        dependency__key=dependency_key,
        key=method_key,
    ).all()

    return [rd.implementation for rd in resolved_dependencies]


def dependency_tree(info: Info, input: inputs.DependencyTreeInput) -> types.DependencyTree:
    """What an assign would bind. takt resolves it exactly as it would for the assign."""
    from facade.backend import controll_backend

    model = input.to_pydantic()
    return types.DependencyTree(_value=controll_backend.resolve_dependencies(info, model), _implementation=model.implementation)

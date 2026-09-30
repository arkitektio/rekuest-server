from kante.types import Info
from facade import types, models, inputs, logic


def auto_resolve(info: Info, input: inputs.AutoResolveInput) -> types.Resolution:
    implementation = models.Implementation.objects.get(id=input.implementation)
    resolution = models.Resolution.objects.create(
        name=f"Auto-resolve for {implementation.action.name}",
        creator=info.context.request.user,
        organization=info.context.request.organization,
        implementation=implementation,
    )

    logic.auto_resolve(info, implementation, resolution)
    return resolution



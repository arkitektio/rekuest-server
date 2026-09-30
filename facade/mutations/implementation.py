import logging

from kante.types import Info

from facade import inputs, models, types

logger = logging.getLogger(__name__)


def create_higher_order_implementation(info: Info, input: inputs.CreateHigherOrderImplementationInput) -> types.Implementation:
    """Deploy a wrapper onto the agent of the implementation it wraps, linked to it.

    Served by agentd (``internal/higher-order/create``), which registers the wrapper as a
    declared implementation is registered and checks that it can work: same organization, no
    self-wrap, no nesting, matching kinds, every lower dependency covered. A wrapper deployed
    again under its interface is updated in place; an agent re-registering keeps it.
    """
    from facade import agentd

    model = input.to_pydantic()
    answer = agentd.call(
        "higher-order/create",
        {"principal": agentd._principal(info), "input": model.model_dump(mode="json", exclude_none=True)},
    )
    return models.Implementation.objects.get(pk=answer["implementation"])


def delete_implementation(info: Info, input: inputs.DeleteImplementationInput) -> str:
    implementation = models.Implementation.objects.get(id=input.implementation)

    implementation.delete()

    return input.implementation


def pin_implementation(info: Info, input: inputs.PinInput) -> types.Implementation:
    user = info.context.request.user

    agent = models.Implementation.objects.get(id=input.id)
    if input.pin:
        agent.pinned_by.add(user)
    else:
        agent.pinned_by.remove(user)
    agent.save()
    return agent

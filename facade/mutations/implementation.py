import logging

from kante.types import Info

from facade import agentd, inputs, models, types
from facade.types.base import scoped_get

logger = logging.getLogger(__name__)


def create_higher_order_implementation(info: Info, input: inputs.CreateHigherOrderImplementationInput) -> types.Implementation:
    """Deploy a wrapper onto the agent of the implementation it wraps, linked to it.

    Served by agentd (``internal/higher-order/create``), which registers the wrapper as a
    declared implementation is registered and checks that it can work: same organization, no
    self-wrap, no nesting, matching kinds, every lower dependency covered. A wrapper deployed
    again under its interface is updated in place; an agent re-registering keeps it.
    """

    model = input.to_pydantic()
    answer = agentd.call(
        "higher-order/create",
        {"principal": agentd._principal(info), "input": model.model_dump(mode="json", exclude_none=True)},
    )
    return models.Implementation.objects.get(pk=answer["implementation"])


def delete_implementation(info: Info, input: inputs.DeleteImplementationInput) -> str:
    """Delete an implementation. agentd owns the rows and their cascade; both sides scope it to the caller's organization."""
    implementation = scoped_get(models.Implementation, info, input.implementation, field="agent__organization")
    agentd.call("implementation/delete", {"principal": agentd._principal(info), "implementation": str(implementation.pk)})
    return input.implementation



import logging
from typing import cast

from kante.types import Info

from facade import inputs, models, takt, takt_api, types
from facade.caller_context import CallerContext
from facade.takt_api import Principal
from facade.types.base import scoped_get

logger = logging.getLogger(__name__)


def create_higher_order_implementation(info: Info, input: inputs.CreateHigherOrderImplementationInput) -> types.Implementation:
    """Deploy a wrapper onto the agent of the implementation it wraps, linked to it.

    Served by takt (``internal/higher-order/create``), which registers the wrapper as a
    declared implementation is registered and checks that it can work: same organization, no
    self-wrap, no nesting, matching kinds, every lower dependency covered. A wrapper deployed
    again under its interface is updated in place; an agent re-registering keeps it.
    """

    request = takt_api.CreateHigherOrderRequest(principal=Principal.of(CallerContext.from_info(info)), input=input.to_pydantic())
    return cast("types.Implementation", models.Implementation.objects.get(pk=takt.call(takt_api.CREATE_HIGHER_ORDER, request).implementation))


def delete_implementation(info: Info, input: inputs.DeleteImplementationInput) -> str:
    """Delete an implementation. takt owns the rows and their cascade; both sides scope it to the caller's organization."""
    data = input.to_pydantic()
    implementation = scoped_get(models.Implementation, info, data.implementation, field="agent__organization")
    takt.call(takt_api.DELETE_IMPLEMENTATION, takt_api.DeleteImplementationRequest(principal=Principal.of(CallerContext.from_info(info)), implementation=str(implementation.pk)))
    return data.implementation

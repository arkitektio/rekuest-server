"""The control backend: assigns, controls, agent operations and probes, served by takt.

takt owns the agent sockets, so it is the one place that writes task state and dispatches;
see :mod:`facade.takt` for the wire.
"""

from facade import models
from facade.takt import TaktControllBackend
from facade.caller_context import CallerContext


def get_caller_for_context(ctx: CallerContext) -> models.Caller:
    """The Caller row of an identity (the requester recorded on work it originates)."""
    return models.Caller.objects.get_or_create(user=ctx.user, client=ctx.client, organization=ctx.organization)[0]


controll_backend = TaktControllBackend()

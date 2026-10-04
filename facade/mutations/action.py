import strawberry
from kante.types import Info

from facade import takt, takt_api
from facade.caller_context import CallerContext
from facade.takt_api import Principal


def cleanup_actions(info: Info, action_ids: list[strawberry.ID] | None = None) -> int:
    """Delete the caller's organization's Actions that no implementation references.

    Always organization-scoped. takt owns the rows: it deletes them with their task history.
    Returns how many actions went.
    """
    request = takt_api.CleanupActionsRequest(principal=Principal.of(CallerContext.from_info(info)), actions=[str(action) for action in action_ids] if action_ids else None)
    return takt.call(takt_api.CLEANUP_ACTIONS, request).deleted

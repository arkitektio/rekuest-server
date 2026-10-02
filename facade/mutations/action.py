import strawberry
from kante.types import Info

from facade import takt


def cleanup_actions(info: Info, action_ids: list[strawberry.ID] | None = None) -> int:
    """Delete the caller's organization's Actions that no implementation references.

    Always organization-scoped. takt owns the rows: it deletes them with their task history.
    Returns how many actions went.
    """
    payload: dict = {"principal": takt._principal(info)}
    if action_ids:
        payload["actions"] = [str(action) for action in action_ids]
    return int(takt.call("action/cleanup", payload)["deleted"])

"""The identity of whoever is originating a task, transport-independent.

The postman ``assign`` path historically read identity straight off a Strawberry
``Info`` (``info.context.request.{user,client,organization,membership}``). The agent
WebSocket has no ``Info`` — it has the registered ``Agent``. ``CallerContext`` is the
small value object both transports build, so the backend never has to know which one it
came from.

Use :meth:`CallerContext.coerce` at public entry points: it passes a ``CallerContext``
through untouched and wraps a legacy ``Info`` via :meth:`from_info`, so existing GraphQL
call sites (and their tests) keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from kante.context import Client, Organization, User


@dataclass(frozen=True)
class CallerContext:
    """Who is originating work: the user/client/organization plus their roles.

    The identity fields are the structural ``kante`` Protocols (``.sub`` / ``.client_id`` /
    ``.slug``), satisfied by both the GraphQL request models and the authentikate ``Agent``
    relations, so the same context serves both transports.
    """

    user: User
    client: Optional[Client]
    organization: Optional[Organization]
    roles: List[str] = field(default_factory=list)

    @classmethod
    def from_info(cls, info: Any) -> "CallerContext":
        """Build from a Strawberry ``Info`` (the GraphQL postman path)."""
        request = info.context.request
        try:
            roles = list(request.membership.roles or [])
        except Exception:
            # Mirrors ``provenance.mint._current_roles``: roles are best-effort.
            roles = []
        # client/organization are read defensively: the provenance mint path only needs
        # ``user``/``roles``, and some request doubles omit the rest.
        return cls(
            user=request.user,
            client=getattr(request, "client", None),
            organization=getattr(request, "organization", None),
            roles=roles,
        )

    @classmethod
    def coerce(cls, value: Any) -> "CallerContext":
        """Return ``value`` if it is already a ``CallerContext``, else wrap a legacy ``Info``."""
        if isinstance(value, cls):
            return value
        return cls.from_info(value)

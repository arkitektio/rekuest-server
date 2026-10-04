"""The identity of whoever is originating a task, transport-independent.

A GraphQL resolver reads identity off its ``Info`` (``info.context.request``); the hub's own
provisioning has the rows of a service agent and no request. ``CallerContext`` is the small value
both build, so what acts on an identity never has to know where it came from. Resolvers build
it at their edge with :meth:`CallerContext.from_info`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from authentikate.models import Client, Membership, Organization, User
from kante.types import Info

from facade import models


@dataclass(frozen=True)
class CallerContext:
    """Who is originating work: the user, client and organization rows, plus the roles."""

    user: User
    client: Client | None
    organization: Organization | None
    roles: list[str] = field(default_factory=list)

    def caller(self) -> models.Caller:
        """The Caller row of this identity: the requester recorded on work it originates."""
        return models.Caller.objects.get_or_create(user=self.user, client=self.client, organization=self.organization)[0]

    @classmethod
    def from_info(cls, info: Info) -> CallerContext:
        """The identity of a GraphQL request.

        A request promises only protocols (``kante.context``); what authentikate puts on it are
        its own rows, and that is checked here once instead of assumed everywhere else.
        """
        request = info.context.request
        user = request.user
        if not isinstance(user, User):
            raise TypeError(f"The request's user is a {type(user).__name__}, not an authentikate user")
        # A request says "not set" by raising: a token may carry no client or organization.
        try:
            client = request.client
        except ValueError:
            client = None
        try:
            organization = request.organization
        except ValueError:
            organization = None
        try:
            membership = request.membership
        except ValueError:
            membership = None
        return cls(
            user=user,
            client=client if isinstance(client, Client) else None,
            organization=organization if isinstance(organization, Organization) else None,
            roles=[str(role) for role in membership.roles or []] if isinstance(membership, Membership) else [],
        )

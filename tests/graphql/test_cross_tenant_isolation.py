"""Cross-tenant isolation: a caller in org A must not see org B's rows.

The audit found that scoping exists on only 4 of ~35 auto-generated types (via
``facade.types.base.build_prescoped_queryset``), so most of the GraphQL surface returns rows
from every organization to any authenticated user. Nothing in the suite asserted otherwise —
before this file there were exactly two negative-authz assertions in the whole test suite, both
about probes.

``test_actions_are_scoped_to_the_callers_organization`` is the control: ``Action`` *is* one of
the four scoped types, so it passes and proves the harness itself is sound. The remaining tests
exercise unscoped types and are expected to FAIL until scoping is added — they are the
executable statement of the finding.
"""

from dataclasses import dataclass
from typing import TypeAlias

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from authentikate.expand import (
    aexpand_client_from_token,
    aexpand_organization_from_token,
    aexpand_user_from_token,
)
from authentikate.models import Client, Membership, Organization, User
from authentikate.utils import authenticate_token_or_none
from kante.context import HttpContext, UniversalRequest
from strawberry.http.temporal_response import TemporalResponse

from facade.models import Action, Agent, Caller, State
from facade.schema import schema
from tests.factories import (
    TEST_TOKEN,
    _build_state_for_agent,
    create_action_for_organization,
    create_agent_for_registry,
)

OTHER_TOKEN = "test-other"

#: A context and the identity it is authenticated as (``tenant_context``'s answer).
TenantContext: TypeAlias = tuple[HttpContext, User, Client, Organization]


def tenant_context(
    token: str = TEST_TOKEN,
) -> TenantContext:
    """An HttpContext authenticated by ``token`` plus the identity it resolves to.

    ``AuthentikateExtension`` overwrites the request's user/client/organization from the bearer
    token on every execution and rejects requests without one, so a tenant can only be expressed
    as a token: ``TEST_TOKEN`` lives in organization 1, ``OTHER_TOKEN`` in organization 2 (see
    ``rekuest/settings_test.py``). Contexts that pre-set a different organization and still sent
    ``Bearer test`` silently collapsed into the same tenant, which made every cross-tenant
    assertion vacuous.
    """
    decoded = async_to_sync(authenticate_token_or_none)(token)
    assert decoded is not None, f"the static token {token!r} does not authenticate"
    user = async_to_sync(aexpand_user_from_token)(decoded)
    client = async_to_sync(aexpand_client_from_token)(decoded)
    org = async_to_sync(aexpand_organization_from_token)(decoded)
    membership, _ = Membership.objects.get_or_create(user=user, organization=org)
    request = UniversalRequest(
        _extensions={"token": token},
        _client=client,  # type: ignore[arg-type]
        _user=user,  # type: ignore[arg-type]
        _organization=org,  # type: ignore[arg-type]
    )
    request.set_membership(membership)  # type: ignore[attr-defined]
    return (
        HttpContext(
            request=request,
            response=TemporalResponse(),
            headers={"Authorization": f"Bearer {token}"},
            type="http",
        ),
        user,
        client,
        org,
    )


def organization_of(context: HttpContext) -> Organization:
    """The organization a test context is made for, as the model it is: kante types the
    request's organization as a protocol."""
    organization = context.request.organization
    assert isinstance(organization, Organization)
    return organization


def _context_for(user: User, client: Client, org: Organization) -> HttpContext:
    """A context acting as ``user`` in ``org`` for **direct resolver calls only**.

    Valid when a test invokes a resolver function with ``SimpleNamespace(context=...)``: no
    extension runs, so the preset organization is honoured. It must not be passed to
    ``schema.execute`` -- there the extension re-derives the identity from the token (see
    ``tenant_context``).
    """
    membership, _ = Membership.objects.get_or_create(user=user, organization=org)
    request = UniversalRequest(
        _extensions={"token": "test"},
        _client=client,  # type: ignore[arg-type]
        _user=user,  # type: ignore[arg-type]
        _organization=org,  # type: ignore[arg-type]
    )
    request.set_membership(membership)  # type: ignore[attr-defined]
    return HttpContext(
        request=request, response=TemporalResponse(), headers={}, type="http"
    )


@dataclass(frozen=True)
class Tenant:
    """One tenant's context and the rows seeded for it."""

    context: HttpContext
    org: Organization
    agent: Agent
    state: State
    action: Action


@sync_to_async
def _seed_two_tenants(prefix: str) -> dict[str, Tenant]:
    """Two complete, independent tenants, each with an agent, a state and an action."""
    tenants: dict[str, Tenant] = {}
    for side, token in (("a", TEST_TOKEN), ("b", OTHER_TOKEN)):
        context, user, client, org = tenant_context(token)
        caller, _ = Caller.objects.get_or_create(
            client=client, user=user, organization=org
        )
        agent = create_agent_for_registry(caller, user, org, f"{prefix}-{side}")
        state = _build_state_for_agent(
            agent.pk, f"{prefix}-{side}-iface", f"{prefix}-{side}"
        )
        action = create_action_for_organization(org, f"{prefix}-{side}-act")
        tenants[side] = Tenant(
            context=context, org=org, agent=agent, state=state, action=action
        )
    return tenants


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestCrossTenantIsolation:
    async def test_actions_are_scoped_to_the_callers_organization(
        self, authenticated_context: HttpContext
    ) -> None:
        """Control: Action has a get_queryset scope, so this must pass.

        ``authenticated_context`` is requested only to pull in the ``db`` + ``backend_stack``
        session setup; each test builds its own per-tenant contexts.
        """
        t = await _seed_two_tenants("xt-actions")

        result = await schema.execute(
            "query { actions { id } }", context_value=t["a"].context
        )

        assert not result.errors, result.errors
        assert result.data is not None
        returned = {row["id"] for row in result.data["actions"]}
        assert str(t["b"].action.pk) not in returned, "org A can see org B's Action"

    async def test_single_state_lookup_is_scoped(
        self, authenticated_context: HttpContext
    ) -> None:
        """facade/schema.py:104 resolves State.objects.get(id=id) with no organization filter."""
        t = await _seed_two_tenants("xt-state-by-id")

        result = await schema.execute(
            "query Q($id: ID!) { state(id: $id) { id interface } }",
            context_value=t["a"].context,
            variable_values={"id": str(t["b"].state.pk)},
        )

        assert result.errors or result.data.get("state") is None, (
            "org A fetched org B's State by id"
        )

    async def test_states_are_scoped_to_the_callers_organization(
        self, authenticated_context: HttpContext
    ) -> None:
        """The root `states` list hands out the caller's organization's states only."""
        t = await _seed_two_tenants("xt-states")

        result = await schema.execute(
            "query { states(pagination: {limit: 1000}) { id interface } }",
            context_value=t["a"].context,
        )

        assert not result.errors, result.errors
        assert result.data is not None
        returned = {row["id"] for row in result.data["states"]}
        assert str(t["a"].state.pk) in returned, "org A does not see its own State"
        assert str(t["b"].state.pk) not in returned, "org A can see org B's States"

        # Asking for org B's agent by id changes nothing: the scope comes first.
        filtered = await schema.execute(
            "query Q($agent: ID!) { states(filters: {agent: $agent}) { id } }",
            context_value=t["a"].context,
            variable_values={"agent": str(t["b"].agent.pk)},
        )
        assert not filtered.errors, filtered.errors
        assert filtered.data is not None
        assert filtered.data["states"] == [], "org A can see org B's States by agent"

    async def test_an_implementation_of_another_organization_cannot_be_deleted(
        self, authenticated_context: HttpContext, takt: str
    ) -> None:
        """deleteImplementation fetched by bare id: org A could delete org B's implementation."""
        from facade.models import Implementation

        t = await _seed_two_tenants("xt-impl-delete")
        theirs = await sync_to_async(Implementation.objects.create)(
            agent=t["b"].agent, action=t["b"].action, interface="theirs"
        )
        mine = await sync_to_async(Implementation.objects.create)(
            agent=t["a"].agent, action=t["a"].action, interface="mine"
        )
        delete = "mutation M($id: ID!) { deleteImplementation(input: {implementation: $id}) }"

        refused = await schema.execute(
            delete,
            context_value=t["a"].context,
            variable_values={"id": str(theirs.pk)},
        )
        allowed = await schema.execute(
            delete,
            context_value=t["a"].context,
            variable_values={"id": str(mine.pk)},
        )

        assert refused.errors, "org A deleted org B's implementation"
        assert await sync_to_async(Implementation.objects.filter(pk=theirs.pk).exists)()
        assert not allowed.errors, allowed.errors
        assert not await sync_to_async(
            Implementation.objects.filter(pk=mine.pk).exists
        )()

    async def test_cleanup_actions_asks_takt_for_the_callers_organization_only(
        self, authenticated_context: HttpContext, takt: str
    ) -> None:
        """cleanupActions deletes takt-owned rows, so takt does it, told whose organization it is."""
        t = await _seed_two_tenants(
            "xt-cleanup"
        )  # neither tenant's action is implemented

        result = await schema.execute(
            "mutation { cleanupActions }", context_value=t["a"].context
        )

        assert not result.errors, result.errors
        from facade.models import Action

        # takt deletes them, for the organization that asked and no other.
        assert not await sync_to_async(
            Action.objects.filter(pk=t["a"].action.pk).exists
        )()
        assert await sync_to_async(Action.objects.filter(pk=t["b"].action.pk).exists)()
        assert await sync_to_async(Action.objects.filter(pk=t["b"].action.pk).exists)()

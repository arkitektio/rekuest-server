"""An admin pins a shortcut for users, in one app or in every app (``bindShortcut``).

The bound user sees it as ``pinned`` (and first, and under the ``pinned`` filter) only in the app
it was bound in; everyone else still sees the shortcut, unpinned. A user rekuest never saw is made
the way their first request would make them, so that request finds the same row.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import Client, Membership, User
from dokker import Deployment
from graphql import ExecutionResult
from kante.context import HttpContext, UniversalRequest
from strawberry.http.temporal_response import TemporalResponse

from facade import models
from facade.schema import schema
from tests.factories import create_action_for_organization

# The static tokens (``rekuest/settings_test.py``): "test" is rekuest's admin (sub 1), "test2"
# a second user in its organization (sub 2). Their app is a static token's default ``client_app``
# (the ``app`` key there is not a claim).
APP = "static_app"

BIND = """
    mutation Bind($shortcut: ID!, $users: [ID!]!, $app: String) {
        bindShortcut(input: {shortcut: $shortcut, users: $users, app: $app}) { id bindings { id app user { sub } } }
    }
"""
UNBIND = "mutation Unbind($id: ID!) { unbindShortcut(input: {id: $id}) }"
PINNED = "query { shortcuts(filters: {pinned: true}) { name pinned } }"
ORDERED = "query { shortcuts(ordering: [{pinned: DESC}, {name: ASC}]) { name pinned } }"
ME = "query { dashboards { id } }"


def _as(token: str) -> HttpContext:
    return HttpContext(request=UniversalRequest(_extensions={}), response=TemporalResponse(), headers={"Authorization": f"Bearer {token}"}, type="http")


def _reason(result: ExecutionResult) -> str | None:
    return result.errors[0].extensions.get("reason") if result.errors else None


def _shortcuts() -> dict[str, str]:
    """Four shortcuts in the admin's organization, by name."""
    admin = User.objects.get(sub="1")
    membership = Membership.objects.filter(user=admin).select_related("organization").first()
    assert membership is not None
    client = Client.objects.get(client_id="oinsoins")
    toolbox = models.Toolbox.objects.create(name="box", description="", creator=admin, client=client, organization=membership.organization)
    return {name: str(models.Shortcut.objects.create(name=name, toolbox=toolbox, creator=admin).pk) for name in ("a-here", "b-elsewhere", "c-everywhere", "d-unbound")}


async def _bind(shortcut: str, users: list[str], app: str | None, token: str = "test") -> ExecutionResult:
    return await schema.execute(BIND, variable_values={"shortcut": shortcut, "users": users, "app": app}, context_value=_as(token))


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_bound_shortcut_is_pinned_for_its_user_in_its_app(backend_stack: Deployment) -> None:
    """Pinned (and first) for the bound user in its app or every app, not in another app, not for the admin."""
    assert (await schema.execute(ME, context_value=_as("test"))).errors is None
    ids = await sync_to_async(_shortcuts)()

    # "test2" has never called rekuest: binding makes their user row.
    for name, app in (("a-here", APP), ("b-elsewhere", "another-app"), ("c-everywhere", None)):
        bound = await _bind(ids[name], ["2"], app)
        assert bound.errors is None, bound.errors

    admin = await User.objects.aget(sub="1")
    made = await User.objects.aget(sub="2")
    assert made.iss == admin.iss

    # Their first request finds that row, and sees what is pinned for them in this app.
    pinned = await schema.execute(PINNED, context_value=_as("test2"))
    assert pinned.errors is None, pinned.errors
    assert sorted(s["name"] for s in pinned.data["shortcuts"]) == ["a-here", "c-everywhere"]
    assert await User.objects.filter(sub="2").acount() == 1

    ordered = await schema.execute(ORDERED, context_value=_as("test2"))
    assert ordered.errors is None, ordered.errors
    assert [(s["name"], s["pinned"]) for s in ordered.data["shortcuts"]] == [
        ("a-here", True),
        ("c-everywhere", True),
        ("b-elsewhere", False),
        ("d-unbound", False),
    ]

    # The admin bound them for someone else: nothing is pinned for the admin.
    mine = await schema.execute(PINNED, context_value=_as("test"))
    assert mine.errors is None and mine.data["shortcuts"] == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_binding_again_changes_nothing_and_unbinding_unpins(backend_stack: Deployment) -> None:
    """A second bind keeps the one binding; unbinding unpins."""
    assert (await schema.execute(ME, context_value=_as("test"))).errors is None
    ids = await sync_to_async(_shortcuts)()

    await _bind(ids["c-everywhere"], ["2"], None)
    again = await _bind(ids["c-everywhere"], ["2", "2"], None)
    assert again.errors is None, again.errors
    bindings = again.data["bindShortcut"]["bindings"]
    assert [(b["app"], b["user"]["sub"]) for b in bindings] == [(None, "2")]

    unbound = await schema.execute(UNBIND, variable_values={"id": bindings[0]["id"]}, context_value=_as("test"))
    assert unbound.errors is None, unbound.errors
    pinned = await schema.execute(PINNED, context_value=_as("test2"))
    assert pinned.data["shortcuts"] == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_only_an_admin_binds(backend_stack: Deployment) -> None:
    """A member who is not rekuest's admin is refused at the gate."""
    assert (await schema.execute(ME, context_value=_as("test"))).errors is None
    ids = await sync_to_async(_shortcuts)()

    refused = await _bind(ids["a-here"], ["2"], APP, token="test-member")
    assert _reason(refused) == "INSUFFICIENT_ROLE"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_another_organizations_admin_cannot_bind(backend_stack: Deployment) -> None:
    """An admin elsewhere cannot reach this organization's shortcut."""
    assert (await schema.execute(ME, context_value=_as("test"))).errors is None
    ids = await sync_to_async(_shortcuts)()

    refused = await _bind(ids["a-here"], ["3"], APP, token="test-other")
    assert refused.errors is not None
    assert not await models.ShortcutBinding.objects.filter(shortcut_id=ids["a-here"]).aexists()


MENU = """
    query {
        shortcuts(ordering: [{pinned: DESC}, {assignable: DESC}], pagination: {limit: 2}) { name }
    }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_menus_limit_keeps_the_pinned_shortcuts(backend_stack: Deployment) -> None:
    """The menu's order (pinned, then runnable, then `pk`) under its limit still lists what is pinned."""
    assert (await schema.execute(ME, context_value=_as("test"))).errors is None
    ids = await sync_to_async(_shortcuts)()
    # Bound last, so `pk` alone would put them last.
    await _bind(ids["d-unbound"], ["2"], APP)
    await _bind(ids["c-everywhere"], ["2"], None)

    menu = await schema.execute(MENU, context_value=_as("test2"))
    assert menu.errors is None, menu.errors
    assert sorted(s["name"] for s in menu.data["shortcuts"]) == ["c-everywhere", "d-unbound"]


CREATE = """
    mutation Create($action: ID!, $bindings: ShortcutBindingsInput) {
        createShortcut(input: {name: "pinned", action: $action, args: {}, bindings: $bindings}) { id toolbox { id } bindings { app user { sub } } }
    }
"""


def _action() -> str:
    admin = User.objects.get(sub="1")
    membership = Membership.objects.filter(user=admin).select_related("organization").first()
    assert membership is not None
    return str(create_action_for_organization(membership.organization, "bindcreate").pk)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_admin_creates_a_shortcut_pinned_for_members(backend_stack: Deployment) -> None:
    """`createShortcut` with bindings makes the shortcut, in this organization's toolbox, and its pins."""
    assert (await schema.execute(ME, context_value=_as("test"))).errors is None
    action = await sync_to_async(_action)()

    made = await schema.execute(CREATE, variable_values={"action": action, "bindings": {"users": ["2"], "app": APP}}, context_value=_as("test"))
    assert made.errors is None, made.errors
    assert made.data["createShortcut"]["bindings"] == [{"app": APP, "user": {"sub": "2"}}]
    admin = await User.objects.aget(sub="1")
    toolbox = await models.Toolbox.objects.select_related("organization").aget(pk=made.data["createShortcut"]["toolbox"]["id"])
    assert await Membership.objects.filter(user=admin, organization=toolbox.organization).aexists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_member_cannot_create_a_shortcut_pinned_for_others(backend_stack: Deployment) -> None:
    """Only the bindings need an admin; refused, nothing is made."""
    assert (await schema.execute(ME, context_value=_as("test"))).errors is None
    action = await sync_to_async(_action)()

    refused = await schema.execute(CREATE, variable_values={"action": action, "bindings": {"users": ["2"], "app": APP}}, context_value=_as("test-member"))
    assert refused.errors is not None
    assert not await models.Shortcut.objects.filter(name="pinned").aexists()

    plain = await schema.execute(CREATE, variable_values={"action": action, "bindings": None}, context_value=_as("test-member"))
    assert plain.errors is None, plain.errors

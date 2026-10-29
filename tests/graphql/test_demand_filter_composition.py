"""Port-demand filters compose with ``OR`` and ``NOT``, like every other filter field.

strawberry-django combines a custom filter's returned ``Q`` with ``OR``/``NOT``, but applies a
``queryset.filter()`` a field makes on its own unconditionally. ``{demands: A, OR: {demands: B}}``
has to be the union of what each demand finds, and ``{NOT: {demands: A}}`` the complement.
"""

from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from authentikate.models import App, Client, Release
from facade import models
from facade.schema import schema
from rekuest_core.inputs.models import ImplementationInputModel
from tests.models.test_action_matching import make_action
from tests.registered import create_blok, create_implementation

IMAGE = {"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": False}
#: An image arg that only takes images with ``axes == "c"``.
STRICT_IMAGE = {**IMAGE, "requires": [{"key": "axes", "operator": "EQUALS", "value": "c"}]}
TABLE = {"key": "table", "kind": "STRUCTURE", "identifier": "@mikro/table", "nullable": False}

TAKES_IMAGE = {"kind": "ARGS", "matches": [{"identifier": "@mikro/image"}]}
TAKES_TABLE = {"kind": "ARGS", "matches": [{"identifier": "@mikro/table"}]}

ACTIONS = """
    query Actions($filters: ActionFilter) {
        actions(filters: $filters) { name }
    }
"""

SHORTCUTS = """
    query Shortcuts($filters: ShortcutFilter) {
        shortcuts(filters: $filters) { name }
    }
"""

BLOKS = """
    query Bloks($filters: BlokFilter) {
        bloks(filters: $filters) { name }
    }
"""

AGENTS = """
    query Agents($filters: AgentFilter) {
        agents(filters: $filters) { name }
    }
"""

IMPLEMENTATIONS = """
    query Implementations($filters: ImplementationFilter) {
        implementations(filters: $filters) { interface }
    }
"""


def _catalog(context: HttpContext) -> SimpleNamespace:
    """Three of everything in the caller's organization: one taking an image (with ``axes == "c"``), one a table, one both."""
    org, user, client = context.request.organization, context.request.user, context.request.client
    shapes = {"image": [STRICT_IMAGE], "table": [TABLE], "both": [IMAGE, TABLE]}

    for name, args in shapes.items():
        make_action(org, f"composed-{name}", args=args)
        create_blok(org, user, f"blok-{name}", args)

    toolbox = models.Toolbox.objects.create(name="composed", description="", creator=user, client=client, organization=org)
    for name, args in shapes.items():
        action = make_action(org, f"shortcut-action-{name}", args=args)
        models.Shortcut.objects.create(name=f"shortcut-{name}", toolbox=toolbox, creator=user, action=action, args=args)

    for name, args in shapes.items():
        # One agent per shape: agents are unique per (client, user, organization).
        app = App.objects.create(identifier=f"agent-{name}-app")
        release = Release.objects.create(app=app, version="1.0.0")
        agent_client = Client.objects.create(client_id=f"composed-{name}-client")
        agent = models.Agent.objects.create(name=f"agent-{name}", app=app, release=release, user=user, client=agent_client, organization=org, hash=f"composed-{name}-agent")
        definition = {"key": f"impl-{name}", "version": "1", "name": f"Impl {name}", "kind": "FUNCTION", "args": args, "returns": []}
        create_implementation(ImplementationInputModel.model_validate({"interface": f"impl-{name}", "definition": definition}), agent)
    return SimpleNamespace(org=org)


async def _names(context: HttpContext, query: str, field: str, filters: dict[str, object], key: str = "name") -> set[str]:
    result = await schema.execute(query, context_value=context, variable_values={"filters": filters})
    assert result.errors is None, result.errors
    return {row[key] for row in result.data[field]}


def _composed_actions(names: set[str]) -> set[str]:
    """The test's own actions among ``names`` (named ``<prefix> action``): the shortcut and implementation fixtures add more."""
    return {name.removesuffix(" action") for name in names if name.startswith("composed-")}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestDemandComposition:
    async def test_actions_or_is_the_union(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", {"demands": [TAKES_IMAGE]})) == {"composed-image", "composed-both"}
        either = {"demands": [TAKES_IMAGE], "OR": {"demands": [TAKES_TABLE]}}
        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", either)) == {"composed-image", "composed-table", "composed-both"}
        object_either = {"objectDemands": [TAKES_IMAGE], "OR": {"objectDemands": [TAKES_TABLE]}}
        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", object_either)) == {"composed-image", "composed-table", "composed-both"}

    async def test_actions_not_is_the_complement(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", {"NOT": {"demands": [TAKES_IMAGE]}})) == {"composed-table"}

    async def test_actions_fields_still_and(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        both = {"demands": [TAKES_IMAGE], "objectDemands": [TAKES_TABLE]}
        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", both)) == {"composed-both"}
        # A union narrowed by another field nests the union under AND: (A or B) and name.
        narrowed = {"name": {"startsWith": "composed-t"}, "AND": {"demands": [TAKES_IMAGE], "OR": {"demands": [TAKES_TABLE]}}}
        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", narrowed)) == {"composed-table"}
        # Siblings of OR bind tighter: {A, OR: B, name} is (A and name) or B.
        flat = {"demands": [TAKES_IMAGE], "OR": {"demands": [TAKES_TABLE]}, "name": {"startsWith": "composed-t"}}
        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", flat)) == {"composed-table", "composed-both"}

    async def test_shortcuts_or_is_the_union(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        assert await _names(authenticated_context, SHORTCUTS, "shortcuts", {"demands": [TAKES_IMAGE]}) == {"shortcut-image", "shortcut-both"}
        either = {"demands": [TAKES_IMAGE], "OR": {"demands": [TAKES_TABLE]}}
        assert await _names(authenticated_context, SHORTCUTS, "shortcuts", either) == {"shortcut-image", "shortcut-table", "shortcut-both"}
        assert await _names(authenticated_context, SHORTCUTS, "shortcuts", {"NOT": {"demands": [TAKES_IMAGE]}}) == {"shortcut-table"}

    async def test_bloks_or_is_the_union(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        either = {"demands": [TAKES_IMAGE], "OR": {"demands": [TAKES_TABLE]}}
        assert await _names(authenticated_context, BLOKS, "bloks", either) == {"blok-image", "blok-table", "blok-both"}
        assert await _names(authenticated_context, BLOKS, "bloks", {"NOT": {"demands": [TAKES_IMAGE]}}) == {"blok-table"}

    async def test_implementation_actions_or_is_the_union(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        either = {"action": {"demands": [TAKES_IMAGE], "OR": {"demands": [TAKES_TABLE]}}}
        assert await _names(authenticated_context, IMPLEMENTATIONS, "implementations", either, key="interface") == {"impl-image", "impl-table", "impl-both"}
        either_action = {"actionDemand": {"argMatches": [{"identifier": "@mikro/image"}]}, "OR": {"actionDemand": {"argMatches": [{"identifier": "@mikro/table"}]}}}
        assert await _names(authenticated_context, IMPLEMENTATIONS, "implementations", either_action, key="interface") == {"impl-image", "impl-table", "impl-both"}

    async def test_shortcuts_check_descriptors_on_their_actions(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        def described(axes: str) -> dict[str, object]:
            return {"kind": "ARGS", "matches": [{"identifier": "@mikro/image", "descriptors": [{"key": "axes", "value": axes}]}]}

        assert await _names(authenticated_context, SHORTCUTS, "shortcuts", {"demands": [described("c")]}) == {"shortcut-image", "shortcut-both"}
        assert await _names(authenticated_context, SHORTCUTS, "shortcuts", {"demands": [described("z")]}) == {"shortcut-both"}

    async def test_agents_or_is_the_union(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        image = {"argMatches": [{"identifier": "@mikro/image"}]}
        table = {"argMatches": [{"identifier": "@mikro/table"}]}
        nothing = {"argMatches": [{"identifier": "@mikro/nothing"}]}
        assert await _names(authenticated_context, AGENTS, "agents", {"actionDemands": [image]}) == {"agent-image", "agent-both"}
        assert await _names(authenticated_context, AGENTS, "agents", {"actionDemands": [image], "OR": {"actionDemands": [table]}}) == {"agent-image", "agent-table", "agent-both"}
        assert await _names(authenticated_context, AGENTS, "agents", {"NOT": {"actionDemands": [image]}}) == {"agent-table"}
        # A demand no action meets matches no agent, alone or beside one that does.
        assert await _names(authenticated_context, AGENTS, "agents", {"actionDemands": [nothing]}) == set()
        assert await _names(authenticated_context, AGENTS, "agents", {"actionDemands": [nothing], "OR": {"actionDemands": [table]}}) == {"agent-table", "agent-both"}

    async def test_any_demands_is_the_union(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)
        either = {"anyDemands": [[TAKES_IMAGE], [TAKES_TABLE]]}

        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", either)) == {"composed-image", "composed-table", "composed-both"}
        assert await _names(authenticated_context, SHORTCUTS, "shortcuts", either) == {"shortcut-image", "shortcut-table", "shortcut-both"}
        assert await _names(authenticated_context, BLOKS, "bloks", either) == {"blok-image", "blok-table", "blok-both"}
        assert await _names(authenticated_context, IMPLEMENTATIONS, "implementations", {"action": either}, key="interface") == {"impl-image", "impl-table", "impl-both"}

    async def test_any_demands_sets_are_anded_within(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        both_or_nothing = {"anyDemands": [[TAKES_IMAGE, TAKES_TABLE], [{"kind": "ARGS", "matches": [{"identifier": "@mikro/nothing"}]}]]}
        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", both_or_nothing)) == {"composed-both"}

    async def test_any_demands_composes(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", {"NOT": {"anyDemands": [[TAKES_IMAGE], [TAKES_TABLE]]}})) == set()
        narrowed = {"anyDemands": [[TAKES_IMAGE], [TAKES_TABLE]], "name": {"startsWith": "composed-t"}}
        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", narrowed)) == {"composed-table"}

    async def test_any_demands_empty(self, authenticated_context: HttpContext) -> None:
        await sync_to_async(_catalog)(authenticated_context)

        assert _composed_actions(await _names(authenticated_context, ACTIONS, "actions", {"anyDemands": []})) == {"composed-image", "composed-table", "composed-both"}
        result = await schema.execute(ACTIONS, context_value=authenticated_context, variable_values={"filters": {"anyDemands": [[TAKES_IMAGE], []]}})
        assert result.errors is not None and "at least one demand" in result.errors[0].message

"""What a client asks for in one request instead of one per row, or in pages instead of whole.

- `Action.hasFits` and `Action.assignBloks` are answered for a whole list of actions in a fixed
  number of queries (an annotation, a prefetch), not one more query per action.
- `bloksForModels(refs)` answers every model a scene shows at once.
- `ClientFilter.search` and `BlokFilter.nameStartsWith` narrow on the server what clients used
  to load whole and narrow themselves.
- `dashboards`, `structures`, `interfaces` and `structurePackages` take `pagination`.
"""

import pytest
from asgiref.sync import async_to_sync
from authentikate.models import App, Client, Device, Release, User
from django.db import connection
from django.test.utils import CaptureQueriesContext

from facade import models
from facade.schema import schema
from tests.factories import TEST_TOKEN
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context

pytestmark = pytest.mark.django_db(transaction=True)


def _run(query: str, context, **variables):
    result = async_to_sync(schema.execute)(
        query, variable_values=variables or None, context_value=context
    )
    assert result.errors is None, result.errors
    return result.data


def _agent(organization, prefix: str) -> models.Agent:
    user = User.objects.create(
        username=f"{prefix}-user", password="x", sub=f"{prefix}-sub"
    )
    client = Client.objects.create(client_id=f"{prefix}-client")
    app = App.objects.create(identifier=f"{prefix}-app")
    release = Release.objects.create(app=app, version="1.0.0")
    return models.Agent.objects.create(
        app=app,
        release=release,
        user=user,
        client=client,
        organization=organization,
        hash=f"{prefix}-hash",
        name=prefix,
    )


def _blok(organization, creator, name: str, **fields) -> models.Blok:
    module = models.Module.objects.get_or_create(
        name="orkestrator", organization=organization
    )[0]
    return models.Blok.objects.create(
        name=name, organization=organization, creator=creator, module=module, **fields
    )


def _actions(
    organization, agent: models.Agent, count: int, prefix: str
) -> list[models.Action]:
    """``count`` actions on ``agent``: every other one with a fit and an assign blok."""
    actions = []
    for index in range(count):
        action = models.Action.objects.create(
            app=agent.app,
            key=f"{prefix}-{index}",
            version="1.0.0",
            name=f"{prefix} {index}",
            description="",
            hash=f"{prefix}-{index}-hash",
            organization=organization,
        )
        implementation = models.Implementation.objects.create(
            interface=f"{prefix}-{index}", action=action, agent=agent
        )
        if index % 2 == 0:
            models.ImplementationFit.objects.create(
                implementation=implementation,
                port_key="shape",
                compiled_jsonpath="$agent.memory_mib >= 1000",
                call={},
            )
            blok = _blok(
                organization,
                agent.user,
                f"{prefix}-{index}:assign",
                held_by_action=True,
                declaration_hash=f"{index:064d}",
            )
            models.ActionBlok.objects.create(
                action=action, context="PANEL", phase="ASSIGN", blok=blok
            )
        actions.append(action)
    return actions


LISTED = """query($ids: [ID!]!) {
  actions(filters: {ids: $ids}, pagination: {limit: 50}) {
    key
    hasFits
    assignBloks { id }
    bloks { id }
  }
}"""


class TestActionListCost:
    def _count(self, context, actions: list[models.Action]) -> tuple[int, list[dict]]:
        with CaptureQueriesContext(connection) as captured:
            data = _run(LISTED, context, ids=[str(action.pk) for action in actions])
        return len(captured.captured_queries), data["actions"]

    def test_fits_and_assign_bloks_are_answered_per_list_not_per_row(self):
        context, *_, organization = tenant_context(TEST_TOKEN)
        agent = _agent(organization, "qc-agent")
        small_queries, small = self._count(
            context, _actions(organization, agent, 2, "qcsmall")
        )
        big_queries, big = self._count(
            context, _actions(organization, agent, 8, "qcbig")
        )

        assert {row["key"]: row["hasFits"] for row in small} == {
            "qcsmall-0": True,
            "qcsmall-1": False,
        }
        assert {row["key"]: len(row["assignBloks"]) for row in big} == {
            f"qcbig-{index}": 1 if index % 2 == 0 else 0 for index in range(8)
        }
        assert all(row["bloks"] == [] for row in big)
        # Four times the rows, the same number of queries.
        assert big_queries == small_queries, (small_queries, big_queries)


class TestBloksForModels:
    def test_every_model_of_a_scene_in_one_request(self):
        context, user, _, organization = tenant_context(TEST_TOKEN)
        _, other_user, _, other_organization = tenant_context(OTHER_TOKEN)
        _blok(organization, user, "turret", for_model="sha256:turret")
        _blok(organization, user, "base", for_model="sha256:base")
        _blok(organization, user, "unrelated", for_model="sha256:elsewhere")
        _blok(other_organization, other_user, "theirs", for_model="sha256:turret")

        data = _run(
            "query($refs: [String!]!) { bloksForModels(refs: $refs) { name forModel } }",
            context,
            refs=["sha256:turret", "sha256:base"],
        )

        assert data["bloksForModels"] == [
            {"name": "base", "forModel": "sha256:base"},
            {"name": "turret", "forModel": "sha256:turret"},
        ]


class TestNarrowedOnTheServer:
    def test_clients_are_searched_by_app_version_device_and_id(self):
        context, *_ = tenant_context(TEST_TOKEN)
        app = App.objects.create(identifier="qc-microscope")
        release = Release.objects.create(app=app, version="4.2.0")
        device = Device.objects.create(device_id="qc-bench-7")
        found = Client.objects.create(
            client_id="qc-client-abc", release=release, device=device
        )
        Client.objects.create(client_id="qc-client-other")

        listed = "query($search: String) { clients(filters: {search: $search}) { clientId } }"

        def ids(search: str) -> set[str]:
            return {
                row["clientId"]
                for row in _run(listed, context, search=search)["clients"]
            }

        for term in ("microscope", "4.2.0", "BENCH-7", "client-abc"):
            assert found.client_id in ids(term), term
        assert ids("qc-microscope") == {found.client_id}

    def test_bloks_whose_name_a_new_one_would_take(self):
        context, user, _, organization = tenant_context(TEST_TOKEN)
        for name in ("Stage panel", "stage panel 2", "Other stage panel"):
            _blok(organization, user, name)

        data = _run(
            'query { bloks(filters: {nameStartsWith: "STAGE PANEL"}) { name } }',
            context,
        )

        assert sorted(row["name"] for row in data["bloks"]) == [
            "Stage panel",
            "stage panel 2",
        ]


class TestPagedDerivedLists:
    def test_structures_interfaces_and_packages_page_like_any_list(self):
        context, user, _, organization = tenant_context(TEST_TOKEN)
        agent = _agent(organization, "qc-ports")
        action = models.Action.objects.create(
            app=agent.app,
            key="qc-ports",
            version="1.0.0",
            name="qc ports",
            description="",
            hash="qc-ports-hash",
            organization=organization,
        )
        for index in range(5):
            models.ArgPort.objects.create(
                action=action,
                key=f"s{index}",
                kind="STRUCTURE",
                identifier=f"@qcpkg{index}/thing",
                index=index,
            )
            models.ArgPort.objects.create(
                action=action,
                key=f"i{index}",
                kind="INTERFACE",
                identifier=f"@qcpkg{index}/iface",
                index=10 + index,
            )

        def page(field: str, key: str, offset: int, limit: int) -> list[str]:
            query = f'query($p: OffsetPaginationInput) {{ {field}(search: "qcpkg", pagination: $p) {{ {key} }} }}'
            return [
                row[key]
                for row in _run(query, context, p={"offset": offset, "limit": limit})[
                    field
                ]
            ]

        whole = [
            row["identifier"]
            for row in _run(
                'query { structures(search: "qcpkg") { identifier } }', context
            )["structures"]
        ]
        assert len(whole) == 5
        assert (
            page("structures", "identifier", 0, 2)
            + page("structures", "identifier", 2, 2)
            + page("structures", "identifier", 4, 2)
            == whole
        )
        assert page("interfaces", "identifier", 1, 2) == [
            "@qcpkg1/iface",
            "@qcpkg2/iface",
        ]
        assert page("structurePackages", "key", 3, 10) == ["qcpkg3", "qcpkg4"]

    def test_dashboards_take_pagination(self):
        context, *_, organization = tenant_context(TEST_TOKEN)
        for index in range(3):
            models.Dashboard.objects.create(
                name=f"qc-board-{index}", organization=organization
            )

        data = _run(
            "query { dashboards(pagination: {offset: 0, limit: 2}) { name } }", context
        )

        assert len(data["dashboards"]) == 2

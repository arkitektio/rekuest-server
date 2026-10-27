"""Blok write path: updateBlok, materializeBlok and friends.

These mutations used to be untested; ``updateBlok`` could not succeed at all (it read fields its
input did not have) and ``materializeBlok`` violated its own unique constraint as soon as a blok
declared two dependencies.
"""

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import App, Client, Device, Organization, Release, User
from kante.context import HttpContext

from facade.models import Agent, Blok, BlokAgentMapping, BlokDependency, Caller, Dashboard, DashboardPlacement, MaterializedBlok, Module
from facade.schema import schema
from tests.graphql_ops import CREATE_BLOK, DELETE_MATERIALIZED_BLOK, MATERIALIZE_BLOK, UPDATE_BLOK, UPDATE_MATERIALIZED_BLOK


def _agent_in(org: Organization, user: User, prefix: str) -> Agent:
    """An agent of ``user`` in ``org`` with its own client, app and release."""
    client = Client.objects.create(client_id=f"{prefix}-client", device=Device.objects.create(device_id=f"{prefix}-device"))
    Caller.objects.create(client=client, user=user, organization=org)
    release = Release.objects.create(app=App.objects.create(identifier=f"{prefix}-app"), version="1.0.0")
    return Agent.objects.create(app=release.app, hash=f"{prefix}-hash", release=release, user=user, client=client, organization=org)


@sync_to_async
def _seed(context: HttpContext, prefix: str, *, camera_optional: bool = False) -> dict:
    """A blok with two dependencies, a dashboard and one agent per dependency, all in the caller's org."""
    org, user = context.request.organization, context.request.user
    catalog = Module.objects.get_or_create(name="default", organization=org)[0]
    blok = Blok.objects.create(name=f"{prefix}-blok", creator=user, organization=org, module=catalog)
    BlokDependency.objects.create(blok=blok, key="stage", optional=False)
    BlokDependency.objects.create(blok=blok, key="camera", optional=camera_optional)
    dashboard = Dashboard.objects.create(name=f"{prefix}-dash", organization=org)
    return {
        "blok": blok,
        "dashboard": dashboard,
        "stage_agent": _agent_in(org, user, f"{prefix}-stage"),
        "camera_agent": _agent_in(org, user, f"{prefix}-camera"),
    }


@sync_to_async
def _counts() -> tuple[int, int, int]:
    """Row counts of MaterializedBlok, BlokAgentMapping and DashboardPlacement."""
    return MaterializedBlok.objects.count(), BlokAgentMapping.objects.count(), DashboardPlacement.objects.count()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestBlokWritePath:
    async def test_update_blok_round_trip(self, authenticated_context: HttpContext) -> None:
        """Every field of UpdateBlokInput is applied; omitted fields stay untouched; dependencies replace wholesale."""
        created = await schema.execute(
            CREATE_BLOK,
            context_value=authenticated_context,
            variable_values={"input": {"name": "Updatable", "vocab": 2, "description": "before", "dependencies": [{"key": "stage"}], "locals": [{"key": "exposure", "default": 3, "kind": "INT"}]}},
        )
        assert not created.errors, created.errors
        blok_id = created.data["createBlok"]["id"]

        result = await schema.execute(
            UPDATE_BLOK,
            context_value=authenticated_context,
            variable_values={
                "input": {
                    "id": blok_id,
                    "module": "custom",
                    "components": [{"id": "html", "component": "Html", "children": [{"id": "root", "component": "Slider"}]}],
                    "dependencies": [{"key": "camera", "optional": True}],
                }
            },
        )

        assert not result.errors, result.errors
        blok = result.data["updateBlok"]
        assert blok["name"] == "Updatable"
        assert blok["description"] == "before"
        # updateBlok keeps what it is not given: the locals and the language version.
        assert blok["vocab"] == 2
        assert blok["locals"] == [{"key": "exposure", "kind": "INT", "default": 3, "persist": False}]
        assert blok["module"]["name"] == "custom"
        assert blok["dependencies"] == [{"key": "camera", "optional": True}]
        # The selection reads the root only: the html blok's `Html`, holding the Slider.
        assert blok["components"] == [{"id": "html", "component": "Html"}]

    async def test_materialize_blok_with_two_dependencies(self, authenticated_context: HttpContext) -> None:
        """Both mappings are written under their own keys, the placement is created, and a second materialization is a second row."""
        env = await _seed(authenticated_context, "two")
        variables = {
            "input": {
                "blok": str(env["blok"].id),
                "dashboard": str(env["dashboard"].id),
                "agentMappings": [
                    {"key": "stage", "agent": str(env["stage_agent"].id)},
                    {"key": "camera", "agent": str(env["camera_agent"].id)},
                ],
            }
        }

        first = await schema.execute(MATERIALIZE_BLOK, context_value=authenticated_context, variable_values=variables)
        assert not first.errors, first.errors
        mblok = first.data["materializeBlok"]
        assert mblok["name"] == "two-blok"
        assert {m["key"]: m["agent"]["id"] for m in mblok["agentMappings"]} == {
            "stage": str(env["stage_agent"].id),
            "camera": str(env["camera_agent"].id),
        }
        assert len(mblok["dashboardPlacements"]) == 1

        second = await schema.execute(MATERIALIZE_BLOK, context_value=authenticated_context, variable_values=variables)
        assert not second.errors, second.errors
        assert second.data["materializeBlok"]["id"] != mblok["id"]
        assert await sync_to_async(MaterializedBlok.objects.filter(blok=env["blok"]).count)() == 2

    async def test_materialize_optional_dependency_unmapped_succeeds(self, authenticated_context: HttpContext) -> None:
        """Materialize optional dependency unmapped succeeds."""
        env = await _seed(authenticated_context, "opt", camera_optional=True)

        result = await schema.execute(
            MATERIALIZE_BLOK,
            context_value=authenticated_context,
            variable_values={"input": {"blok": str(env["blok"].id), "agentMappings": [{"key": "stage", "agent": str(env["stage_agent"].id)}]}},
        )

        assert not result.errors, result.errors
        assert [m["key"] for m in result.data["materializeBlok"]["agentMappings"]] == ["stage"]

    async def test_materialize_required_dependency_unmapped_fails_atomically(self, authenticated_context: HttpContext) -> None:
        """Materialize required dependency unmapped fails atomically."""
        env = await _seed(authenticated_context, "req")

        result = await schema.execute(
            MATERIALIZE_BLOK,
            context_value=authenticated_context,
            variable_values={"input": {"blok": str(env["blok"].id), "dashboard": str(env["dashboard"].id), "agentMappings": []}},
        )

        assert result.errors and "required" in result.errors[0].message
        assert await _counts() == (0, 0, 0)

    async def test_materialize_rejects_unknown_mapping_key(self, authenticated_context: HttpContext) -> None:
        """Materialize rejects unknown mapping key."""
        env = await _seed(authenticated_context, "unk", camera_optional=True)

        result = await schema.execute(
            MATERIALIZE_BLOK,
            context_value=authenticated_context,
            variable_values={
                "input": {
                    "blok": str(env["blok"].id),
                    "agentMappings": [{"key": "stage", "agent": str(env["stage_agent"].id)}, {"key": "laser", "agent": str(env["camera_agent"].id)}],
                }
            },
        )

        assert result.errors and "laser" in result.errors[0].message
        assert await _counts() == (0, 0, 0)

    async def test_materialize_enforces_app_filter(self, authenticated_context: HttpContext) -> None:
        """Materialize enforces app filter."""
        env = await _seed(authenticated_context, "app", camera_optional=True)
        await sync_to_async(BlokDependency.objects.filter(blok=env["blok"], key="stage").update)(app_filter="some-other-app")

        result = await schema.execute(
            MATERIALIZE_BLOK,
            context_value=authenticated_context,
            variable_values={"input": {"blok": str(env["blok"].id), "agentMappings": [{"key": "stage", "agent": str(env["stage_agent"].id)}]}},
        )

        assert result.errors and "some-other-app" in result.errors[0].message
        assert await _counts() == (0, 0, 0)

    async def test_update_and_delete_materialized_blok(self, authenticated_context: HttpContext) -> None:
        """Update and delete materialized blok."""
        env = await _seed(authenticated_context, "upd", camera_optional=True)
        created = await schema.execute(
            MATERIALIZE_BLOK,
            context_value=authenticated_context,
            variable_values={"input": {"blok": str(env["blok"].id), "agentMappings": [{"key": "stage", "agent": str(env["stage_agent"].id)}]}},
        )
        assert not created.errors, created.errors
        mblok_id = created.data["materializeBlok"]["id"]

        updated = await schema.execute(
            UPDATE_MATERIALIZED_BLOK,
            context_value=authenticated_context,
            variable_values={
                "input": {
                    "id": mblok_id,
                    "agentMappings": [{"key": "stage", "agent": str(env["camera_agent"].id)}, {"key": "camera", "agent": str(env["camera_agent"].id)}],
                }
            },
        )
        assert not updated.errors, updated.errors
        assert {m["key"]: m["agent"]["id"] for m in updated.data["updateMaterializedBlok"]["agentMappings"]} == {
            "stage": str(env["camera_agent"].id),
            "camera": str(env["camera_agent"].id),
        }

        deleted = await schema.execute(DELETE_MATERIALIZED_BLOK, context_value=authenticated_context, variable_values={"input": {"id": mblok_id}})
        assert not deleted.errors, deleted.errors
        assert deleted.data["deleteMaterializedBlok"] is True
        assert await _counts() == (0, 0, 0)

    async def test_update_blok_validates_the_merged_manifest(self, authenticated_context: HttpContext) -> None:
        """New components are checked against the dependencies in force after the update: a
        dependency the tree calls but the blok does not declare is refused, never inferred."""
        created = await schema.execute(
            CREATE_BLOK,
            context_value=authenticated_context,
            variable_values={"input": {"name": "Checked", "vocab": 2, "dependencies": [{"key": "stage"}]}},
        )
        assert not created.errors, created.errors
        blok_id = created.data["createBlok"]["id"]
        components = [{"id": "html", "component": "Html", "children": [{"id": "root", "component": "Button", "props": [{"key": "onClick", "agentCall": {"dependency": "laser", "operation": "fire"}}]}]}]

        rejected = await schema.execute(UPDATE_BLOK, context_value=authenticated_context, variable_values={"input": {"id": blok_id, "components": components}})
        assert rejected.errors
        assert "'laser' is not a dependency of the blok" in rejected.errors[0].message
        assert 'Fix: dependencies=[AgentDependencyInput(key="laser")]' in rejected.errors[0].message

        accepted = await schema.execute(
            UPDATE_BLOK,
            context_value=authenticated_context,
            variable_values={"input": {"id": blok_id, "components": components, "dependencies": [{"key": "laser"}]}},
        )
        assert not accepted.errors, accepted.errors
        assert accepted.data["updateBlok"]["dependencies"] == [{"key": "laser", "optional": False}]

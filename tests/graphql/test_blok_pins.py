"""Per-user pins on bloks and materializations, of every kind. takt deletes both in SQL, so a pin
must not stop that: its rows go with them in the database."""

import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from kante.context import HttpContext

from facade.models import Blok, MaterializedBlok, Module
from facade.schema import schema
from tests.graphql.test_blok_revisions import PROVIDED, _agent_in

PIN = """
    mutation Pin($blok: PinInput!, $materialized: PinInput!) {
        pinBlok(input: $blok) { id pinned }
        pinMaterializedBlok(input: $materialized) { id pinned }
    }
"""

PINNED = """
    query Pinned {
        bloks(filters: { pinned: true }) { id pinned }
        materializedBloks(filters: { pinned: true }) { id pinned }
    }
"""


async def _run(
    context: HttpContext, operation: str, variables: dict | None = None
) -> dict:
    result = await schema.execute(
        operation, context_value=context, variable_values=variables
    )
    assert not result.errors, result.errors
    assert result.data is not None
    return result.data


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_pinned_provided_blok_and_declared_materialization_list_and_go_in_sql(
    authenticated_context: HttpContext,
) -> None:
    org, user = (
        authenticated_context.request.organization,
        authenticated_context.request.user,
    )

    @sync_to_async
    def provided() -> tuple[Blok, MaterializedBlok]:
        agent = _agent_in(org, user, "pins")
        catalog = Module.objects.get_or_create(name="orkestrator", organization=org)[0]
        blok = Blok.objects.create(
            name="panel",
            creator=user,
            organization=org,
            module=catalog,
            app=agent.app,
            declaration_hash="c" * 64,
            components=PROVIDED,
        )
        return blok, MaterializedBlok.objects.create(
            blok=blok, declared_by=agent, name="panel", description=""
        )

    blok, slot = await provided()
    pinned = await _run(
        authenticated_context,
        PIN,
        {
            "blok": {"id": str(blok.id), "pin": True},
            "materialized": {"id": str(slot.id), "pin": True},
        },
    )
    assert pinned["pinBlok"]["pinned"] and pinned["pinMaterializedBlok"]["pinned"]

    listed = await _run(authenticated_context, PINNED)
    assert listed["bloks"] == [{"id": str(blok.id), "pinned": True}]
    assert listed["materializedBloks"] == [{"id": str(slot.id), "pinned": True}]

    @sync_to_async
    def delete_as_takt() -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM facade_materializedblok WHERE id = %s", [slot.id]
            )
            cursor.execute("DELETE FROM facade_blok WHERE id = %s", [blok.id])

    await delete_as_takt()
    assert await _run(authenticated_context, PINNED) == {
        "bloks": [],
        "materializedBloks": [],
    }

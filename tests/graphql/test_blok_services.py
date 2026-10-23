"""A blok's services: the backends its ``query`` components read from, frozen at their versions.

Every base ``query`` names the service it runs on (``schema="mikro"``), and the blok must declare
that service. What is declared is stored with the blok and its revision, and read back as
``Blok.services``; a client compares the frozen version and schema hash with the live service.
"""

import pytest
from kante.context import HttpContext

from facade.blok_revisions import BlokContent
from facade.schema import schema
from rekuest_core import enums
from rekuest_core.inputs import models as rimodels

CREATE = """
mutation CreateBlok($input: CreateBlokInput!) {
  createBlok(input: $input) { id services { key service version schemaHash } }
}
"""

QUERY = {
    "id": "cells",
    "component": "query",
    "props": [
        {"key": "schema", "staticValue": "mikro"},
        {"key": "query", "staticValue": "query { arrayDatasets { id name } }"},
        {"key": "bind", "staticValue": "cells"},
    ],
}
MIKRO = {"key": "mikro", "service": "live.arkitekt.mikro", "version": "8", "schemaHash": "sha256:" + "ab" * 32}


def _node(**fields: object) -> rimodels.ComponentNodeInputModel:
    return rimodels.ComponentNodeInputModel.model_validate(fields)


def test_a_query_runs_on_a_declared_service() -> None:
    tree = [_node(id="root", component="div", children=[{"id": "q", "component": "query", "props": [{"key": "schema", "static_value": "mikro"}]}])]
    rimodels.check_blok_services(tree, [rimodels.BlokServiceInputModel(key="mikro")])
    with pytest.raises(ValueError, match=r"queries the 'mikro' service, which the blok does not declare"):
        rimodels.check_blok_services(tree, [])
    with pytest.raises(ValueError, match="duplicate key .mikro."):
        rimodels.check_blok_services([], [rimodels.BlokServiceInputModel(key="mikro"), rimodels.BlokServiceInputModel(key="mikro")])


def test_no_services_leave_the_content_hash_as_it_was() -> None:
    """Services count in the revision hash only when there are some, so older revisions keep theirs."""
    catalog = type("Catalog", (), {"pk": 1})()
    content = BlokContent(components=[], demo_state={}, context=enums.ComponentContext.HTML, catalog=catalog, dependencies=[])  # type: ignore[arg-type]
    with_services = content.but(services=[rimodels.BlokServiceInputModel(key="mikro")])
    assert content.but(services=[]).hash() == content.hash()
    assert with_services.hash() != content.hash()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestBlokServices:
    async def test_services_are_stored_and_read_back(self, authenticated_context: HttpContext) -> None:
        result = await schema.execute(
            CREATE,
            context_value=authenticated_context,
            variable_values={"input": {"name": "Cells", "components": [QUERY], "demoState": {"cells": None}, "services": [MIKRO]}},
        )
        assert not result.errors, result.errors
        assert result.data["createBlok"]["services"] == [MIKRO]

    async def test_a_query_on_an_undeclared_service_is_refused(self, authenticated_context: HttpContext) -> None:
        result = await schema.execute(
            CREATE,
            context_value=authenticated_context,
            variable_values={"input": {"name": "Cells", "components": [QUERY], "demoState": {"cells": None}}},
        )
        assert result.errors and "does not declare in its services" in result.errors[0].message

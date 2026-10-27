"""``materializedBloks`` filtered by the context its blok renders in.

A list of interfaces to open leaves out the logos agents and actions declare: they are
materialized like any blok, but they draw an icon, not a page.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from facade.models import Blok, MaterializedBlok, Module
from facade.schema import schema
from tests.factories import TEST_TOKEN
from tests.graphql.test_cross_tenant_isolation import tenant_context

MATERIALIZED_BLOKS = """
    query MaterializedBloks($filters: MaterializedBlokFilter) {
        materializedBloks(filters: $filters) {
            name
            blok { context }
        }
    }
"""


@sync_to_async
def _seed(prefix: str) -> HttpContext:
    """One materialized blok per context, in the token-"test" organization."""
    context, user, _client, org = tenant_context(TEST_TOKEN)
    catalog = Module.objects.get_or_create(name="default", organization=org)[0]
    for blok_context in ("HTML", "SCENE", "LOGO", "ACTION_LOGO"):
        blok = Blok.objects.create(name=f"{prefix}-{blok_context}", creator=user, organization=org, module=catalog, context=blok_context)
        MaterializedBlok.objects.create(blok=blok, name=blok.name, description="")
    return context


async def _contexts(context: HttpContext, prefix: str, contexts: list[str] | None) -> list[str]:
    filters: dict = {"search": prefix}
    if contexts is not None:
        filters["contexts"] = contexts
    result = await schema.execute(MATERIALIZED_BLOKS, context_value=context, variable_values={"filters": filters})
    assert not result.errors, result.errors
    return sorted(row["blok"]["context"] for row in result.data["materializedBloks"])


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestMaterializedBlokContextFilter:
    """The ``contexts`` key of ``MaterializedBlokFilter``."""

    async def test_interfaces_leave_out_the_logos(self, authenticated_context: HttpContext) -> None:
        """HTML and SCENE keep the interfaces and drop both kinds of logo."""
        context = await _seed("ctx-ui")
        assert await _contexts(context, "ctx-ui", ["HTML", "SCENE"]) == ["HTML", "SCENE"]

    async def test_logos_only(self, authenticated_context: HttpContext) -> None:
        """LOGO and ACTION_LOGO keep the logos alone."""
        context = await _seed("ctx-logo")
        assert await _contexts(context, "ctx-logo", ["LOGO", "ACTION_LOGO"]) == ["ACTION_LOGO", "LOGO"]

    async def test_no_contexts_keeps_everything(self, authenticated_context: HttpContext) -> None:
        """Without the key nothing is filtered by context."""
        context = await _seed("ctx-all")
        assert await _contexts(context, "ctx-all", None) == ["ACTION_LOGO", "HTML", "LOGO", "SCENE"]

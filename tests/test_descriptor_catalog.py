"""Descriptors and hosted structures as rows: listed, searched and linked through GraphQL. Hub-wide."""

import pytest
from asgiref.sync import sync_to_async

from facade import models, service_catalog
from facade.schema import schema
from tests.factories import TEST_TOKEN
from tests.graphql.test_cross_tenant_isolation import OTHER_TOKEN, tenant_context

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]

AXES = {"key": "@mikro/n_space_axes", "type": "INT", "description": "How many space axes it has"}
CHANNELS = {"key": "@mikro/n_channels", "type": "INT", "description": "Its total extent along its CHANNEL axes"}


def _structures(*declared: dict) -> list[service_catalog.StructureManifest]:
    """Structures as a service's manifest declares them."""
    return [service_catalog.StructureManifest.model_validate(structure) for structure in declared]


def _catalogue() -> None:
    mikro = models.Service.objects.create(name="mikro", description="Microscopy data")
    service_catalog._sync_structures(
        mikro,
        _structures(
            {"identifier": "@mikro/arraydataset", "label": "Array Dataset", "description": "A multi-dimensional array", "descriptors": [AXES, CHANNELS]},
            {"identifier": "@mikro/lens", "label": "Lens", "descriptors": [AXES, CHANNELS]},
            {"identifier": "@mikro/folder", "label": "Folder", "descriptors": []},
        ),
    )
    bank = models.Service.objects.create(name="bank")
    service_catalog._sync_structures(bank, _structures({"identifier": "@bank/category", "label": "Category", "descriptors": [{"key": "@bank/kind", "type": "STRING", "description": "Expense, income or transfer"}]}))


async def _run(query: str, context, **variables):
    result = await schema.execute(query, variable_values=variables or None, context_value=context)
    assert result.errors is None, result.errors
    return result.data


async def test_descriptors_are_searched_by_key_and_by_meaning(authenticated_context):
    context = (await sync_to_async(tenant_context)(TEST_TOKEN))[0]
    await sync_to_async(_catalogue)()
    query = "query($filters: StructureDescriptorFilter, $ordering: [StructureDescriptorOrder!]! = []) { descriptors(filters: $filters, ordering: $ordering) { key type structure { identifier } service { name } } }"

    async def found(**variables):
        return [(row["key"], row["structure"]["identifier"]) for row in (await _run(query, context, **variables))["descriptors"]]

    assert len(await found()) == 5
    assert await found(filters={"search": "channel"}) == [("@mikro/n_channels", "@mikro/arraydataset"), ("@mikro/n_channels", "@mikro/lens")]
    assert await found(filters={"search": "transfer"}) == [("@bank/kind", "@bank/category")]  # found by what it means
    assert await found(filters={"structure": "@mikro/lens"}, ordering=[{"key": "DESC"}]) == [("@mikro/n_space_axes", "@mikro/lens"), ("@mikro/n_channels", "@mikro/lens")]
    assert await found(filters={"type": ["string"]}) == [("@bank/kind", "@bank/category")]
    assert await found(filters={"service": "bank"}) == [("@bank/kind", "@bank/category")]
    assert len(await found(filters={"package": "mikro", "key": "@mikro/n_channels"})) == 2

    # The catalog is the hub's, the same for every organization.
    other = (await sync_to_async(tenant_context)(OTHER_TOKEN))[0]
    assert len((await _run(query, other))["descriptors"]) == 5


async def test_a_descriptor_and_a_hosted_structure_are_fetched_and_lead_to_each_other(authenticated_context):
    context = (await sync_to_async(tenant_context)(TEST_TOKEN))[0]
    await sync_to_async(_catalogue)()
    channels = await models.Descriptor.objects.aget(key="@mikro/n_channels", structure__identifier="@mikro/arraydataset")

    one = (await _run("query($id: ID!) { descriptor(id: $id) { id key description hostedStructure { identifier label } sharedWith { identifier } } }", context, id=str(channels.pk)))["descriptor"]
    assert one == {
        "id": str(channels.pk),
        "key": "@mikro/n_channels",
        "description": "Its total extent along its CHANNEL axes",
        "hostedStructure": {"identifier": "@mikro/arraydataset", "label": "Array Dataset"},
        "sharedWith": [{"identifier": "@mikro/lens"}],
    }

    hosted = (await _run("query($id: ID!) { hostedStructure(id: $id) { identifier key package { key } service { name } descriptors { key type } structure { identifier inputUsages { portKey } } } }", context, id=str(channels.structure_id)))["hostedStructure"]
    assert hosted["descriptors"] == [{"key": "@mikro/n_space_axes", "type": "INT"}, {"key": "@mikro/n_channels", "type": "INT"}]  # in the declared order
    assert (hosted["key"], hosted["package"], hosted["service"]) == ("arraydataset", {"key": "mikro"}, {"name": "mikro"})
    assert hosted["structure"] == {"identifier": "@mikro/arraydataset", "inputUsages": []}

    # The wider Structure type hands out the same rows.
    structure = (await _run('query { structure(identifier: "@mikro/arraydataset") { hosted { id } descriptors { id key } } }', context))["structure"]
    assert structure["hosted"] == {"id": str(channels.structure_id)}
    assert {"id": str(channels.pk), "key": "@mikro/n_channels"} in structure["descriptors"]


async def test_hosted_structures_are_searched(authenticated_context):
    context = (await sync_to_async(tenant_context)(TEST_TOKEN))[0]
    await sync_to_async(_catalogue)()
    query = "query($filters: HostedStructureFilter) { hostedStructures(filters: $filters, ordering: [{identifier: ASC}]) { identifier } }"

    async def found(**filters):
        return [row["identifier"] for row in (await _run(query, context, filters=filters or None))["hostedStructures"]]

    assert await found() == ["@bank/category", "@mikro/arraydataset", "@mikro/folder", "@mikro/lens"]
    assert await found(search="multi-dimensional") == ["@mikro/arraydataset"]
    assert await found(descriptor="@mikro/n_channels") == ["@mikro/arraydataset", "@mikro/lens"]
    assert await found(package="mikro", described=False) == ["@mikro/folder"]
    assert await found(service="bank") == ["@bank/category"]


async def test_a_descriptor_keeps_its_row_across_provisioning_passes(authenticated_context):
    await sync_to_async(_catalogue)()
    before = await models.Descriptor.objects.aget(key="@mikro/n_channels", structure__identifier="@mikro/lens")
    lens = await models.StructureDeclaration.objects.aget(identifier="@mikro/lens")

    # The next manifest: one descriptor reworded, one dropped, one new.
    await sync_to_async(service_catalog._sync_descriptors)(lens, [service_catalog.DescriptorManifest.model_validate(d) for d in ({**CHANNELS, "description": "Channels"}, {"key": "@mikro/n_timepoints", "type": "INT"})])

    rows = [row async for row in models.Descriptor.objects.filter(structure=lens).order_by("position")]
    assert [(row.key, row.description) for row in rows] == [("@mikro/n_channels", "Channels"), ("@mikro/n_timepoints", None)]
    assert rows[0].pk == before.pk  # what a client holds on to stays valid

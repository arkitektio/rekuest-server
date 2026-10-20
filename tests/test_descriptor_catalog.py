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
    _declare(models.Service.objects.create(name="mikro", description="Microscopy data"), models.Service.objects.create(name="bank"))


def _declare(mikro: models.Service, bank: models.Service) -> None:
    """What mikro and bank declare, as one provisioning pass reads it."""
    service_catalog._sync_structures(
        mikro,
        _structures(
            {"identifier": "@mikro/arraydataset", "label": "Array Dataset", "description": "A multi-dimensional array", "descriptors": [AXES, CHANNELS]},
            {"identifier": "@mikro/lens", "label": "Lens", "descriptors": [AXES, CHANNELS]},
            {"identifier": "@mikro/folder", "label": "Folder", "descriptors": []},
        ),
    )
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


SIGNALS = [
    {"identifier": "@mikro/arraydataset", "kinds": ["CREATED", "DELETED"], "descriptors": ["@mikro/n_channels"], "description": "An array dataset was created or deleted"},
    {"identifier": "@bank/category", "kinds": ["CREATED"], "description": "A spending or income category was set up"},
]


def _catalogue_signals() -> None:
    for name in ("mikro", "bank"):
        service = models.Service.objects.get(name=name)
        service_catalog._sync_signals(service, [service_catalog.SignalManifest.model_validate(signal) for signal in SIGNALS if signal["identifier"].startswith(f"@{name}/")])


async def test_the_catalogue_is_embedded_when_it_is_read(authenticated_context):
    await sync_to_async(_catalogue)()
    await sync_to_async(_catalogue_signals)()

    def embedded(queryset) -> dict[str, bool]:
        return {str(row): row.embedding is not None for row in queryset}

    structures = await sync_to_async(embedded)(models.StructureDeclaration.objects.select_related("service"))
    assert all(structures.values()), structures  # every one has a label
    descriptors = await sync_to_async(embedded)(models.Descriptor.objects.select_related("structure"))
    assert all(descriptors.values()), descriptors
    signals = await sync_to_async(embedded)(models.SignalDeclaration.objects.select_related("service"))
    assert all(signals.values()), signals

    # A declaration with nothing to embed has no vector, never a zero one.
    folder = await models.StructureDeclaration.objects.aget(identifier="@mikro/folder")
    await sync_to_async(service_catalog._sync_descriptors)(folder, [service_catalog.DescriptorManifest.model_validate({"key": "@mikro/tag"})])
    assert (await models.Descriptor.objects.aget(key="@mikro/tag")).embedding is None


async def test_a_row_without_a_vector_is_embedded_on_the_next_pass(authenticated_context):
    # Rows catalogued before the column existed, or while the model could not load, have none.
    await sync_to_async(_catalogue)()
    await sync_to_async(_catalogue_signals)()
    await models.StructureDeclaration.objects.all().aupdate(embedding=None)
    await models.Descriptor.objects.all().aupdate(embedding=None)
    await models.SignalDeclaration.objects.all().aupdate(embedding=None)

    await sync_to_async(_declare)(await models.Service.objects.aget(name="mikro"), await models.Service.objects.aget(name="bank"))
    await sync_to_async(_catalogue_signals)()

    assert not await models.StructureDeclaration.objects.filter(embedding__isnull=True).aexists()
    assert not await models.Descriptor.objects.filter(embedding__isnull=True).aexists()
    assert not await models.SignalDeclaration.objects.filter(embedding__isnull=True).aexists()


async def test_the_catalogue_is_searched_by_meaning(authenticated_context):
    """No word of these queries is in what the service declares; what it says means them."""
    context = (await sync_to_async(tenant_context)(TEST_TOKEN))[0]
    await sync_to_async(_catalogue)()
    await sync_to_async(_catalogue_signals)()

    structures = (await _run("query($search: String!) { hostedStructures(filters: {search: $search}) { identifier } }", context, search="directory"))["hostedStructures"]
    assert structures[0] == {"identifier": "@mikro/folder"}

    descriptors = (await _run("query($search: String!) { descriptors(filters: {search: $search}) { key } }", context, search="money moved between accounts"))["descriptors"]
    assert descriptors[0] == {"key": "@bank/kind"}

    signals = (await _run("query($search: String!) { signalDeclarations(search: $search) { identifier kind } }", context, search="expense classification"))["signalDeclarations"]
    assert signals[0] == {"identifier": "@bank/category", "kind": "CREATED"}


async def test_signal_declarations_are_searched(authenticated_context):
    context = (await sync_to_async(tenant_context)(TEST_TOKEN))[0]
    await sync_to_async(_catalogue)()
    await sync_to_async(_catalogue_signals)()
    query = "query($search: String) { signalDeclarations(search: $search) { identifier kind } }"

    everything = (await _run(query, context))["signalDeclarations"]
    assert [(row["identifier"], row["kind"]) for row in everything] == [("@bank/category", "CREATED"), ("@mikro/arraydataset", "CREATED"), ("@mikro/arraydataset", "DELETED")]

    # A substring of the identifier or the description, ranked before anything found by meaning.
    found = (await _run(query, context, search="deleted"))["signalDeclarations"]
    assert {(row["identifier"], row["kind"]) for row in found[:2]} == {("@mikro/arraydataset", "CREATED"), ("@mikro/arraydataset", "DELETED")}
    assert [(row["identifier"], row["kind"]) for row in (await _run(query, context, search="@bank/"))["signalDeclarations"]][0] == ("@bank/category", "CREATED")

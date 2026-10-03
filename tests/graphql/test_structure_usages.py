"""GraphQL reverse lookup: which actions use a structure, derived from the port rows.

The four usage tables are gone — ``Structure.inputUsages``/``outputUsages`` (and the interface
variants) now resolve ``PortUsage`` objects straight from the relational ArgPort/ReturnPort rows
(indexed ``identifier``), reconstructing container ``modifiers`` from the materialized key_path.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from authentikate.models import App, Release
from rekuest_core.inputs.models import ImplementationInputModel

from facade import models
from tests.registered import create_implementation as _create_implementation
from facade.schema import schema

USAGES_QUERY = """
    query StructureUsages {
        structures {
            identifier
            inputUsages { portKey index keyPath modifiers action { name } }
            outputUsages { portKey index keyPath modifiers action { name } }
        }
    }
"""


def _masker_input():
    return ImplementationInputModel.model_validate(
        {
            "interface": "masker",
            "definition": {
                "key": "masker",
                "version": "1",
                "name": "Masker",
                "kind": "FUNCTION",
                "args": [{"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": False}],
                "returns": [
                    {
                        "key": "masks",
                        "kind": "LIST",
                        "nullable": False,
                        "children": [{"key": "mask", "kind": "STRUCTURE", "identifier": "@mikro/mask"}],
                    }
                ],
            },
        }
    )


def _seed_masker(context):
    request = context.request
    app = App.objects.create(identifier="usages-app")
    release = Release.objects.create(app=app, version="1.0.0")
    agent = models.Agent.objects.create(app=app, release=release, user=request.user, client=request.client, organization=request.organization, hash="usages-agent-hash")
    return _create_implementation(_masker_input(), agent).action


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_structure_usages_resolved_from_port_rows(authenticated_context: HttpContext):
    # Warm-up execute so the seed lands in the org the executed query scopes to.
    await schema.execute("query { __typename }", context_value=authenticated_context)
    await sync_to_async(_seed_masker)(authenticated_context)

    result = await schema.execute(USAGES_QUERY, context_value=authenticated_context)

    assert result.errors is None, result.errors
    by_identifier = {s["identifier"]: s for s in result.data["structures"]}

    mask = by_identifier["@mikro/mask"]
    assert mask["inputUsages"] == []
    assert len(mask["outputUsages"]) == 1
    usage = mask["outputUsages"][0]
    assert usage["action"]["name"] == "Masker"
    assert usage["portKey"] == "masks"
    assert usage["keyPath"] == "masks.mask"
    assert usage["modifiers"] == ["list"]

    image = by_identifier["@mikro/image"]
    assert image["outputUsages"] == []
    assert len(image["inputUsages"]) == 1
    assert image["inputUsages"][0]["modifiers"] == []
    assert image["inputUsages"][0]["portKey"] == "image"


HOSTED_QUERY = """
    query Hosted($identifier: ID!) {
        structure(identifier: $identifier) {
            identifier label description
            service { name }
            descriptors { key type description }
            signals { kind descriptorKeys }
            package { key service { name } }
            inputUsages { portKey }
        }
        structures { identifier service { name } }
        services { name description structures { identifier } signals { identifier kind } }
    }
"""


def _seed_hosted():
    """A service hosting a structure that no action port references — and no agent anywhere."""
    service = models.Service.objects.create(name="lab", description="The bench.")
    models.StructureDeclaration.objects.create(
        service=service, identifier="@lab/sample", label="Sample", description="A sample on the bench.", descriptors=[{"key": "@lab/volume", "type": "FLOAT", "description": "Millilitres"}]
    )
    models.SignalDeclaration.objects.create(service=service, identifier="@lab/sample", kind="CREATED", descriptor_keys=["@lab/volume"])


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_hosted_structure_is_listed_though_no_port_references_it(authenticated_context: HttpContext) -> None:
    await sync_to_async(_seed_hosted)()

    result = await schema.execute(HOSTED_QUERY, variable_values={"identifier": "@lab/sample"}, context_value=authenticated_context)
    assert result.errors is None, result.errors

    assert result.data["structure"] == {
        "identifier": "@lab/sample",
        "label": "Sample",
        "description": "A sample on the bench.",
        "service": {"name": "lab"},
        "descriptors": [{"key": "@lab/volume", "type": "FLOAT", "description": "Millilitres"}],
        "signals": [{"kind": "CREATED", "descriptorKeys": ["@lab/volume"]}],
        "package": {"key": "lab", "service": {"name": "lab"}},
        "inputUsages": [],
    }
    assert {"identifier": "@lab/sample", "service": {"name": "lab"}} in result.data["structures"]
    # The service is a thing of its own: it hosts and emits, and that is all it does.
    assert result.data["services"] == [
        {"name": "lab", "description": "The bench.", "structures": [{"identifier": "@lab/sample"}], "signals": [{"identifier": "@lab/sample", "kind": "CREATED"}]}
    ]

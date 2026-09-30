"""GraphQL ``actions`` filtering by concrete runtime objects (the descriptor match path).

Exercises the full filter pipeline end-to-end: the ``ActionFilter.object_demands`` field
(``facade/filters/action.py``) -> ``managers.get_action_ids_by_demands`` -> the compiled
``requires`` JSONPath on the relational ArgPort rows, evaluated against the runtime ``descriptors``
of a candidate object. This is the "pass a runtime object with descriptors and find actions that
accept it" path. ``PortMatchInput`` stays purely structural; the descriptors live on the separate
the optional ``descriptors`` on ``PortMatchInput``.
"""

import pytest
from asgiref.sync import sync_to_async
from kante.context import HttpContext

from authentikate.models import App, Release
from rekuest_core.inputs.models import ImplementationInputModel

from facade import models
from tests.registered import create_implementation as _create_implementation
from facade.schema import schema

FILTER_QUERY = """
    query FilterActions($demands: [PortDemandInput!]) {
        actions(filters: { objectDemands: $demands }) {
            id
            name
        }
    }
"""


def _implementation_input():
    return ImplementationInputModel.model_validate(
        {
            "interface": "thresholder",
            "definition": {
                "key": "thresholder",
                "version": "1",
                "name": "Thresholder",
                "kind": "FUNCTION",
                "args": [
                    {
                        "key": "image",
                        "kind": "STRUCTURE",
                        "identifier": "@mikro/image",
                        "nullable": False,
                        "requires": [{"key": "axes", "operator": "EQUALS", "value": "c"}],
                    }
                ],
                "returns": [],
            },
        }
    )


def _seed_action_requiring_axes_c(context):
    """Register (via the real path) an Action in the authenticated org whose ``image`` arg
    REQUIRES ``axes == "c"``.

    The action is seeded into the *exact* org/user/client the request carries (not re-looked-up by
    slug), so it matches the org-prescope the ``actions`` query applies. Going through
    ``_create_implementation`` also creates the Implementation/Agent, so the action is fully visible.
    """
    request = context.request
    org = request.organization
    app = App.objects.create(identifier="flt-app")
    release = Release.objects.create(app=app, version="1.0.0")
    agent = models.Agent.objects.create(app=app, release=release, user=request.user, client=request.client, organization=org, hash="flt-agent-hash")
    return _create_implementation(_implementation_input(), agent).action


async def _seed_in_execution_org(context):
    """Seed the action into the org the *executed* query will scope to.

    The kante auth extension resolves and sets ``request.organization`` from the token during
    ``schema.execute`` (overwriting the fixture's default). We run one warm-up execute first so the
    seed lands in that same org and the org-prescoped ``actions`` query can see it.
    """
    await schema.execute("query { __typename }", context_value=context)
    return await sync_to_async(_seed_action_requiring_axes_c)(context)


def _demands(descriptor_values):
    return [
        {
            "kind": "ARGS",
            "matches": [
                {
                    "identifier": "@mikro/image",
                    "descriptors": [{"key": k, "value": v} for k, v in descriptor_values.items()],
                }
            ],
        }
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestActionObjectFilter:
    """Filtering the action catalog by a candidate object's runtime descriptors."""

    async def test_filter_matches_when_descriptors_satisfied(self, authenticated_context: HttpContext):
        await _seed_in_execution_org(authenticated_context)

        result = await schema.execute(
            FILTER_QUERY,
            context_value=authenticated_context,
            variable_values={"demands": _demands({"axes": "c"})},
        )

        assert result.errors is None, result.errors
        names = [a["name"] for a in result.data["actions"]]
        assert "Thresholder" in names

    async def test_filter_excludes_when_descriptors_violated(self, authenticated_context: HttpContext):
        await _seed_in_execution_org(authenticated_context)

        result = await schema.execute(
            FILTER_QUERY,
            context_value=authenticated_context,
            variable_values={"demands": _demands({"axes": "z"})},
        )

        assert result.errors is None, result.errors
        names = [a["name"] for a in result.data["actions"]]
        assert "Thresholder" not in names

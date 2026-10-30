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

from authentikate.models import App, Client, Release
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
    id), so it matches the org-prescope the ``actions`` query applies. Going through
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


ESTIMATE_QUERY = """
    query Estimated($filters: ActionFilter, $ordering: [ActionOrder!]) {
        actions(filters: $filters, ordering: $ordering) {
            name
            estimatedRuntimeSeconds
        }
    }
"""


def _seed_estimates(context: HttpContext) -> None:
    """Three actions on one structure: fast, slow, and one with no estimate."""
    from django.utils import timezone

    organization = context.request.organization
    release = Release.objects.create(app=App.objects.create(identifier="estimate-app"), version="1.0.0")
    for name, seconds in (("slow", 1000), ("fast", 10), ("none", None)):
        payload = _implementation_input().model_dump()
        payload["interface"] = name
        payload["definition"].update(key=name, name=name)
        payload["definition"]["args"][0].pop("requires", None)
        # An agent per client: one client, user and organization has one agent.
        client = Client.objects.create(client_id=f"estimate-{name}", release=release)
        agent = models.Agent.objects.create(app=release.app, release=release, user=context.request.user, client=client, organization=organization, hash=name, name=name, connected=True, last_seen=timezone.now(), hardware={"cpu_count": 1})
        implementation = _create_implementation(ImplementationInputModel.model_validate(payload), agent)
        if seconds is not None:
            models.Implementation.objects.filter(pk=implementation.pk).update(estimated_runtime_path=f"({seconds} / $agent.cpu_count)", estimated_runtime_port="image")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_actions_filter_and_sort_by_their_estimate(authenticated_context: HttpContext):
    await schema.execute("query { __typename }", context_value=authenticated_context)
    await sync_to_async(_seed_estimates)(authenticated_context)

    def object_filter(**extra):
        return {"estimatedRuntime": {"identifier": "@mikro/image", "descriptors": [{"key": "axes", "value": "c"}], **extra}}

    ordered = await schema.execute(ESTIMATE_QUERY, variable_values={"filters": object_filter(), "ordering": [{"estimatedRuntime": "ASC_NULLS_LAST"}]}, context_value=authenticated_context)
    assert ordered.errors is None, ordered.errors
    assert [(action["name"], action["estimatedRuntimeSeconds"]) for action in ordered.data["actions"]] == [("fast", 10.0), ("slow", 1000.0), ("none", None)]

    limited = await schema.execute(ESTIMATE_QUERY, variable_values={"filters": object_filter(lte=60)}, context_value=authenticated_context)
    assert limited.errors is None, limited.errors
    assert {action["name"] for action in limited.data["actions"]} == {"fast", "none"}

    # Without the filter there is nothing to order by.
    plain = await schema.execute(ESTIMATE_QUERY, variable_values={"ordering": [{"estimatedRuntime": "ASC_NULLS_LAST"}]}, context_value=authenticated_context)
    assert plain.errors is None, plain.errors
    assert {action["estimatedRuntimeSeconds"] for action in plain.data["actions"]} == {None}

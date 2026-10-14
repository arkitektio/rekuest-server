"""``Action.assignable`` / ``liveImplementations`` through GraphQL: the fields, the filter, and the ordering that keeps a
search's ranking as its tie-break (``facade/filters/action.py`` ``assignable_first``)."""

import numpy as np
import pytest
from asgiref.sync import sync_to_async
from django.db import connection
from kante.context import HttpContext

from authentikate.models import App, Release
from embeddings import engine
from facade import models
from facade.schema import schema
from tests.graphql.test_action_search import QUERY, _seed, _warm

ACTIONS = """
    query Actions($search: String, $assignable: Boolean, $ordering: [ActionOrder!]! = []) {
        actions(filters: { search: $search, assignable: $assignable }, ordering: $ordering) {
            name
            assignable
            liveImplementations
        }
    }
"""


def _runnable(context: HttpContext, *actions: models.Action) -> None:
    """A webhook agent (always available) in the request's org implementing ``actions``."""
    request = context.request
    app = App.objects.create(identifier="assignable-webhook-app")
    agent = models.Agent.objects.create(
        app=app,
        release=Release.objects.create(app=app, version="1.0.0"),
        user=request.user,
        client=request.client,
        organization=request.organization,
        hash="assignable-webhook-agent",
        kind="WEBHOOK",
    )
    for index, action in enumerate(actions):
        models.Implementation.objects.create(agent=agent, action=action, interface=f"run-{index}")
    # takt's drain, which runs on the commit's notice on a hub.
    with connection.cursor() as cursor:
        cursor.execute("SELECT facade_refresh_live_implementations(500)")


async def _actions(context: HttpContext, **variables) -> list[tuple[str, bool]]:
    result = await schema.execute(ACTIONS, context_value=context, variable_values=variables)
    assert result.errors is None, result.errors
    rows = result.data["actions"]
    assert all(a["assignable"] == (a["liveImplementations"] > 0) for a in rows)
    return [(a["name"], a["assignable"]) for a in rows]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestActionAssignable:
    async def test_runnable_first_keeps_the_search_ranking_in_each_group(self, authenticated_context: HttpContext) -> None:
        await _warm(authenticated_context)
        e0 = np.asarray(engine.embed_query(QUERY))
        near = await sync_to_async(_seed)(authenticated_context, "as-near", "Near", distance=0.05, e0=e0)
        mid = await sync_to_async(_seed)(authenticated_context, "as-mid", "Mid", distance=0.2, e0=e0)
        far = await sync_to_async(_seed)(authenticated_context, "as-far", "Far", distance=0.3, e0=e0)
        await sync_to_async(_seed)(authenticated_context, "as-farther", "Farther", distance=0.4, e0=e0)
        await sync_to_async(_runnable)(authenticated_context, far, mid)
        del near

        assert await _actions(authenticated_context, search=QUERY) == [
            ("Near", False),
            ("Mid", True),
            ("Far", True),
            ("Farther", False),
        ]
        assert await _actions(authenticated_context, search=QUERY, ordering=[{"assignable": "DESC"}]) == [
            ("Mid", True),
            ("Far", True),
            ("Near", False),
            ("Farther", False),
        ]

    async def test_filters_to_what_can_run(self, authenticated_context: HttpContext) -> None:
        await _warm(authenticated_context)
        runnable = await sync_to_async(_seed)(authenticated_context, "as-on", "Runs")
        await sync_to_async(_seed)(authenticated_context, "as-off", "Waits")
        await sync_to_async(_runnable)(authenticated_context, runnable)

        assert await _actions(authenticated_context, assignable=True) == [("Runs", True)]
        assert await _actions(authenticated_context, assignable=False) == [("Waits", False)]

    async def test_without_a_search_the_tie_break_is_stable(self, authenticated_context: HttpContext) -> None:
        await _warm(authenticated_context)
        first = await sync_to_async(_seed)(authenticated_context, "as-1", "One")
        await sync_to_async(_seed)(authenticated_context, "as-2", "Two")
        third = await sync_to_async(_seed)(authenticated_context, "as-3", "Three")
        await sync_to_async(_runnable)(authenticated_context, third, first)

        names = [name for name, _ in await _actions(authenticated_context, ordering=[{"assignable": "DESC"}])]
        assert names == ["One", "Three", "Two"]

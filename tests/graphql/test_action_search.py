"""The hybrid ``actions(filters: { search })``: substring OR semantic, ranked.

Real model, real Postgres with pgvector, the real GraphQL pipeline (org prescope, filters,
ordering, pagination). Where a test needs rows at *known* distances from the query it writes
the vectors directly: ``e0`` is the real embedding of the query, and ``_vec(d)`` is a unit
vector at cosine distance ``d`` from it. That is real data in the real column, not a stub.
"""

import math

import numpy as np
import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings
from kante.context import HttpContext

from embeddings import engine
from facade.models import Action
from facade.schema import schema
from tests.factories import create_action_for_organization

QUERY = "detect cells"

SEARCH = """
    query Search($search: String!, $ordering: [ActionOrder!]! = []) {
        actions(filters: { search: $search }, ordering: $ordering) {
            name
        }
    }
"""


def _unit_orthogonal(e0: np.ndarray) -> np.ndarray:
    axis = np.zeros_like(e0)
    axis[int(np.argmin(np.abs(e0)))] = 1.0
    u = axis - float(np.dot(axis, e0)) * e0
    return u / np.linalg.norm(u)


def _vec(e0: np.ndarray, distance: float) -> list[float]:
    """A unit vector at cosine distance ``distance`` from ``e0``."""
    theta = math.acos(1.0 - distance)
    return (math.cos(theta) * e0 + math.sin(theta) * _unit_orthogonal(e0)).astype(float).tolist()


async def _warm(context: HttpContext) -> None:
    # The auth extension sets ``request.organization`` from the token during execute; seed
    # after one warm-up so the rows land in the org the scoped query reads.
    await schema.execute("query { __typename }", context_value=context)


def _seed(context: HttpContext, prefix: str, name: str, description: str = "", *, distance: float | None = None, e0: np.ndarray | None = None, embedding_model: str | None = None) -> Action:
    """An action in the request's org; optionally with a vector pinned at ``distance`` from ``e0``."""
    action = create_action_for_organization(context.request.organization, prefix, name=name, description=description or f"{prefix} description")
    if distance is not None:
        Action.objects.filter(pk=action.pk).update(embedding=_vec(e0, distance), embedding_model=embedding_model or engine.model_id())
    elif embedding_model is not None:
        Action.objects.filter(pk=action.pk).update(embedding_model=embedding_model)
    return action


async def _names(context: HttpContext, search: str, ordering: list | None = None) -> list[str]:
    result = await schema.execute(SEARCH, context_value=context, variable_values={"search": search, "ordering": ordering or []})
    assert result.errors is None, result.errors
    return [a["name"] for a in result.data["actions"]]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestActionSearch:
    """The hybrid search filter, end to end."""

    async def test_semantic_match_without_substring(self, authenticated_context: HttpContext) -> None:
        """A description that means the query is found although no word of the query is in the name."""
        await _warm(authenticated_context)
        await sync_to_async(_seed)(authenticated_context, "sem-a", "Segment nuclei", "Find cell nuclei in a fluorescence image and detect every cell")
        await sync_to_async(_seed)(authenticated_context, "sem-b", "Export spreadsheet", "Write a table to an xlsx file on disk")

        names = await _names(authenticated_context, QUERY)

        assert "Segment nuclei" in names
        assert "Export spreadsheet" not in names

    async def test_substring_still_matches_when_disabled(self, authenticated_context: HttpContext) -> None:
        """With embeddings off the filter is exactly the old substring match."""
        await _warm(authenticated_context)
        await sync_to_async(_seed)(authenticated_context, "off-a", "Detect Cells", "")
        await sync_to_async(_seed)(authenticated_context, "off-b", "Segment nuclei", "detect cells in an image")

        with override_settings(EMBEDDINGS={**engine._settings(), "ENABLED": False}):
            names = await _names(authenticated_context, QUERY)

        assert names == ["Detect Cells"]

    async def test_orders_closest_first(self, authenticated_context: HttpContext) -> None:
        """Semantic-only hits come back nearest first."""
        await _warm(authenticated_context)
        e0 = np.asarray(engine.embed_query(QUERY))
        await sync_to_async(_seed)(authenticated_context, "ord-far", "Far", distance=0.5, e0=e0)
        await sync_to_async(_seed)(authenticated_context, "ord-near", "Near", distance=0.1, e0=e0)
        await sync_to_async(_seed)(authenticated_context, "ord-mid", "Mid", distance=0.3, e0=e0)

        assert await _names(authenticated_context, QUERY) == ["Near", "Mid", "Far"]

    async def test_substring_ranks_before_semantic(self, authenticated_context: HttpContext) -> None:
        """A substring hit precedes a semantic-only hit, even one far closer."""
        await _warm(authenticated_context)
        e0 = np.asarray(engine.embed_query(QUERY))
        await sync_to_async(_seed)(authenticated_context, "rank-near", "Near", distance=0.05, e0=e0)
        sub = await sync_to_async(_seed)(authenticated_context, "rank-sub", "Detect cells (substring)", distance=0.5, e0=e0)
        # A substring hit with no vector at all still ranks first.
        await sync_to_async(Action.objects.filter(pk=sub.pk).update)(embedding=None)

        assert await _names(authenticated_context, QUERY) == ["Detect cells (substring)", "Near"]

    async def test_threshold_excludes_far_rows(self, authenticated_context: HttpContext) -> None:
        """A vector beyond the threshold is not a hit."""
        await _warm(authenticated_context)
        e0 = np.asarray(engine.embed_query(QUERY))
        await sync_to_async(_seed)(authenticated_context, "thr-in", "In", distance=0.4, e0=e0)
        await sync_to_async(_seed)(authenticated_context, "thr-out", "Out", distance=0.7, e0=e0)

        assert await _names(authenticated_context, QUERY) == ["In"]

    async def test_stale_embedding_model_is_not_a_vector_hit(self, authenticated_context: HttpContext) -> None:
        """A row embedded by another model is skipped by the vector leg, still found by substring."""
        await _warm(authenticated_context)
        e0 = np.asarray(engine.embed_query(QUERY))
        await sync_to_async(_seed)(authenticated_context, "stale-a", "Old model near", distance=0.05, e0=e0, embedding_model="some/older-model")
        await sync_to_async(_seed)(authenticated_context, "stale-b", "Old model detect cells", distance=0.05, e0=e0, embedding_model="some/older-model")

        assert await _names(authenticated_context, QUERY) == ["Old model detect cells"]

    async def test_explicit_ordering_replaces_the_ranking(self, authenticated_context: HttpContext) -> None:
        """A client's ``ordering`` wins over the distance ranking."""
        await _warm(authenticated_context)
        e0 = np.asarray(engine.embed_query(QUERY))
        await sync_to_async(_seed)(authenticated_context, "exp-first", "First defined", distance=0.4, e0=e0)
        await sync_to_async(_seed)(authenticated_context, "exp-second", "Second defined", distance=0.1, e0=e0)

        assert await _names(authenticated_context, QUERY) == ["Second defined", "First defined"]
        assert await _names(authenticated_context, QUERY, ordering=[{"definedAt": "ASC"}]) == ["First defined", "Second defined"]

    async def test_blank_search_is_lexical(self, authenticated_context: HttpContext) -> None:
        """A blank query keeps today's behaviour: ``icontains ''`` matches every action."""
        await _warm(authenticated_context)
        await sync_to_async(_seed)(authenticated_context, "blank-a", "Anything")

        assert await _names(authenticated_context, "") == ["Anything"]

    async def test_unloadable_model_degrades_to_substring(self, authenticated_context: HttpContext) -> None:
        """When the weights cannot be loaded the query still answers, substring-only."""
        await _warm(authenticated_context)
        e0 = np.asarray(engine.embed_query(QUERY))
        await sync_to_async(_seed)(authenticated_context, "deg-near", "Near", distance=0.05, e0=e0)
        await sync_to_async(_seed)(authenticated_context, "deg-sub", "Detect cells")

        try:
            with override_settings(EMBEDDINGS={**engine._settings(), "MODEL_PATH": "/nonexistent/embeddings"}):
                engine.reset()
                assert await _names(authenticated_context, QUERY) == ["Detect cells"]
        finally:
            engine.reset()

    async def test_nested_action_search_is_substring_only(self, authenticated_context: HttpContext) -> None:
        """``implementations(filters: { action: { search } })`` keeps its substring semantics."""
        await _warm(authenticated_context)
        result = await schema.execute(
            'query { implementations(filters: { action: { search: "detect" } }) { id } }',
            context_value=authenticated_context,
        )
        assert result.errors is None, result.errors
        assert result.data["implementations"] == []

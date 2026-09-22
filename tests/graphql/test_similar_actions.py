"""``similarActions``: the org's other actions ranked by cosine distance to one action's vector.

Real model, real Postgres with pgvector. The origin's vector is whatever the model gave its
text; the candidates are pinned at known distances from it so the ranking is exact.
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

ROOT = """
    query Similar($action: ID!, $filters: ActionFilter, $limit: Int! = 10, $maxDistance: Float) {
        similarActions(action: $action, filters: $filters, limit: $limit, maxDistance: $maxDistance) { name }
    }
"""
FIELD = """
    query Similar($action: ID!, $limit: Int! = 10) {
        action(id: $action) { similarActions(limit: $limit) { name } }
    }
"""


def _unit_orthogonal(e0: np.ndarray) -> np.ndarray:
    axis = np.zeros_like(e0)
    axis[int(np.argmin(np.abs(e0)))] = 1.0
    u = axis - float(np.dot(axis, e0)) * e0
    return u / np.linalg.norm(u)


def _vec(e0: np.ndarray, distance: float) -> list[float]:
    theta = math.acos(1.0 - distance)
    return (math.cos(theta) * e0 + math.sin(theta) * _unit_orthogonal(e0)).astype(float).tolist()


async def _warm(context: HttpContext) -> None:
    await schema.execute("query { __typename }", context_value=context)


def _seed(context: HttpContext, prefix: str, name: str, *, e0: np.ndarray | None = None, distance: float | None = None, embedding_model: str | None = None, **overrides) -> Action:
    action = create_action_for_organization(context.request.organization, prefix, name=name, description=f"{prefix} description", **overrides)
    if e0 is not None:
        Action.objects.filter(pk=action.pk).update(embedding=_vec(e0, distance) if distance is not None else None, embedding_model=embedding_model or engine.model_id())
    return action


async def _origin_and_e0(context: HttpContext) -> tuple[Action, np.ndarray]:
    origin = await sync_to_async(_seed)(context, "origin", "Segment nuclei")
    await origin.arefresh_from_db()
    return origin, np.asarray(origin.embedding)


async def _names(context: HttpContext, query: str, **variables) -> list[str]:
    result = await schema.execute(query, context_value=context, variable_values=variables)
    assert result.errors is None, result.errors
    data = result.data["similarActions"] if "similarActions" in result.data else result.data["action"]["similarActions"]
    return [a["name"] for a in data]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestSimilarActions:
    """The root query and the type field, end to end."""

    async def test_nearest_first_and_origin_excluded(self, authenticated_context: HttpContext) -> None:
        await _warm(authenticated_context)
        origin, e0 = await _origin_and_e0(authenticated_context)
        await sync_to_async(_seed)(authenticated_context, "far", "Far", e0=e0, distance=0.6)
        await sync_to_async(_seed)(authenticated_context, "near", "Near", e0=e0, distance=0.1)
        await sync_to_async(_seed)(authenticated_context, "mid", "Mid", e0=e0, distance=0.3)

        assert await _names(authenticated_context, ROOT, action=str(origin.pk)) == ["Near", "Mid", "Far"]
        assert await _names(authenticated_context, FIELD, action=str(origin.pk)) == ["Near", "Mid", "Far"]

    async def test_limit_and_max_distance(self, authenticated_context: HttpContext) -> None:
        await _warm(authenticated_context)
        origin, e0 = await _origin_and_e0(authenticated_context)
        for name, d in (("Far", 0.6), ("Near", 0.1), ("Mid", 0.3)):
            await sync_to_async(_seed)(authenticated_context, name.lower(), name, e0=e0, distance=d)

        assert await _names(authenticated_context, ROOT, action=str(origin.pk), limit=2) == ["Near", "Mid"]
        assert await _names(authenticated_context, ROOT, action=str(origin.pk), maxDistance=0.5) == ["Near", "Mid"]

    async def test_filters_narrow_the_candidates(self, authenticated_context: HttpContext) -> None:
        await _warm(authenticated_context)
        origin, e0 = await _origin_and_e0(authenticated_context)
        await sync_to_async(_seed)(authenticated_context, "near", "Near", e0=e0, distance=0.1, is_dev=True)
        mid = await sync_to_async(_seed)(authenticated_context, "mid", "Mid", e0=e0, distance=0.3)

        assert await _names(authenticated_context, ROOT, action=str(origin.pk), filters={"ids": [str(mid.pk)]}) == ["Mid"]
        assert await _names(authenticated_context, ROOT, action=str(origin.pk), filters={"search": "mid"}) == ["Mid"]

    async def test_unembedded_and_stale_rows_are_skipped(self, authenticated_context: HttpContext) -> None:
        await _warm(authenticated_context)
        origin, e0 = await _origin_and_e0(authenticated_context)
        await sync_to_async(_seed)(authenticated_context, "near", "Near", e0=e0, distance=0.1)
        await sync_to_async(_seed)(authenticated_context, "stale", "Stale", e0=e0, distance=0.05, embedding_model="some/older-model")
        blank = await sync_to_async(_seed)(authenticated_context, "blank", "Blank", e0=e0, distance=0.05)
        await sync_to_async(Action.objects.filter(pk=blank.pk).update)(embedding=None)

        assert await _names(authenticated_context, ROOT, action=str(origin.pk)) == ["Near"]

    async def test_origin_without_vector_or_embeddings_off_is_empty(self, authenticated_context: HttpContext) -> None:
        await _warm(authenticated_context)
        origin, e0 = await _origin_and_e0(authenticated_context)
        await sync_to_async(_seed)(authenticated_context, "near", "Near", e0=e0, distance=0.1)

        with override_settings(EMBEDDINGS={**engine._settings(), "ENABLED": False}):
            assert await _names(authenticated_context, ROOT, action=str(origin.pk)) == []
        await sync_to_async(Action.objects.filter(pk=origin.pk).update)(embedding=None)
        assert await _names(authenticated_context, ROOT, action=str(origin.pk)) == []

    async def test_other_org_action_is_not_found(self, authenticated_context: HttpContext) -> None:
        """A foreign id is refused (scoped get), never ranked against this org's actions."""
        from authentikate.models import Organization

        await _warm(authenticated_context)
        other = await Organization.objects.acreate(slug="similar-other-org")
        foreign = await sync_to_async(create_action_for_organization)(other, "foreign", name="Foreign")

        result = await schema.execute(ROOT, context_value=authenticated_context, variable_values={"action": str(foreign.pk)})
        assert result.errors and "No Action" in str(result.errors[0])


EMBEDDING = """
    query Embedding($action: ID!) {
        action(id: $action) { name embedding }
    }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_action_publishes_its_vector_with_the_model_id(authenticated_context: HttpContext) -> None:
    """A vector without the model that produced it is not comparable to anything, so the
    descriptor travels inside the value rather than beside it."""
    from embeddings.strawberry import format_embedding

    action = await sync_to_async(create_action_for_organization)(authenticated_context.request.organization, "emb-field", name="Segment nuclei", description="Find cell nuclei in a fluorescence image")
    await action.arefresh_from_db()

    result = await schema.execute(EMBEDDING, context_value=authenticated_context, variable_values={"action": str(action.id)})
    assert not result.errors, result.errors
    published = result.data["action"]["embedding"]

    model_id, _, floats = published.partition(":")
    assert model_id == engine.model_id()
    # The floats round-trip exactly, so a client can reuse the vector it was handed.
    assert [float(component) for component in floats.split(",")] == action.embedding
    assert published == format_embedding(action.embedding, engine.model_id())


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_unindexed_action_publishes_null(authenticated_context: HttpContext) -> None:
    action = await sync_to_async(create_action_for_organization)(authenticated_context.request.organization, "emb-null", name="Nameless")
    await Action.objects.filter(pk=action.pk).aupdate(embedding=None, embedding_model="")

    result = await schema.execute(EMBEDDING, context_value=authenticated_context, variable_values={"action": str(action.id)})
    assert not result.errors, result.errors

    assert result.data["action"]["embedding"] is None

"""Actions embed their name + description on save.

Real model (potion-base-8M), real Postgres with pgvector -- what the service runs.
"""

import pytest
from django.test import override_settings

from embeddings import engine
from facade.models import Action
from tests.factories import create_action_for_organization, create_registry_bundle


def _action(prefix: str, **overrides) -> Action:
    _, _, org, _ = create_registry_bundle(prefix)
    return create_action_for_organization(org, prefix, **overrides)


@pytest.mark.django_db(transaction=True)
def test_create_embeds_with_the_release_model() -> None:
    """A new action gets a unit vector of the release's width."""
    action = _action("emb-create", name="Segment nuclei", description="Find cell nuclei in a fluorescence image")
    action.refresh_from_db()

    assert action.embedding is not None
    assert len(action.embedding) == engine.dimensions() == 256
    assert abs(sum(x * x for x in action.embedding) - 1.0) < 1e-4


@pytest.mark.django_db(transaction=True)
def test_editing_the_description_reembeds() -> None:
    """A changed description changes the vector; an unrelated ``update_fields`` save does not."""
    action = _action("emb-edit", name="Export", description="Write a table to a spreadsheet")
    before = list(Action.objects.get(pk=action.pk).embedding)

    action = Action.objects.get(pk=action.pk)
    action.description = "Detect mitochondria in electron micrographs"
    action.save()
    after = list(Action.objects.get(pk=action.pk).embedding)
    assert after != before

    action = Action.objects.get(pk=action.pk)
    action.scope = "LOCAL"
    action.save(update_fields=["scope"])
    assert list(Action.objects.get(pk=action.pk).embedding) == after


@pytest.mark.django_db(transaction=True)
def test_update_fields_touching_the_source_reembeds() -> None:
    """``save(update_fields=["name"])`` embeds too, and the vector column rides along."""
    action = _action("emb-uf", name="Alpha", description="")
    before = list(Action.objects.get(pk=action.pk).embedding)

    action = Action.objects.get(pk=action.pk)
    action.name = "Count mitochondria per cell"
    action.save(update_fields=["name"])

    assert list(Action.objects.get(pk=action.pk).embedding) != before


@pytest.mark.django_db(transaction=True)
def test_blank_text_stores_null() -> None:
    """No text, no vector."""
    action = _action("emb-blank", name="", description="   ")
    action.refresh_from_db()

    assert action.embedding is None


@pytest.mark.django_db(transaction=True)
def test_an_unloadable_model_saves_the_row_without_a_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    """The write does not fail because the model did; the next save of the row embeds it."""

    def unloadable() -> None:
        raise OSError("no weights")

    monkeypatch.setattr(engine, "_load_model_cached", unloadable)
    action = _action("emb-unloadable", name="Blur", description="Gaussian blur of an image")
    action.refresh_from_db()
    assert action.embedding is None

    monkeypatch.undo()
    action.save()
    action.refresh_from_db()
    assert action.embedding is not None


@pytest.mark.django_db(transaction=True)
def test_disabled_writes_no_vector() -> None:
    """With embeddings off, saving leaves the column untouched."""
    with override_settings(EMBEDDINGS={**engine._settings(), "ENABLED": False}):
        action = _action("emb-off", name="Off", description="Nothing embeds")
        action.refresh_from_db()
        assert action.embedding is None

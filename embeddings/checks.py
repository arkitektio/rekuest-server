"""System check: the vector column must be as wide as the release's model.

Import this module from the service's ``AppConfig.ready()`` to register it. The check is
tagged ``database`` so ``manage.py migrate`` runs it, which makes a mismatch fail the migration
job rather than the first search. It reads the database only: no check loads the model (the
server does, at start), so a management command never pays for it.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.core.checks import Error, Tags, register
from django.db import connections

from embeddings import engine
from embeddings.models import EmbeddedDescriptionMixin


def embedded_models() -> list[type[EmbeddedDescriptionMixin]]:
    """Every concrete installed model that mixes in :class:`EmbeddedDescriptionMixin`."""
    return [model for model in apps.get_models() if issubclass(model, EmbeddedDescriptionMixin)]


@register(Tags.database)
def check_column_dimensions(app_configs: Any, databases: Any = None, **kwargs: Any) -> list[Error]:
    """``embeddings.E002``: a ``vector(N)`` column whose N is not ``engine.DIMENSIONS``.

    Skipped for columns that do not exist yet (the migration adding them has not run).
    """
    if not databases:
        return []
    want = engine.dimensions()
    errors: list[Error] = []
    for alias in databases:
        connection = connections[alias]
        if connection.vendor != "postgresql":
            continue
        for model in embedded_models():
            have = engine.column_dimensions(connection, model._meta.db_table, "embedding")
            if have is not None and have != want:
                errors.append(
                    Error(
                        f"{model._meta.label}.embedding is vector({have}) in the database but this release embeds {want}-wide vectors. A release that changes the model's width carries the migration that alters the column and re-embeds.",
                        id="embeddings.E002",
                    )
                )
    return errors

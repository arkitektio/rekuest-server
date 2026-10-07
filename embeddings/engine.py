"""The embedding model: which one, loading it, text -> vector.

The only module that imports ``model2vec``. The model and its width are constants of the
release: a new embedder ships with a new image, whose migration re-embeds. What stays
configuration (``settings.EMBEDDINGS``: on/off and the distance threshold) is read at call
time, never at import, so ``override_settings`` works in tests.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import numpy as np
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from model2vec import StaticModel

logger = logging.getLogger(__name__)

#: The model2vec model every vector of this release comes from.
MODEL = "minishlab/potion-base-8M"
#: The width of :data:`MODEL`'s vectors, and of every ``vector(N)`` column.
DIMENSIONS = 256
#: Where the image carries the weights of :data:`MODEL` (``save_pretrained`` layout, baked by
#: the Dockerfile). Absent on a developer's machine and in CI, where the weights come from the
#: Hugging Face cache.
MODEL_PATH = "/opt/models/embeddings"
#: Written next to baked weights by the Dockerfile so a process can tell which model the
#: directory holds, and refuse an image baked with another one.
MODEL_ID_FILENAME = "MODEL_ID"

_load_lock = threading.Lock()


class EmbeddingsUnavailable(RuntimeError):
    """The model cannot be loaded (missing weights, no network, corrupt files).

    Raised from :func:`embed_texts` / :func:`embed_query` so a caller that can degrade (the
    ``search`` filter) does so explicitly, and one that cannot (the row's ``save()``) can log
    and store the row without a vector.
    """


def _settings() -> dict[str, Any]:
    return getattr(settings, "EMBEDDINGS", {})


def enabled() -> bool:
    """Whether rows embed themselves and ``search`` has a semantic leg."""
    return bool(_settings().get("ENABLED", False))


def model_id() -> str:
    """The model2vec model id of this release, verbatim."""
    return MODEL


def dimensions() -> int:
    """The vector width of this release. The model's, and the ``vector(N)`` column's."""
    return DIMENSIONS


def distance_threshold() -> float:
    """Cosine distance above which a row stops counting as a semantic ``search`` hit."""
    return float(_settings()["DISTANCE_THRESHOLD"])


@lru_cache(maxsize=1)
def _load_model_cached() -> StaticModel:
    """Load :data:`MODEL` once per process: from the baked directory when the image has one."""
    from model2vec import StaticModel

    baked_path = MODEL_PATH if os.path.isdir(MODEL_PATH) else None
    if baked_path is not None:
        stamp = os.path.join(baked_path, MODEL_ID_FILENAME)
        if os.path.exists(stamp):
            with open(stamp, encoding="utf-8") as handle:
                baked = handle.read().strip()
            if baked != MODEL:
                raise ImproperlyConfigured(f"{baked_path!r} holds the weights of {baked!r} but this release embeds with {MODEL!r}. Rebuild the image.")
        model = StaticModel.from_pretrained(baked_path)
    else:
        # ``force_download`` defaults to True upstream, which would re-fetch the weights on
        # every process start; the hub cache is exactly what a dev box or CI runner wants.
        model = StaticModel.from_pretrained(MODEL, force_download=False)
    if int(model.dim) != DIMENSIONS:
        raise ImproperlyConfigured(f"Embedding model {MODEL!r} produces {model.dim}-wide vectors but this release stores {DIMENSIONS}-wide ones. The two are constants of `embeddings.engine` and change together, with a migration of the vector column.")
    logger.info("Embedding model %s loaded (%d dims)%s", MODEL, DIMENSIONS, f" from {baked_path}" if baked_path else "")
    return model


def _model() -> StaticModel:
    """The loaded model, or :class:`EmbeddingsUnavailable` with the cause chained."""
    try:
        # ``lru_cache`` is not atomic: two worker threads would both load. The lock is only
        # contended during the first load of a process.
        with _load_lock:
            return _load_model_cached()
    except ImproperlyConfigured:
        raise
    except Exception as exc:  # weights missing, no network, corrupt files, ...
        raise EmbeddingsUnavailable(f"Embedding model {MODEL!r} could not be loaded: {exc}") from exc


def reset() -> None:
    """Drop the loaded model so the next call loads it again (tests)."""
    with _load_lock:
        _load_model_cached.cache_clear()


def warm_up() -> None:
    """Load the model now rather than on the first row or query.

    Raises :class:`django.core.exceptions.ImproperlyConfigured` when the image carries another
    model's weights or the width is not the model's, and :class:`EmbeddingsUnavailable` when the
    weights cannot be loaded. A no-op when disabled.
    """
    if enabled():
        _model()


def source_text(*texts: str | None) -> str | None:
    """The text a row is embedded from: its source fields, each stripped, newline-joined.

    ``None`` when they are all blank -- such a row has no vector (never a zero vector).

    Variadic, rather than ``(name, description)``: the mixin splats a model's
    ``embedding_source_fields`` into this, and models that carry their text in one field
    (an app's identifier, a repo's name) or in three raised ``TypeError`` on their first save.
    """
    parts = [part.strip() for part in texts if part and part.strip()]
    return "\n".join(parts) if parts else None


def embed_texts(texts: Sequence[str]) -> list[list[float] | None]:
    """Embed each text; unit-length vectors as plain lists, ``None`` where the model gave zeros.

    A static model can emit an all-zero vector for text made only of unknown tokens (or for
    the empty string). Cosine distance to a zero vector is NaN, so such a row must store
    ``NULL`` and such a query must not run the semantic leg.
    """
    if not texts:
        return []
    model = _model()
    matrix = np.asarray(model.encode(list(texts), show_progress_bar=False, use_multiprocessing=False), dtype=np.float32)
    if matrix.ndim == 1:  # a single text comes back as one row
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1)
    out: list[list[float] | None] = []
    for row, norm in zip(matrix, norms, strict=True):
        if not np.isfinite(norm) or norm == 0.0:
            out.append(None)
        else:
            out.append((row / norm).astype(float).tolist())
    return out


def embed_query(text: str) -> list[float] | None:
    """The vector for a search query, or ``None`` when the text is blank or embeds to zeros."""
    if not text or not text.strip():
        return None
    return embed_texts([text.strip()])[0]


def column_dimensions(connection: Any, table: str, column: str) -> int | None:
    """The declared width of ``table.column``'s ``vector(N)`` type, or ``None`` if absent.

    ``None`` also when the type carries no modifier (a bare ``vector`` column).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid WHERE c.relname = %s AND a.attname = %s AND NOT a.attisdropped",
            [table, column],
        )
        row = cursor.fetchone()
    if row is None:
        return None
    declared = str(row[0])  # e.g. "vector(256)"
    if not declared.startswith("vector(") or not declared.endswith(")"):
        return None
    return int(declared[len("vector(") : -1])

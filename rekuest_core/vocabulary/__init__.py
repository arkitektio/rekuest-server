"""The blok language v2, as the server speaks it: arkitekt-spec's ``vocabulary.json`` (vendored,
byte for byte, with its typed loader ``vocabulary.py``), the spec-owned v2 catalogs
(``catalogs/``) and the Orkestrator's module (``modules/``), the checker a v2 tree is held to (``check.py``, walking it as ``walk.py`` does) and the
inference that resolves a blok's left-out local kinds and dependencies first (``infer.py``).

Refresh from an arkitekt-spec checkout (the files are the spec's; never edit them here):

    cp ~/Code/packages/arkitekt-spec/arkitekt_spec/blok/{vocabulary.json,vocabulary.py} rekuest_core/vocabulary/
    cp ~/Code/packages/arkitekt-spec/arkitekt_spec/blok/catalogs/*.json rekuest_core/vocabulary/catalogs/
    cp ~/Code/packages/arkitekt-spec/arkitekt_spec/blok/modules/*.json rekuest_core/vocabulary/modules/

takt vendors the same files (``crates/rekuest-server-core/src/blok/data/``); the spec corpus
(``takt/crates/rekuest-server-core/tests/fixtures/bsx_v2.json``) holds both checkers to it.
"""

from rekuest_core.vocabulary.check import BlokScope, check_blok_tree
from rekuest_core.vocabulary.infer import resolve_scope
from rekuest_core.vocabulary.view import BlokView, Shadow, blok_view, catalogs_of, default_view
from rekuest_core.vocabulary.vocabulary import JSON, VOCAB, BlokError, Finding, Vocabulary, diagnostic_message, load_vocabulary

__all__ = [
    "JSON",
    "VOCAB",
    "BlokError",
    "BlokScope",
    "BlokView",
    "Finding",
    "Shadow",
    "Vocabulary",
    "blok_view",
    "catalogs_of",
    "check_blok_tree",
    "default_view",
    "diagnostic_message",
    "load_vocabulary",
    "resolve_scope",
]

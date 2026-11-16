"""The blok language v2, as the server speaks it: arkitekt-spec's ``vocabulary.json`` (vendored,
byte for byte, with its typed loader ``vocabulary.py``), what a blok says of itself in its tree
(``declared.py``: its ``<Blok>`` header, its ``<local>``, ``<query>`` and ``<subscription>``
declarations), the checker a v2 tree is held to where it is mounted (``check.py``, walking it as
``walk.py`` does) and the inference that resolves its locals' left-out kinds first (``infer.py``).

The server holds a blok to the language alone. It reads no catalog: what a component takes,
where it may sit and which catalog it is of is checked by whoever draws the blok.

Refresh from an arkitekt-spec checkout (the files are the spec's; never edit them here):

    cp ~/Code/packages/arkitekt-spec/arkitekt_spec/blok/{vocabulary.json,vocabulary.py} rekuest_core/vocabulary/

takt vendors the same file (``crates/rekuest-server-core/src/blok/data/``); the spec corpus
(``takt/crates/rekuest-server-core/tests/fixtures/bsx_v2.json``) holds both checkers to what it
says the language alone finds (each checked case's ``language``).
"""

from rekuest_core.vocabulary.check import BlokScope, LocalBinding, check_blok_tree
from rekuest_core.vocabulary.declared import (
    BlokHeader,
    blok_header,
    declared_locals,
    declared_queries,
    declared_subscriptions,
    header_problem,
    local_node,
)
from rekuest_core.vocabulary.infer import ResolvedLocal, resolve_local, resolve_locals
from rekuest_core.vocabulary.vocabulary import JSON, VOCAB, BlokError, Finding, Vocabulary, diagnostic_message, load_vocabulary

__all__ = [
    "JSON",
    "VOCAB",
    "BlokError",
    "BlokHeader",
    "BlokScope",
    "Finding",
    "LocalBinding",
    "ResolvedLocal",
    "Vocabulary",
    "blok_header",
    "check_blok_tree",
    "declared_locals",
    "declared_queries",
    "declared_subscriptions",
    "diagnostic_message",
    "header_problem",
    "load_vocabulary",
    "local_node",
    "resolve_local",
    "resolve_locals",
]

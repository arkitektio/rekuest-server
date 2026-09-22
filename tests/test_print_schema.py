"""Smoke test: the GraphQL schema must build and render to a non-empty SDL string.

No database required — this only imports and stringifies the schema.
"""

import re

from facade.schema import schema


def test_print_schema():
    sdl = str(schema)
    print(sdl)  # visible with `pytest -s`
    assert sdl.strip(), "Schema SDL should not be empty"


def test_no_phantom_column_ordering_keys():
    """Guard against the ``status``/``startedAt`` class of bug returning.

    ``strawberry_django.order_type`` never validates its annotations against the model, so an
    ordering key naming a nonexistent column builds a perfectly valid SDL and only raises
    ``FieldError`` at query time. ``TaskStatus`` had no backing column and no consumer at all.
    """
    sdl = str(schema)
    assert "TaskStatus" not in sdl

    # ``Session`` genuinely has started_at/ended_at — SessionOrder is the legitimate original
    # these two were copy-pasted from, so scope the assertion to the offending inputs.
    for input_name in ("TaskOrder", "ImplementationOrder"):
        block = re.search(rf"input {input_name} [^{{]*\{{[^}}]*\}}", sdl)
        assert block, f"{input_name} missing from the SDL"
        for phantom in ("startedAt", "finishedAt", "status"):
            if input_name == "TaskOrder" and phantom == "finishedAt":
                continue  # Task really does have finished_at
            assert phantom not in block.group(0), f"{input_name}.{phantom} names a nonexistent column"


def test_blok_dependency_has_no_phantom_implementation_field():
    """`BlokDependency.implementation` was declared on the GraphQL type but never existed on the model."""
    from facade.schema import schema

    sdl = str(schema)
    block = sdl[sdl.index("type BlokDependency") :]
    block = block[: block.index("}")]
    assert "implementation:" not in block
    assert "blok: Blok!" in block


def test_sdl_has_base_catalog_and_diagnostics():
    """The base catalog query, stored diagnostics and the additive call fields are in the SDL."""
    sdl = str(schema)
    assert "baseCatalog: BaseCatalog!" in sdl
    assert "type Diagnostic {" in sdl
    assert "diagnostics: [Diagnostic!]!" in sdl
    validator = sdl[sdl.index("type Validator {") :]
    validator = validator[: validator.index("}")]
    assert "callJson: JSONSerializable!" in validator
    assert "source: String" in validator
    for effect in ("HideEffect", "MessageEffect", "CustomEffect"):
        block = sdl[sdl.index(f"type {effect} implements Effect") :]
        block = block[: block.index("}")]
        assert "callJson: JSONSerializable!" in block, effect


#: The types that publish their stored vector as an `Embedding` string.
EMBEDDING_TYPES = {"Action"}


def _fields_named_embedding(sdl: str) -> dict:
    """``{"type"|"input": {names of the definitions carrying an `embedding` field}}``."""
    import re

    found = {"type": set(), "input": set()}
    kind = name = None
    for line in sdl.splitlines():
        header = re.match(r"(type|input|interface)\s+(\w+)", line)
        if header:
            kind, name = header.group(1), header.group(2)
        elif line.startswith("}"):
            kind = name = None
        elif kind in found and re.match(r"\s+embedding(\(|:)", line):
            found[kind].add(name)
    return found


def test_only_the_embedded_types_publish_a_vector():
    """A vector is readable, and only where a model actually carries one.

    This used to assert the word "embedding" appeared nowhere in the SDL. The vectors are
    published now -- as one self-describing `Embedding` string per row -- so the blanket ban
    is gone, but the half of it that mattered is not: the exact set of types is pinned, so a
    vector column cannot arrive on a type by accident.
    """
    assert _fields_named_embedding(str(schema))["type"] == EMBEDDING_TYPES


def test_no_input_accepts_an_embedding():
    """Read-only: a client may not write a vector, or the healer's contract is a fiction."""
    assert _fields_named_embedding(str(schema))["input"] == set()


def test_the_raw_model_column_is_never_published():
    """`embeddingModel` is carried inside the `Embedding` string, never as its own field."""
    assert "embeddingModel" not in str(schema)



def test_checked_in_sdl_is_current():
    """schema.graphql at the repo root is the printed schema."""
    from pathlib import Path

    assert Path(__file__).resolve().parents[1].joinpath("schema.graphql").read_text() == str(schema)

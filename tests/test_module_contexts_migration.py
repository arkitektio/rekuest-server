"""Migration 0019: a module registered before the self-contained blok root parses again."""

import importlib

migration = importlib.import_module("facade.migrations.0019_module_contexts")


def test_old_contexts_are_renamed_and_the_rest_is_kept() -> None:
    stored = [
        {"name": "Histogram", "context": "HTML", "children_context": None, "props": []},
        {"name": "Glyph", "context": "ACTION_LOGO", "children_context": "ACTION_LOGO"},
        {"name": "Frame", "context": "SCENE", "children_context": "HTML"},
    ]
    assert [(c["context"], c["children_context"]) for c in migration.renamed(stored)] == [("PANEL", None), ("LOGO", "LOGO"), ("SCENE", "PANEL")]
    assert migration.renamed(stored)[0]["props"] == []

"""Migration 0032: a module's stored props and operations become catalog ports."""

import importlib

from rekuest_core.inputs.models import (
    CatalogComponentInputModel,
    CatalogOperationInputModel,
)

migration = importlib.import_module("facade.migrations.0032_catalog_ports")


def test_props_become_ports() -> None:
    stored = [
        {
            "name": "Switch",
            "props": [
                {
                    "key": "label",
                    "kind": "STRING",
                    "required": True,
                    "description": "Its label.",
                },
                {"key": "variant", "kind": "STRING", "values": ["default", "outline"]},
                {
                    "key": "onChange",
                    "kind": "CALLBACK",
                    "event": [{"key": "value", "kind": "BOOL", "description": "On?"}],
                },
                {"key": "onClick", "kind": "CALLBACK", "event": None},
                {"key": "src", "kind": "ASSET"},
                {"key": "bind", "kind": "FLOAT"},
            ],
        }
    ]
    props = migration.converted_components(stored)[0]["props"]
    assert [prop["port"] for prop in props] == [
        {"kind": "STRING"},
        {"kind": "STRING", "choices": [{"value": "default"}, {"value": "outline"}]},
        {
            "kind": "CALLBACK",
            "children": [{"key": "value", "kind": "BOOL", "description": "On?"}],
        },
        {"kind": "CALLBACK"},
        {"kind": "ASSET"},
        {"kind": "BINDING", "children": [{"key": "...", "kind": "FLOAT"}]},
    ]
    assert props[0] == {
        "key": "label",
        "required": True,
        "description": "Its label.",
        "port": {"kind": "STRING"},
    }
    CatalogComponentInputModel(**migration.converted_components(stored)[0])
    assert migration.converted_components(
        migration.converted_components(stored)
    ) == migration.converted_components(stored)


def test_a_list_dict_or_any_prop_drops_the_components() -> None:
    for kind in ("LIST", "DICT", "ANY"):
        stored = [
            {"name": "Box", "props": [{"key": "label", "kind": "STRING"}]},
            {"name": "Table", "props": [{"key": "rows", "kind": kind}]},
        ]
        assert migration.converted_components(stored) == []
    event = [
        {
            "name": "Tabs",
            "props": [
                {
                    "key": "onChange",
                    "kind": "CALLBACK",
                    "event": [{"key": "value", "kind": "ANY"}],
                }
            ],
        }
    ]
    assert migration.converted_components(event) == []


def test_operations_become_ports() -> None:
    stored = [
        {
            "name": "pick",
            "description": None,
            "arguments": [
                {"key": "items", "kind": "LIST", "required": True, "description": None},
                {"key": "value", "kind": "ANY", "required": False, "description": None},
                {"key": "at", "kind": "INT", "required": True, "description": None},
            ],
            "returns": "DICT",
        },
        {"name": "flag", "arguments": [], "returns": "BOOL"},
        {"name": "any", "arguments": [], "returns": "ANY"},
    ]
    operations = migration.converted_operations(stored)
    assert [argument["port"] for argument in operations[0]["arguments"]] == [
        {"kind": "LIST", "children": [{"key": "...", "kind": None}]},
        None,
        {"kind": "INT"},
    ]
    assert [operation["returns"] for operation in operations] == [
        {"kind": "DICT", "children": [{"key": "...", "kind": None}]},
        {"kind": "BOOL"},
        None,
    ]
    for operation in operations:
        CatalogOperationInputModel(**operation)
    assert migration.converted_operations(operations) == operations

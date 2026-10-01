"""Record what the rekuest server's own models make of declarations: the contract for
rekuest_core::inputs.

Run with the server's venv, from this directory:
    /home/jhnnsrs/Code/worktrees/rekuest-server-workflows/.venv/bin/python generate_declarations.py

Inputs: app_declarations.json (REGISTER payloads real apps send, dumped from their App
registries) and the broken variants below. Output: declarations.json, where each case has the
validated `model_dump(mode="json")` of every implementation, state and blok, plus each
definition's `unique_hash`, or the validation message Python raises.
"""

import copy
import json
import os
import sys
from pathlib import Path

SERVER = "/home/jhnnsrs/Code/worktrees/rekuest-server-workflows"
sys.path.insert(0, SERVER)
os.chdir(SERVER)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rekuest.settings_test")
import django  # noqa: E402

django.setup()
from pydantic import ValidationError  # noqa: E402

from facade.mutations.agent import ImplementAgentInputModel  # noqa: E402

HERE = Path(__file__).parent


def record(payload: dict) -> dict:
    try:
        model = ImplementAgentInputModel.model_validate(payload)
    except ValidationError as e:
        error = e.errors()[0]
        message = error["msg"].removeprefix("Value error, ")
        return {"input": payload, "error": message, "error_type": error["type"]}
    return {
        "input": payload,
        "dump": model.model_dump(mode="json"),
        "hashes": [impl.definition.unique_hash for impl in model.implementations or []],
    }


def impl(**definition) -> dict:
    base = {"key": "act", "name": "Act", "kind": "FUNCTION", "args": [], "returns": []}
    return {"interface": "act", "definition": {**base, **definition}}


def port(key: str, kind: str, **rest) -> dict:
    return {"key": key, "kind": kind, **rest}


def call(operation: str = "isTrue", **arguments) -> dict:
    return {"operation": operation, "arguments": [{"key": k, **v} for k, v in arguments.items()] or None}


BROKEN = {
    "reserved key": impl(args=[port("value", "INT")]),
    "separator in key": impl(args=[port("a..b", "INT")]),
    "empty key": impl(args=[port("", "INT")]),
    "list without child": impl(args=[port("l", "LIST")]),
    "int with children": impl(args=[port("i", "INT", children=[port("x", "INT")])]),
    "union with one variant": impl(args=[port("u", "UNION", children=[port("a", "INT")])]),
    "dict mixing item and named": impl(args=[port("d", "DICT", children=[port("...", "INT"), port("n", "INT")])]),
    "structure without identifier": impl(args=[port("s", "STRUCTURE")]),
    "bad identifier": impl(args=[port("s", "STRUCTURE", identifier="mikro/image")]),
    "identifier on int": impl(args=[port("i", "INT", identifier="@a/b")]),
    "enum without choices": impl(args=[port("e", "ENUM")]),
    "choices on bool": impl(args=[port("b", "BOOL", choices=[{"value": True, "label": "yes"}])]),
    "duplicate choices": impl(args=[port("e", "ENUM", choices=[{"value": 1, "label": "a"}, {"value": 1, "label": "b"}])]),
    "quantity without unit": impl(args=[port("q", "QUANTITY")]),
    "quantity bad proposal": impl(args=[port("q", "QUANTITY", reference_unit="volt", proposed_units=["meter"])]),
    "quantity bad dimension": impl(args=[port("q", "QUANTITY", reference_unit="volt", dimension="[length]")]),
    "unit field on int": impl(args=[port("i", "INT", reference_unit="volt")]),
    "default wrong kind": impl(args=[port("i", "INT", default="x")]),
    "default not a choice": impl(args=[port("s", "STRING", default="c", choices=[{"value": "a", "label": "A"}])]),
    "slider on string": impl(args=[port("s", "STRING", widget={"kind": "SLIDER", "min": 0, "max": 1})]),
    "slider empty range": impl(args=[port("f", "FLOAT", widget={"kind": "SLIDER", "min": 2, "max": 1})]),
    "slider default outside": impl(args=[port("f", "FLOAT", default=5, widget={"kind": "SLIDER", "min": 0, "max": 1})]),
    "choice widget without choices": impl(args=[port("s", "STRING", widget={"kind": "CHOICE"})]),
    "widget of another kind's field": impl(args=[port("s", "STRING", widget={"kind": "STRING", "min": 1})]),
    "search query without variables": impl(args=[port("s", "STRUCTURE", identifier="@a/b", widget={"kind": "SEARCH", "query": "query Q { a }", "ward": "a"})]),
    "search query not parsing": impl(args=[port("s", "STRUCTURE", identifier="@a/b", widget={"kind": "SEARCH", "query": "query Q(", "ward": "a"})]),
    "state choice without pointer": impl(args=[port("s", "STRING", widget={"kind": "STATE_CHOICE"})]),
    "custom widget impure prop": impl(args=[port("s", "STRING", widget={"kind": "CUSTOM", "component": "X", "props": [{"key": "p", "agent_call": {"dependency": "d", "operation": "o"}}]})]),
    "effect impure": impl(args=[port("i", "INT", effects=[{"kind": "HIDE", "call": {"operation": "o", "arguments": [{"key": "k", "agent_call": {"dependency": "d", "operation": "o"}}]}}])]),
    "effect undeclared dependency": impl(args=[port("i", "INT", effects=[{"kind": "HIDE", "call": {"operation": "o", "arguments": [{"key": "k", "value_path": "/other"}]}}])]),
    "message effect without message": impl(args=[port("i", "INT", effects=[{"kind": "MESSAGE", "call": {"operation": "o"}}])]),
    "fade on message effect": impl(args=[port("i", "INT", effects=[{"kind": "MESSAGE", "message": "m", "fade": True, "call": {"operation": "o"}}])]),
    "dependency not a port": impl(args=[port("i", "INT", validators=[{"call": {"operation": "o"}, "dependencies": ["nope"]}])]),
    "argument bound twice": impl(args=[port("i", "INT", validators=[{"call": {"operation": "o", "arguments": [{"key": "k", "value_path": "/value", "value_literal": 1}]}}])]),
    "duplicate call argument key": impl(args=[port("i", "INT", validators=[{"call": {"operation": "o", "arguments": [{"key": "k", "value_literal": 1}, {"key": "k", "value_literal": 2}]}}])]),
    "args and returns share a key": impl(args=[port("x", "INT")], returns=[port("x", "INT")]),
    "duplicate arg keys": impl(args=[port("x", "INT"), port("x", "STRING")]),
    "group lists unknown arg": impl(args=[port("x", "INT")], port_groups=[{"key": "g", "ports": ["y"]}]),
    "arg in two groups": impl(args=[port("x", "INT")], port_groups=[{"key": "g", "ports": ["x"]}, {"key": "h", "ports": ["x"]}]),
    "IN needs a list": impl(args=[port("s", "STRUCTURE", identifier="@a/b", requires=[{"key": "k", "operator": "IN", "value": 1}])]),
    "EXISTS takes no value": impl(returns=[port("s", "STRUCTURE", identifier="@a/b", provides=[{"key": "k", "operator": "EXISTS", "value": 3}])]),
    "test target empty": impl(is_test_for=[{}]),
    "unknown definition field": impl(bogus=1),
    "proxy undeclared dependency": impl(args=[port("s", "STRING", widget={"kind": "PROXY", "target_port": "p", "target_action": "a", "target_dependency": "d"})]),
}

VALID_EXTRA = {
    "quantity canonicalized": impl(args=[port("q", "QUANTITY", reference_unit="mV", proposed_units=["V", "kV"])]),
    "rich widgets": impl(
        args=[
            port("f", "FLOAT", default=0.5, widget={"kind": "SLIDER", "min": 0, "max": 1, "step": 0.1}),
            port("s", "STRING", widget={"kind": "CUSTOM", "component": "X", "props": [{"key": "p", "static_value": True}], "fallback": {"kind": "STRING", "placeholder": "p"}}),
            port("e", "ENUM", default="a", choices=[{"value": "a", "label": "A"}, {"value": "b", "label": "B"}], widget={"kind": "CHOICE"}),
            port("m", "MODEL", identifier="@pkg/model", children=[port("x", "INT", nullable=True), port("y", "DATE", default="2026-09-30")]),
            port("l", "LIST", children=[port("...", "STRUCTURE", identifier="@mikro/image")], widget={"kind": "SEARCH", "query": "query Q($search: String, $values: [ID!]) { a }", "ward": "mikro"}),
        ],
        returns=[port("r", "DICT", children=[port("...", "FLOAT")], provides=[{"key": "@mikro/axes", "operator": "IN", "value": ["x"]}])],
        port_groups=[{"key": "g", "title": "G", "ports": ["f", "s"]}],
        collections=["c"],
        description="é unicode \n and floats 1e-07",
    ),
}

cases = {}
for name, declaration in json.loads((HERE / "app_declarations.json").read_text()).items():
    cases[f"app {name}"] = record(declaration)
for name, implementation in {**VALID_EXTRA, **BROKEN}.items():
    cases[name] = record({"implementations": [implementation]})

(HERE / "declarations.json").write_text(json.dumps(cases, indent=1, ensure_ascii=False) + "\n")
valid = sum(1 for c in cases.values() if "dump" in c)
print(f"{len(cases)} cases: {valid} valid, {len(cases) - valid} refused")
for name, case in cases.items():
    if name in BROKEN and "dump" in case:
        print(f"  NOTE: {name!r} was accepted by Python")

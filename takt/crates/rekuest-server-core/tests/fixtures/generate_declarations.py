"""Record what the rekuest server's own models make of declarations: the contract for
rekuest_core::inputs.

Run with the server's venv (the repository root's):
    uv run python takt/crates/rekuest-server-core/tests/fixtures/generate_declarations.py

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

SERVER = str(Path(__file__).resolve().parents[5])  # the repository root: the server itself
sys.path.insert(0, SERVER)
os.chdir(SERVER)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rekuest.settings_test")
import django  # noqa: E402

django.setup()
from pydantic import ValidationError  # noqa: E402

from facade.inputs import ImplementAgentInputModel  # noqa: E402

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
    "protocol hint with whitespace": impl(protocols=["on scene"]),
    "protocol hint empty": impl(protocols=[""]),
    "protocol hint twice": impl(protocols=["on_scene_init", "on_scene_init"]),
    "proxy undeclared dependency": impl(args=[port("s", "STRING", widget={"kind": "PROXY", "target_port": "p", "target_action": "a", "target_dependency": "d"})]),
}

VALID_EXTRA = {
    "protocol hints": impl(protocols=["on_scene_annotation", "on_scene_init"]),
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

REF = "sha256:" + "a" * 64


def asset(**fields) -> dict:
    return {"name": "scope", "kind": "MODEL_3D", "ref": REF, **fields}


ASSETS = {
    "asset held by the agent": asset(size=1024),
    "asset named by a source": asset(source="https://vendor.example/scope.glb?v=2"),
    "asset with a source and a size": asset(source="https://vendor.example/scope.glb", size=1024),
    "asset without size or source": asset(),
    "asset of no size": asset(size=0),
    "asset source not https": asset(source="http://vendor.example/scope.glb"),
    "asset source without a host": asset(source="https://"),
    "asset source too long": asset(source="https://vendor.example/" + "a" * 2000),
    "asset name not an identifier": asset(name="my scope", size=1),
    "asset bad reference": asset(ref="md5:abc", size=1),
}

def html(*children: dict) -> list[dict]:
    """An html blok's tree: its root is `Html` (the root names the context)."""
    return [{"id": "Html[0]", "component": "Html", "children": list(children)}]


def blok(**fields) -> dict:
    """An agent blok; an html one's single root wrapped in `Html` (two roots are left as sent)."""
    tree = [{"id": "Box[0]", "component": "Box", "props": [{"key": "label", "dynamic_value": {"path": "args.image"}}]}]
    components = fields.pop("components", tree)
    if fields.get("context", "HTML") == "HTML" and len(components) == 1:
        components = html(*components)
    return {"key": "inspector", "vocab": 2, "components": components, **fields}


IMAGE = port("image", "STRUCTURE", identifier="@mikro/image", nullable=False)
POSE = [{"key": "pose", "kind": "LIST", "children": [{"key": "...", "kind": "FLOAT"}], "default": [1, 0, 0, 0], "persist": True}, {"key": "zoom", "kind": "INT", "default": 1}]
SELF = [{"key": "self", "state_dependencies": [{"key": "arm", "demand": {"matches": [{"key": "mode", "kind": "STRING"}]}}], "action_dependencies": [{"key": "move", "demand": {"arg_matches": [{"key": "to"}]}}]}]
POSITIONS = {"key": "positions", "service": "mikro", "document": "query($slide: ID!) { positions(slide: $slide) { id } }", "variables": [{"key": "slide", "value_path": "args.image"}]}

BLOKS = {
    "blok without args": blok(components=[{"id": "Box[0]", "component": "Box"}]),
    "blok with args": blok(args=[{**IMAGE, "requires": [{"key": "@mikro/z_size", "operator": "GTE", "value": 2}]}]),
    "blok with a list arg": blok(args=[port("images", "LIST", nullable=False, children=[IMAGE])], components=[{"id": "Box[0]", "component": "Box"}]),
    "blok args keyed twice": blok(args=[IMAGE, IMAGE]),
    "blok args on a scene": blok(context="SCENE", args=[IMAGE]),
    "blok arg of a bad shape": blok(args=[port("image", "STRUCTURE", nullable=False)]),
    "blok without vocab": {"key": "inspector", "components": html({"id": "Box[0]", "component": "Box"})},
    "blok of another vocab": blok(vocab=1),
    "blok with locals, demo and catalogs": blok(components=[{"id": "Box[0]", "component": "Box"}], locals=POSE, demo={"args": {"image": None}}, catalogs=["ui@1"], module="orkestrator"),
    "blok with a local twice": blok(locals=[POSE[0], POSE[0]]),
    "blok with a reserved local": blok(locals=[{"key": "task", "kind": "STRING"}]),
    "blok with a local of no kind": blok(locals=[{"key": "zoom", "default": 1}]),
    "blok with a local whose default does not fit": blok(locals=[{"key": "zoom", "kind": "INT", "default": "1"}]),
    "blok with a local of a bad shape": blok(locals=[{"key": "form", "kind": "DICT"}]),
    "blok with a catalog badly spelled": blok(catalogs=["ui"]),
    "blok with a query": blok(args=[IMAGE], queries=[POSITIONS], components=[{"id": "Text[0]", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "queries.positions.status"}}]}]),
    "blok with a mutation": blok(queries=[{**POSITIONS, "document": "mutation { a }"}]),
    "blok with a query key that is no name": blok(queries=[{**POSITIONS, "key": "my positions"}]),
    "blok with a query twice": blok(queries=[POSITIONS, POSITIONS]),
    "blok with locals twice out of order": blok(locals=[{"key": k, "kind": "STRING"} for k in ("a", "b", "b", "a")]),
    "blok with a query fetching more": blok(queries=[{**POSITIONS, "more": "positions"}], components=[{"id": "Button[0]", "component": "Button", "props": [{"key": "onClick", "util_call": {"operation": "query.fetch_more", "arguments": [{"key": "query", "value_literal": "positions"}, {"key": "variables", "value_dict": [{"key": "after", "value_literal": 1}]}]}}]}]),
    "blok fetching more of a query without more": blok(queries=[POSITIONS], components=[{"id": "Button[0]", "component": "Button", "props": [{"key": "onClick", "util_call": {"operation": "query.fetch_more", "arguments": [{"key": "query", "value_literal": "positions"}]}}]}]),
    "blok with a query more that is no path": blok(queries=[{**POSITIONS, "more": "positions[0]"}]),
    "blok with a subscription document as a query": blok(queries=[{**POSITIONS, "document": "subscription { a }"}]),
    "blok with a query of no operation": blok(queries=[{**POSITIONS, "document": "  # nothing"}]),
    "blok with a query document as a subscription": blok(subscriptions=[{"key": "s", "service": "rekuest", "document": "query { a }"}]),
    "blok with a subscription that is a mutation": blok(subscriptions=[{"key": "s", "service": "rekuest", "document": "mutation { a }"}]),
    "blok with a subscription replaying an unknown query": blok(subscriptions=[{"key": "s", "service": "rekuest", "document": "subscription { a }", "history": "nope"}]),
    "blok reading an undeclared query": blok(components=[{"id": "Text[0]", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "queries.positions.value"}}]}]),
    "blok reading deps as an agent": blok(dependencies=SELF, components=[{"id": "Text[0]", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "deps.robot.arm"}}]}]),
    "blok reading its own state": blok(dependencies=SELF, components=[{"id": "Text[0]", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "self.arm.mode"}}]}]),
    "blok calling itself into a record": blok(dependencies=SELF, components=[{"id": "Button[0]", "component": "Button", "props": [{"key": "onClick", "declares_value": "move", "agent_call": {"dependency": "self", "operation": "move", "arguments": [{"key": "to", "value_literal": True}]}}, {"key": "disabled", "dynamic_value": {"path": "move.running"}}]}]),
    "blok reading now": blok(components=[{"id": "Text[0]", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "now"}}]}]),
    "blok reading then": blok(components=[{"id": "Text[0]", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "then"}}]}]),
    "blok with an old now_interval": blok(now_interval=5),
    "blok with two roots": blok(components=[{"id": "Box[0]", "component": "Box"}, {"id": "Box[1]", "component": "Box"}]),
    "blok demo naming no dependency": blok(demo={"deps": {"robot": {}}}),
    "action logo declaring a local": blok(context="ACTION_LOGO", components=[{"id": "Logo[0]", "component": "ActionLogo"}], locals=[{"key": "x", "kind": "STRING"}]),
}

def anchor(key: str, *children: dict) -> dict:
    return {"id": f"anchor-{key}", "component": "Anchor", "props": [{"key": "dep", "static_value": key}], "children": list(children) or None}


def through(*keys: str) -> dict:
    return {"id": "route", "component": "Route", "props": [{"key": "through", "static_value": list(keys)}, {"key": "progress", "dynamic_value": {"path": "task.progress"}}]}


def overlay(*children: dict, **fields) -> dict:
    return {"context": "OVERLAY", "vocab": 2, "components": [{"id": "root", "component": "Overlay", "children": list(children)}], **fields}


def flow(*bloks: dict, dependencies: tuple[str, ...] = ("robot", "frame")) -> dict:
    return {**impl(bloks=list(bloks), args=[port("slides", "LIST", nullable=False, children=[port("...", "STRING", nullable=False)])]), "dependencies": [{"key": key} for key in dependencies]}


GLOW = {"id": "glow", "component": "Glow", "props": [{"key": "on", "dynamic_value": {"path": "self.helsinki.stage"}}]}
SHOW = [{"key": "show_path", "kind": "BOOL", "default": True, "persist": True}]

ACTION_BLOKS = {
    "action bloks": flow(
        overlay(anchor("robot", GLOW), through("robot", "frame", "robot"), locals=SHOW),
        {"context": "HTML", "vocab": 2, "components": html({"id": "card", "component": "Card", "props": [{"key": "value", "dynamic_value": {"path": "args.slides"}}], "children": [{"id": "ask", "component": "ask"}]}), "description": "The run"},
    ),
    "action blok through an unknown key": flow(overlay(through("robot", "stitcher"))),
    "action blok anchored at an unknown key": flow(overlay(anchor("stitcher"))),
    "action blok anchored at self": flow(overlay(anchor("self"))),
    "action blok anchored at a dynamic key": flow(overlay({"id": "at", "component": "Anchor", "props": [{"key": "dep", "dynamic_value": {"path": "show_path"}}]}, locals=SHOW)),
    "action blok reading an unknown root": flow(overlay({"id": "t", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "stage.position"}}]})),
    "action blok reading an undeclared arg": flow(overlay({"id": "t", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "args.nope"}}]})),
    "action blok calling a dependency": flow(overlay({"id": "b", "component": "Button", "props": [{"key": "onClick", "agent_call": {"dependency": "robot", "operation": "home"}}]})),
    "action blok calling an undeclared agent": flow(overlay({"id": "b", "component": "Button", "props": [{"key": "onClick", "agent_call": {"dependency": "stage", "operation": "home"}}]})),
    "action bloks twice in one context": flow(overlay(), overlay()),
    "action blok in a logo context": flow({"context": "LOGO", "vocab": 2, "components": [{"id": "root", "component": "AgentLogo"}]}),
    "action blok sent twice": flow({"context": "HTML", "vocab": 2, "components": html({"id": "root", "component": "Card"}), "source": "<Html><Card /></Html>"}),
    "action blok sent not at all": flow({"context": "HTML", "vocab": 2}),
    "action blok without vocab": flow({"context": "HTML", "components": html({"id": "root", "component": "Card"})}),
    "action blok with an unknown field": flow({**overlay(), "name": "nope"}),
    "action blok with an old field": flow({**overlay(), "demo_state": {}}),
    "agent declaring an overlay blok": {"bloks": [{"key": "over", "vocab": 2, "context": "OVERLAY", "components": [{"id": "root", "component": "Overlay"}]}]},
}

cases = {}
for name, declaration in json.loads((HERE / "app_declarations.json").read_text()).items():
    cases[f"app {name}"] = record(declaration)
for name, implementation in {**VALID_EXTRA, **BROKEN}.items():
    cases[name] = record({"implementations": [implementation]})

for name, declared in ASSETS.items():
    cases[name] = record({"assets": [declared]})

for name, declared in BLOKS.items():
    cases[name] = record({"bloks": [declared]})

for name, declared in ACTION_BLOKS.items():
    cases[name] = record(declared if "bloks" in declared and "definition" not in declared else {"implementations": [declared]})

(HERE / "declarations.json").write_text(json.dumps(cases, indent=1, ensure_ascii=False) + "\n")
valid = sum(1 for c in cases.values() if "dump" in c)
print(f"{len(cases)} cases: {valid} valid, {len(cases) - valid} refused")
for name, case in cases.items():
    if name in BROKEN and "dump" in case:
        print(f"  NOTE: {name!r} was accepted by Python")

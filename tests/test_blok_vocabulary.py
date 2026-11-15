"""The blok language v2 checker (``rekuest_core.vocabulary``) against the spec's corpus.

``bsx_v2.json`` is written by arkitekt-spec (``python -m arkitekt_spec.blok.corpus``); its
checked cases are trees with the scope they were checked against. The server holds a blok to
the language alone and reads no catalog, so it is held to each case's ``language``: the codes
the spec's validator finds, in order, and the locals it resolves, with no catalog read. takt
is held to the same (``takt/crates/rekuest-server-core/tests/bsx.rs``).
"""

import json
from pathlib import Path

import pytest

from rekuest_core.inputs import models as rimodels
from rekuest_core.vocabulary import BlokScope, check_blok_tree, load_vocabulary, resolve_scope
from rekuest_core.vocabulary.check import dependency_hint
from rekuest_core.vocabulary.vocabulary import JSON

FIXTURES = Path(__file__).parent.parent / "takt" / "crates" / "rekuest-server-core" / "tests" / "fixtures"


def _cases(kind: str) -> list[dict[str, JSON]]:
    corpus = json.loads((FIXTURES / "bsx_v2.json").read_text())
    return [case for case in corpus["cases"] if kind in case]


def _scope(raw: dict[str, JSON]) -> BlokScope:
    def entries(key: str) -> list[dict[str, JSON]]:
        value = raw.get(key)
        return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []

    args = raw.get("args")
    return BlokScope(
        kind=str(raw["kind"]),
        context=str(raw["context"]),
        dependencies=tuple(rimodels.AgentDependencyInputModel.model_validate(entry) for entry in entries("dependencies")),
        locals=tuple(rimodels.BlokLocalInputModel.model_validate(entry) for entry in entries("locals")),
        queries=tuple(rimodels.BlokQueryInputModel.model_validate(entry) for entry in entries("queries")),
        subscriptions=tuple(rimodels.BlokSubscriptionInputModel.model_validate(entry) for entry in entries("subscriptions")),
        args=tuple(str(arg) for arg in args) if isinstance(args, list) else None,
    )


def _language(case: dict[str, JSON]) -> dict[str, JSON]:
    """What the spec says the language alone makes of a checked case."""
    language = case["language"]
    assert isinstance(language, dict)
    return language


@pytest.mark.parametrize("case", _cases("scope"), ids=lambda case: str(case["name"]))
def test_a_checked_tree_finds_what_the_spec_finds(case: dict[str, JSON]) -> None:
    tree = rimodels.ComponentNodeInputModel.model_validate(case["tree"])
    scope = _scope(case["scope"]) if isinstance(case["scope"], dict) else BlokScope()
    codes = [finding.code for finding in check_blok_tree(tree, scope)]
    assert codes == _language(case)["codes"]
    assert not [code for code in codes if code.startswith("catalog.")], "the server reads no catalog"


@pytest.mark.parametrize("case", _cases("resolved"), ids=lambda case: str(case["name"]))
def test_a_checked_tree_resolves_its_scope_as_the_spec_does(case: dict[str, JSON]) -> None:
    tree = rimodels.ComponentNodeInputModel.model_validate(case["tree"])
    assert isinstance(case["scope"], dict)
    resolved = resolve_scope(tree, _scope(case["scope"]))
    assert {"locals": [local.model_dump(mode="json", exclude_none=True) for local in resolved.locals]} == _language(case)["resolved"]


def test_an_enum_local_needs_its_choices() -> None:
    with pytest.raises(ValueError, match="must declare choices"):
        rimodels.BlokLocalInputModel.model_validate({"key": "mode", "kind": "ENUM"})
    assert rimodels.BlokLocalInputModel.model_validate({"key": "mode", "kind": "ENUM", "choices": [{"value": "fast", "label": "Fast"}]}).kind == "ENUM"


@pytest.mark.parametrize(
    ("kind", "hint"),
    [
        ("custom", 'dependencies=[AgentDependencyInput(key="laser")]'),
        ("task", 'dependencies=[AgentDependencyInput(key="laser")]'),
        ("action", "add a dependency parameter `laser` to the action"),
    ],
)
def test_the_deps_unknown_hint_says_what_the_kind_does(kind: str, hint: str) -> None:
    tree = rimodels.ComponentNodeInputModel.model_validate({"id": "html", "component": "Html", "children": [{"id": "t", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "deps.laser.power"}}]}]})
    findings = check_blok_tree(tree, BlokScope(kind=kind, context="HTML"))
    assert [finding.code for finding in findings] == ["deps.unknown"]
    assert findings[0].message.endswith(f"Fix: {hint}.")


def test_an_agent_blok_reads_no_deps_at_all() -> None:
    tree = rimodels.ComponentNodeInputModel.model_validate({"id": "html", "component": "Html", "children": [{"id": "t", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "deps.laser.power"}}]}]})
    assert [finding.code for finding in check_blok_tree(tree, BlokScope(kind="agent", context="HTML"))] == ["roots.not_in_kind"]
    assert dependency_hint("laser", "agent") == "agent bloks read only `self`"


def test_a_left_out_kind_stays_left_out_until_resolved() -> None:
    local = rimodels.BlokLocalInputModel.model_validate({"key": "zoom", "kind": None})
    assert local.kind is None
    assert rimodels.BlokLocalInputModel.model_validate({"key": "zoom", "kind": "ANY"}).kind == "ANY"


@pytest.mark.parametrize("case", _cases("tree"), ids=lambda case: str(case["name"]))
def test_a_parsed_tree_dumps_as_the_spec_stores_it(case: dict[str, JSON]) -> None:
    tree = rimodels.ComponentNodeInputModel.model_validate(case["tree"])
    assert tree.model_dump(mode="json", exclude_none=True) == case["tree"]


def test_the_vocabulary_is_the_version_the_corpus_speaks() -> None:
    corpus = json.loads((FIXTURES / "bsx_v2.json").read_text())
    assert load_vocabulary().vocab == corpus["vocab"] == 2



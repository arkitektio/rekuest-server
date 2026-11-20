"""The blok language v2 checker (``rekuest_core.vocabulary``) against the spec's corpus.

``bsx_v2.json`` is written by arkitekt-spec (``python -m arkitekt_spec.blok.corpus``); its
checked cases are trees with the mount they were checked in (``kinds``, ``dependencies`` or null
for a blok mounted nowhere, ``args``). The server holds a blok to
the language alone and reads no catalog, so it is held to each case's ``language``: the codes
the spec's validator finds, in order, and the locals it resolves, with no catalog read. takt
is held to the same (``takt/crates/rekuest-server-core/tests/bsx.rs``).
"""

import json
from pathlib import Path

import pytest

from rekuest_core.inputs import models as rimodels
from rekuest_core.vocabulary import BlokScope, check_blok_tree, load_vocabulary, resolve_locals
from rekuest_core.vocabulary.check import bound_scope, dependency_hint
from rekuest_core.vocabulary.vocabulary import JSON

FIXTURES = Path(__file__).parent.parent / "takt" / "crates" / "rekuest-server-core" / "tests" / "fixtures"


def _cases(kind: str) -> list[dict[str, JSON]]:
    corpus = json.loads((FIXTURES / "bsx_v2.json").read_text())
    return [case for case in corpus["cases"] if kind in case]


def _dependencies(raw: dict[str, JSON]) -> tuple[rimodels.AgentDependencyInputModel, ...] | None:
    """The mount's dependencies; None for a blok mounted nowhere."""
    value = raw.get("dependencies")
    if not isinstance(value, list):
        return None
    return tuple(rimodels.AgentDependencyInputModel.model_validate(entry) for entry in value if isinstance(entry, dict))


def _scope(raw: dict[str, JSON]) -> BlokScope:
    kinds = raw.get("kinds")
    args = raw.get("args")
    port = raw.get("port")
    workflows = raw.get("workflows")
    return BlokScope(
        kinds=tuple(str(kind) for kind in kinds) if isinstance(kinds, list) else (),
        dependencies=_dependencies(raw),
        args=tuple(str(arg) for arg in args) if isinstance(args, list) else None,
        port=rimodels.PortMatchInputModel.model_validate(port) if isinstance(port, dict) else None,
        workflows={
            str(action): tuple(rimodels.AgentDependencyInputModel.model_validate(entry) for entry in entries if isinstance(entry, dict))
            for action, entries in workflows.items()
            if isinstance(entries, list)
        }
        if isinstance(workflows, dict)
        else None,
    )


def _mount(case: dict[str, JSON]) -> dict[str, JSON]:
    mount = case["mount"]
    assert isinstance(mount, dict)
    return mount


def _language(case: dict[str, JSON]) -> dict[str, JSON]:
    """What the spec says the language alone makes of a checked case."""
    language = case["language"]
    assert isinstance(language, dict)
    return language


@pytest.mark.parametrize("case", _cases("mount"), ids=lambda case: str(case["name"]))
def test_a_checked_tree_finds_what_the_spec_finds(case: dict[str, JSON]) -> None:
    tree = rimodels.ComponentNodeInputModel.model_validate(case["tree"])
    codes = [finding.code for finding in check_blok_tree(tree, _scope(_mount(case)))]
    assert codes == _language(case)["codes"]
    assert not [code for code in codes if code.startswith("catalog.")], "the server reads no catalog"


@pytest.mark.parametrize("case", _cases("mount"), ids=lambda case: str(case["name"]))
def test_a_checked_tree_resolves_its_locals_as_the_spec_does(case: dict[str, JSON]) -> None:
    tree = rimodels.ComponentNodeInputModel.model_validate(case["tree"])
    resolved = resolve_locals(tree, bound_scope(tree, _scope(_mount(case))).dependencies)
    locals_ = {node: each.local.model_dump(mode="json", exclude_none=True) for node, each in resolved.items()}
    assert {"locals": locals_} == _language(case)["resolved"]


def test_an_enum_local_needs_its_choices() -> None:
    with pytest.raises(ValueError, match="must declare choices"):
        rimodels.BlokLocalInputModel.model_validate({"key": "mode", "kind": "ENUM"})
    assert rimodels.BlokLocalInputModel.model_validate({"key": "mode", "kind": "ENUM", "choices": [{"value": "fast", "label": "Fast"}]}).kind == "ENUM"


def _reads_the_laser(kind: str) -> rimodels.ComponentNodeInputModel:
    """A panel blok of ``kind`` that reads ``deps.laser.power``."""
    return rimodels.ComponentNodeInputModel.model_validate(
        {
            "id": "Blok[0]",
            "component": "Blok",
            "props": [{"key": "kind", "static_value": kind}, {"key": "context", "static_value": "panel"}],
            "children": [{"id": "Blok[0]/Text[0]", "component": "Text", "props": [{"key": "text", "dynamic_value": {"path": "deps.laser.power"}}]}],
        }
    )


def test_a_blok_mounted_nowhere_reads_any_dependency() -> None:
    assert check_blok_tree(_reads_the_laser("custom")) == []


@pytest.mark.parametrize(
    ("kind", "hint"),
    [
        ("custom", 'dependencies=[AgentDependencyInput(key="laser")]'),
        ("task", 'dependencies=[AgentDependencyInput(key="laser")]'),
        ("action", "add a dependency parameter `laser` to the action"),
    ],
)
def test_the_deps_unknown_hint_says_what_the_kind_does(kind: str, hint: str) -> None:
    tree = _reads_the_laser(kind)
    findings = check_blok_tree(tree, BlokScope(dependencies=()))
    assert [finding.code for finding in findings] == ["deps.unknown"]
    assert findings[0].message.endswith(f"Fix: {hint}.")


def test_an_agent_blok_reads_only_the_dependencies_it_declares() -> None:
    tree = _reads_the_laser("agent")
    assert [finding.code for finding in check_blok_tree(tree, BlokScope(dependencies=()))] == ["deps.unknown"]
    assert dependency_hint("laser", "agent") == 'declare it in the blok: <dependencies key="laser" like="self.<action>" />, then read laser.<dependency>'


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



"""Resolve what a blok leaves to its tree: the kinds of its locals.

The server's port of arkitekt-spec's ``declare/blok/infer.py`` (``resolve_scope``), on
rekuest_core's models. The spec, the server and takt infer the same way, so a source blok sent
without kinds resolves the same everywhere; the spec corpus (``bsx_v2.json``, the ``resolved`` of
its checked cases) holds them to it. A blok stores its locals as written and, beside them, as
resolved (``resolved_locals``: every local with a kind). Dependencies are never inferred: every
``deps.<key>`` a blok reads, whatever its kind, must be declared (``deps.unknown``).

**A local's kind**, when it is left out (a given kind, ``ANY`` included, is kept), is the first of:

1. its ``default``: a bool ``BOOL``, an integer ``INT``, a number ``FLOAT``, a string ``STRING``,
   a non-empty list of one scalar kind a ``LIST`` of it, a non-empty object of one scalar kind a
   ``DICT`` of it (the item port keyed ``...``);
2. the port of the task argument it is passed to whole (``self.move(to: position)``), at the
   first such call in tree order whose demand's arg match has a kind (every child too) and makes
   a valid local;
3. the bind kind of the first control bound to it whole (``<Slider bind={x}>``: ``FLOAT``), when
   that is ``STRING``, ``INT``, ``FLOAT`` or ``BOOL``;
4. else ``ANY``: an untyped value, never an error.

Every argument site comes before every bind site, whatever their place in the tree. Every field
the local sets wins over the inferred type, which only fills what it leaves out; a candidate that
makes no valid local with them is skipped. The other call sites and binds are then checked against
the kind found (``local.arg_kind``, ``local.bind_kind``), which ``ANY`` skips.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from pydantic import ValidationError

from rekuest_core import enums
from rekuest_core.inputs.models import (
    AgentDependencyInputModel,
    AgentProbeInputModel,
    BlokLocalInputModel,
    ComponentNodeInputModel,
    ComponentPropInputModel,
    PortMatchInputModel,
    UtilCallInputModel,
)
from rekuest_core.vocabulary.view import BlokView
from rekuest_core.vocabulary.walk import BIND_PROP, Where, action_key_for, walk_tree

if TYPE_CHECKING:
    from rekuest_core.vocabulary.check import BlokScope

ITEM_KEY = "..."
BIND_INFERRED = ("STRING", "INT", "FLOAT", "BOOL")
"""The bind kinds a local takes from its control; a LIST, DICT or ANY control tells too little."""

Typed = dict[str, object]
"""A port type as the fields of a local: ``kind``, and ``identifier``, ``nullable``, ``children``."""


def kind_given(local: BlokLocalInputModel) -> bool:
    """Whether ``local`` says its kind (``ANY`` included), rather than leaving it to the tree."""
    return local.kind is not None


def _scalar_kind(value: object) -> str | None:
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT"
    if isinstance(value, float):
        return "FLOAT"
    if isinstance(value, str):
        return "STRING"
    return None


def default_type(default: object) -> Typed | None:
    """The type a default implies (rule 1), or None when it says nothing (``None``, ``[]``, ``{}``,
    mixed items)."""
    scalar = _scalar_kind(default)
    if scalar is not None:
        return {"kind": scalar}
    items: list[object]
    if isinstance(default, list):
        items = [item for item in default]
    elif isinstance(default, dict):
        items = [item for item in default.values()]
    else:
        items = []
    kinds = {_scalar_kind(item) for item in items}
    if len(kinds) != 1 or None in kinds:
        return None
    (item_kind,) = kinds
    return {"kind": "LIST" if isinstance(default, list) else "DICT", "children": [{"key": ITEM_KEY, "kind": item_kind}]}


def match_type(match: PortMatchInputModel) -> Typed | None:
    """The type an arg match says (a kind, recursively for its children), or None when it says
    too little to be a port."""
    if match.kind is None:
        return None
    typed: Typed = {"kind": match.kind.value, "identifier": match.identifier, "nullable": bool(match.nullable)}
    if match.children:
        children: list[Typed] = []
        for child in match.children:
            child_type = match_type(child)
            if child_type is None or child.key is None:
                return None
            children.append({**child_type, "key": child.key})
        typed["children"] = children
    return typed


def typed_local(local: BlokLocalInputModel, typed: Mapping[str, object]) -> BlokLocalInputModel | None:
    """``local`` with the type ``typed`` filling what it does not say: every field the local
    sets (its key, default, choices, nullable, widget, ...) wins over the inferred one. None when
    that makes no valid local (a QUANTITY without a unit, an ENUM without choices, ...)."""
    own = local.model_dump(mode="json", exclude={"kind"}, exclude_unset=True)
    try:
        return BlokLocalInputModel.model_validate({**typed, **own})
    except ValidationError:
        return None


def any_local(local: BlokLocalInputModel) -> BlokLocalInputModel:
    """``local`` resolved to ``ANY``."""
    return BlokLocalInputModel.model_validate({**local.model_dump(mode="json", exclude_none=True, exclude={"kind"}), "kind": enums.LocalKind.ANY.value})


@dataclass
class _Uses:
    """Where a tree uses its locals whole (task arguments, binds)."""

    locals: frozenset[str]
    dependencies: Mapping[str, AgentDependencyInputModel]
    view: BlokView | None
    arguments: dict[str, list[Typed]] = field(default_factory=dict[str, list[Typed]])
    binds: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def visit_component(self, node: ComponentNodeInputModel, ancestors: tuple[ComponentNodeInputModel, ...], scope: Mapping[str, None]) -> None:
        spec = self.view.component(node.component) if self.view is not None else None
        bind = next((prop for prop in spec.props if prop.key == BIND_PROP), None) if spec is not None else None
        for prop in node.props or ():
            path = prop.dynamic_value.path if prop.key == BIND_PROP and prop.dynamic_value is not None else None
            if bind is not None and path is not None and path in self.locals and path not in scope:
                self.binds.setdefault(path, []).append(bind.kind.value)

    def visit_path(self, path: str, where: Where, scope: Mapping[str, None]) -> None:
        return None

    def visit_agent_call(self, call: AgentProbeInputModel, where: Where, scope: Mapping[str, None], into: str | None) -> None:
        action = action_key_for(call)
        dependency = self.dependencies.get(call.dependency)
        demand = next((each.demand for each in (dependency.action_dependencies or ()) if each.key == action), None) if dependency is not None else None
        matches = {match.key: match for match in (demand.arg_matches or ()) if match.key is not None} if demand is not None else {}
        for argument in call.arguments or ():
            path = argument.value_path
            if path is None or path not in self.locals or path in scope or argument.key is None:
                continue
            match = matches.get(argument.key)
            typed = match_type(match) if match is not None else None
            if typed is not None:
                self.arguments.setdefault(path, []).append(typed)

    def visit_util_call(self, call: UtilCallInputModel, where: Where, scope: Mapping[str, None], top: bool) -> None:
        return None

    def bind_let(self, name: str, items: ComponentPropInputModel | None, where: Where, scope: Mapping[str, None]) -> None:
        return None

    def bind_into(self, name: str, call: AgentProbeInputModel) -> None:
        return None


def _uses(tree: ComponentNodeInputModel, scope: "BlokScope", view: BlokView | None) -> _Uses:
    uses = _Uses(
        locals=frozenset(local.key for local in scope.locals),
        dependencies={dependency.key: dependency for dependency in scope.dependencies},
        view=view,
    )
    walk_tree(tree, uses)
    return uses


def resolve_local(local: BlokLocalInputModel, arguments: Sequence[Mapping[str, object]], binds: Sequence[str]) -> BlokLocalInputModel:
    """``local`` with its kind resolved by the rules of this module (a given kind is kept)."""
    if kind_given(local):
        return local
    candidates: list[Mapping[str, object]] = []
    from_default = default_type(local.default)
    if from_default is not None:
        candidates.append(from_default)
    candidates.extend(arguments)
    candidates.extend({"kind": kind} for kind in binds if kind in BIND_INFERRED)
    for candidate in candidates:
        resolved = typed_local(local, candidate)
        if resolved is not None:
            return resolved
    return any_local(local)


def resolve_scope(tree: ComponentNodeInputModel, scope: "BlokScope", view: BlokView | None = None) -> "BlokScope":
    """``scope`` with what ``tree`` tells: each local's kind (see the module). Resolving a
    resolved scope changes nothing."""
    uses = _uses(tree, scope, view)
    locals_ = tuple(resolve_local(local, uses.arguments.get(local.key, ()), uses.binds.get(local.key, ())) for local in scope.locals)
    return replace(scope, locals=locals_)


__all__ = ["default_type", "kind_given", "match_type", "resolve_local", "resolve_scope", "typed_local"]

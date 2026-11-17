"""Resolve what a blok leaves to its tree: the kinds of its ``<local>`` declarations.

The server's port of arkitekt-spec's ``declare/blok/infer.py`` (``resolve_locals``), on
rekuest_core's models. The spec, the server and takt infer the same way, so a source blok sent
without kinds resolves the same everywhere; the spec corpus (``bsx_v2.json``, the
``language.resolved`` of its checked cases) holds them to it. A blok stores its tree as written
and, beside it, its locals as resolved (``resolved_locals``: every ``<local>`` by its node, with a
kind). Dependencies are never inferred: every
``deps.<key>`` a blok reads, whatever its kind, must be declared (``deps.unknown``).

**A local's kind**, when it is left out (a given kind, ``ANY`` included, is kept), is the first of:

0. the argument it borrows with ``like="self.<action>.<arg>"`` / ``"deps.<key>.<action>.<arg>"``,
   as the mount's dependency demands type it (resolved only where the blok is mounted: not
   mounted, it borrows nothing and says nothing of it);
1. its ``default``: a bool ``BOOL``, an integer ``INT``, a number ``FLOAT``, a string ``STRING``,
   a non-empty list of one scalar kind a ``LIST`` of it, a non-empty object of one scalar kind a
   ``DICT`` of it (the item port keyed ``...``);
2. the port of the task argument it is passed to whole (``self.move(to: position)``), at the
   first such call in tree order whose demand's arg match has a kind (every child too) and makes
   a valid local;
3. the bind kind of the first control bound to it whole (``<Slider bind={x}>``: ``FLOAT``), when
   that is ``STRING``, ``INT``, ``FLOAT`` or ``BOOL``;
4. else ``ANY``: an untyped value, never an error.

Only uses in the local's scope (its parent's subtree) count. Every argument site comes before
every bind site, whatever their place in the tree. Every field
the local sets wins over the inferred type, which only fills what it leaves out; a candidate that
makes no valid local with them is skipped. The other call sites and binds are then checked against
the kind found (``local.arg_kind``, ``local.bind_kind``), which ``ANY`` skips.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

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
from rekuest_core.vocabulary.declared import LOCAL_COMPONENT, declared_local, local_like, local_problem
from rekuest_core.vocabulary.walk import Where, action_key_for, iter_nodes, walk_tree

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


@dataclass(frozen=True)
class ResolvedLocal:
    """A ``<local>`` with its kind resolved, and why its ``like`` borrows nothing (None when it
    borrows, has none, or the blok is not mounted)."""

    local: BlokLocalInputModel
    like_problem: str | None = None


@dataclass
class _Uses:
    """Where a tree uses its locals whole (task arguments, binds), per local node id.

    A local's binding is its node id; a ``let`` or ``into`` name is bound to None, so a path
    whose root is bound to a string reads a local, and that local only (scoping is the walker's).
    """

    dependencies: Mapping[str, AgentDependencyInputModel]
    arguments: dict[str, list[Typed]] = field(default_factory=dict[str, list[Typed]])
    binds: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def visit_component(self, node: ComponentNodeInputModel, ancestors: tuple[ComponentNodeInputModel, ...], scope: Mapping[str, str | None]) -> None:
        # What a control writes through its `bind` is its catalog's to say, and a server reads none:
        # a local only a bind would type stays ANY here, and the renderer types it.
        return None

    def visit_path(self, path: str, where: Where, scope: Mapping[str, str | None]) -> None:
        return None

    def visit_agent_call(self, call: AgentProbeInputModel, where: Where, scope: Mapping[str, str | None], into: str | None) -> None:
        action = action_key_for(call)
        for argument in call.arguments or ():
            path = argument.value_path
            local = scope.get(path) if path is not None else None
            if local is None or argument.key is None:
                continue
            typed = argument_type(call.dependency, action, argument.key, self.dependencies)
            if typed is not None:
                self.arguments.setdefault(local, []).append(typed)

    def visit_util_call(self, call: UtilCallInputModel, where: Where, scope: Mapping[str, str | None], top: bool) -> None:
        return None

    def bind_let(self, name: str, items: ComponentPropInputModel | None, where: Where, scope: Mapping[str, str | None]) -> None:
        return None

    def bind_index(self, name: str, where: Where, scope: Mapping[str, str | None]) -> None:
        return None

    def bind_into(self, name: str, call: AgentProbeInputModel) -> None:
        return None

    def bind_local(self, name: str, node: ComponentNodeInputModel, scope: Mapping[str, str | None]) -> str:
        return node.id


def argument_type(dependency: str, action: str, argument: str, dependencies: Mapping[str, AgentDependencyInputModel]) -> Typed | None:
    """The type of an action's argument, as the server knows it: the arg match of a dependency's
    demand; None when it does not tell (the server knows no action's full arg ports)."""
    declared = dependencies.get(dependency)
    demand = next((each.demand for each in (declared.action_dependencies or ()) if each.key == action), None) if declared is not None else None
    match = next((each for each in (demand.arg_matches or ()) if each.key == argument), None) if demand is not None else None
    return match_type(match) if match is not None else None


def borrowed_type(like: str, dependencies: Mapping[str, AgentDependencyInputModel]) -> tuple[Typed | None, str | None]:
    """What ``like="self.<action>.<arg>"`` / ``"deps.<key>.<action>.<arg>"`` borrows: the
    argument's type as the dependency's demand says it, or why it borrows nothing."""
    parts = like.split(".")
    if parts[:1] == ["self"] and len(parts) == 3:
        dependency, action, argument = "self", parts[1], parts[2]
    elif parts[:1] == ["deps"] and len(parts) == 4:
        dependency, action, argument = parts[1], parts[2], parts[3]
    else:
        return None, "is no argument: write self.<action>.<arg> or deps.<key>.<action>.<arg>"
    if dependency not in dependencies and dependency != "self":
        return None, f"names the dependency {dependency!r}, which the mount does not have ({', '.join(sorted(dependencies)) or 'none'})"
    declared = dependencies.get(dependency)
    actions = {each.key: each for each in (declared.action_dependencies or ())} if declared is not None else {}
    if action not in actions:
        return None, f"names the action {action!r}, which {dependency} does not have ({', '.join(sorted(actions)) or 'none'})"
    typed = argument_type(dependency, action, argument, dependencies)
    if typed is None:
        return None, f"names the argument {argument!r}, which {dependency}.{action} does not have, or does not type"
    return typed, None


def resolve_local(
    local: BlokLocalInputModel,
    arguments: Sequence[Mapping[str, object]],
    binds: Sequence[str],
    borrowed: Mapping[str, object] | None = None,
) -> BlokLocalInputModel:
    """``local`` with its kind resolved by the rules of this module (a given kind is kept)."""
    if kind_given(local):
        return local
    candidates: list[Mapping[str, object]] = []
    if borrowed is not None:
        candidates.append(borrowed)
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


def resolve_locals(
    tree: ComponentNodeInputModel,
    dependencies: Sequence[AgentDependencyInputModel] | None = None,
) -> dict[str, ResolvedLocal]:
    """Every readable ``<local>`` of ``tree`` with its kind resolved (see the module), by node id.

    ``dependencies`` None is a blok not mounted: a ``like`` then borrows nothing, and says nothing
    of it either. Resolving resolved locals changes nothing.
    """
    by_key = {dependency.key: dependency for dependency in dependencies or ()}
    uses = _Uses(dependencies=by_key)
    walk_tree(tree, uses)
    resolved: dict[str, ResolvedLocal] = {}
    for node in iter_nodes(tree):
        if node.component != LOCAL_COMPONENT or local_problem(node) is not None:
            continue
        local = declared_local(node)
        like = local_like(node)
        borrowed, problem = (None, None)
        if like is not None and dependencies is not None:
            borrowed, problem = borrowed_type(like, by_key)
        resolved[node.id] = ResolvedLocal(
            local=resolve_local(local, uses.arguments.get(node.id, ()), uses.binds.get(node.id, ()), borrowed),
            like_problem=problem,
        )
    return resolved


__all__ = [
    "ResolvedLocal",
    "argument_type",
    "borrowed_type",
    "default_type",
    "kind_given",
    "match_type",
    "resolve_local",
    "resolve_locals",
    "typed_local",
]

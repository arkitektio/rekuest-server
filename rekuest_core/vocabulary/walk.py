"""Walking a v2 blok tree: every value it reads, every call it makes, with what is in scope.

The server's port of arkitekt-spec's ``declare/blok/walk.py``. The checker (:mod:`.check`) and
the inference (:mod:`.infer`) walk the same
tree under the same scoping rules, so the traversal lives here and they supply a visitor:

- ``into`` names are in scope in the whole tree (the button that starts a run and the panel
  that shows it are rarely nested), each written by one call site;
- a ``foreach``'s ``let`` is in scope in its children and in its own ``key``, not in its
  ``items``;
- ``event`` is in scope in a callback prop (one that holds a task call or an effect, a
  query's refetch or fetch_more included), ``error`` inside the ``<error>`` slot of a
  ``<suspense>``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from rekuest_core.inputs.models import (
    ActionArgumentInputModel,
    AgentProbeInputModel,
    ComponentNodeInputModel,
    ComponentPropInputModel,
    UtilCallInputModel,
)
from rekuest_core.vocabulary.vocabulary import load_vocabulary

FOREACH_COMPONENT = "foreach"
FOREACH_LET_PROP = "let"
FOREACH_ITEMS_PROP = "items"
FOREACH_KEY_PROP = "key"
IF_COMPONENT = "if"
ELSE_COMPONENT = "else"
SUSPENSE_COMPONENT = "suspense"
ERROR_SLOT = "error"
WHEN_PROP = "when"
BIND_PROP = "bind"

Binding = TypeVar("Binding")


@dataclass(frozen=True)
class Where:
    """Where a value sits: the node, the prop, and which scoped roots are in force there."""

    node: ComponentNodeInputModel
    key: str
    callback: bool
    in_error: bool

    @property
    def text(self) -> str:
        """The place, for a message: ``onClick of <Button> (Card[0]/Button[0])``."""
        return f"{self.key} of <{self.node.component}> ({self.node.id})"


def node_context(node: ComponentNodeInputModel) -> str:
    """Render a node as ``<Component> (id)`` for messages."""
    return f"<{node.component}> ({node.id})"


def action_key_for(call: AgentProbeInputModel) -> str:
    """The action a task call names."""
    return call.operation.split(".")[-1]


def is_effect(call: UtilCallInputModel) -> bool:
    """Whether ``call`` is an effect (``ui.copy``, ``task.answer``, ``query.refetch``,
    ``query.fetch_more``), not a pure function."""
    function = load_vocabulary().function(call.operation)
    return function is not None and function.effect


def is_callback_value(prop: ComponentPropInputModel) -> bool:
    """Whether a prop holds a callback's one thing: a task call or an effect."""
    return prop.agent_call is not None or (prop.util_call is not None and is_effect(prop.util_call))


def into_sites(node: ComponentNodeInputModel) -> dict[str, list[tuple[AgentProbeInputModel, str]]]:
    """Every name a task call writes its record to (``... into move``), with each call site."""
    found: dict[str, list[tuple[AgentProbeInputModel, str]]] = {}

    def visit(current: ComponentNodeInputModel) -> None:
        for prop in current.props or ():
            if prop.agent_call is not None and prop.declares_value:
                found.setdefault(prop.declares_value, []).append(
                    (prop.agent_call, f"{prop.key} of {node_context(current)}")
                )
        for child in current.children or ():
            visit(child)

    visit(node)
    return found


def foreach_let(node: ComponentNodeInputModel) -> str | None:
    """The loop variable a ``foreach`` declares."""
    if node.component != FOREACH_COMPONENT:
        return None
    return next(
        (prop.declares_value for prop in node.props or () if prop.key == FOREACH_LET_PROP and prop.declares_value),
        None,
    )


def iter_nodes(node: ComponentNodeInputModel) -> list[ComponentNodeInputModel]:
    """``node`` and every node below it, depth first."""
    nodes = [node]
    for child in node.children or ():
        nodes.extend(iter_nodes(child))
    return nodes


class TreeVisitor(Protocol[Binding]):
    """What :func:`walk_tree` calls. ``scope`` maps ``let`` and ``into`` names to their bindings."""

    def visit_component(
        self, node: ComponentNodeInputModel, ancestors: tuple[ComponentNodeInputModel, ...], scope: Mapping[str, Binding]
    ) -> None:  # pragma: no cover - protocol
        """Once per node, before its props."""
        ...

    def visit_path(self, path: str, where: Where, scope: Mapping[str, Binding]) -> None:  # pragma: no cover - protocol
        """A value read at ``path``."""
        ...

    def visit_agent_call(
        self, call: AgentProbeInputModel, where: Where, scope: Mapping[str, Binding], into: str | None
    ) -> None:  # pragma: no cover - protocol
        """A task call (before its arguments)."""
        ...

    def visit_util_call(
        self, call: UtilCallInputModel, where: Where, scope: Mapping[str, Binding], top: bool
    ) -> None:  # pragma: no cover - protocol
        """A function or effect call (before its arguments); ``top`` when it is the prop's value."""
        ...

    def bind_let(
        self, name: str, items: ComponentPropInputModel | None, where: Where, scope: Mapping[str, Binding]
    ) -> Binding:  # pragma: no cover - protocol
        """What a ``foreach``'s loop variable is bound to, its ``items`` read in ``scope``."""
        ...

    def bind_into(self, name: str, call: AgentProbeInputModel) -> Binding:  # pragma: no cover - protocol
        """What an ``into`` name is bound to: the record of ``call``."""
        ...


def walk_tree(node: ComponentNodeInputModel, visitor: TreeVisitor[Binding]) -> None:
    """Walk ``node`` with ``visitor``: every node, then every value of its props, in scope."""
    scope: dict[str, Binding] = {
        name: visitor.bind_into(name, sites[0][0]) for name, sites in into_sites(node).items()
    }
    _walk_node(node, visitor, scope, (), False)


def _walk_node(
    node: ComponentNodeInputModel,
    visitor: TreeVisitor[Binding],
    scope: dict[str, Binding],
    ancestors: tuple[ComponentNodeInputModel, ...],
    in_error: bool,
) -> None:
    visitor.visit_component(node, ancestors, scope)
    inner = dict(scope)
    let = foreach_let(node)
    if let is not None:
        items = next((prop for prop in node.props or () if prop.key == FOREACH_ITEMS_PROP), None)
        where = Where(node=node, key=FOREACH_ITEMS_PROP, callback=False, in_error=in_error)
        inner[let] = visitor.bind_let(let, items, where, scope)
    for prop in node.props or ():
        # A foreach reads its items outside the loop, its key inside it.
        prop_scope = scope if (let is not None and prop.key == FOREACH_ITEMS_PROP) else inner
        _walk_prop(node, prop, visitor, prop_scope, in_error)
    in_slot = in_error or (
        node.component == ERROR_SLOT and bool(ancestors) and ancestors[-1].component == SUSPENSE_COMPONENT
    )
    for child in node.children or ():
        _walk_node(child, visitor, inner, (*ancestors, node), in_slot)


def _walk_prop(
    node: ComponentNodeInputModel,
    prop: ComponentPropInputModel,
    visitor: TreeVisitor[Binding],
    scope: Mapping[str, Binding],
    in_error: bool,
) -> None:
    where = Where(node=node, key=prop.key, callback=is_callback_value(prop), in_error=in_error)
    if prop.dynamic_value is not None and prop.dynamic_value.path is not None:
        visitor.visit_path(prop.dynamic_value.path, where, scope)
    if prop.agent_call is not None:
        visitor.visit_agent_call(prop.agent_call, where, scope, prop.declares_value)
        for argument in prop.agent_call.arguments or ():
            _walk_argument(argument, visitor, scope, where)
    if prop.util_call is not None:
        _walk_util(prop.util_call, visitor, scope, where, top=True)


def _walk_util(
    call: UtilCallInputModel, visitor: TreeVisitor[Binding], scope: Mapping[str, Binding], where: Where, *, top: bool
) -> None:
    visitor.visit_util_call(call, where, scope, top)
    for argument in call.arguments or ():
        _walk_argument(argument, visitor, scope, where)


def _walk_argument(
    argument: ActionArgumentInputModel, visitor: TreeVisitor[Binding], scope: Mapping[str, Binding], where: Where
) -> None:
    if argument.value_path is not None:
        visitor.visit_path(argument.value_path, where, scope)
    if argument.agent_call is not None:
        visitor.visit_agent_call(argument.agent_call, where, scope, None)
        for nested in argument.agent_call.arguments or ():
            _walk_argument(nested, visitor, scope, where)
    if argument.util_call is not None:
        _walk_util(argument.util_call, visitor, scope, where, top=False)
    for nested in (*(argument.value_list or ()), *(argument.value_dict or ())):
        _walk_argument(nested, visitor, scope, where)


def argument_paths(arguments: Sequence[ActionArgumentInputModel]) -> list[str]:
    """Every path a list of arguments reads (a query's variables), depth first."""
    paths: list[str] = []

    def visit(argument: ActionArgumentInputModel) -> None:
        if argument.value_path is not None:
            paths.append(argument.value_path)
        for call in (argument.util_call, argument.agent_call):
            for nested in (call.arguments or ()) if call is not None else ():
                visit(nested)
        for nested in (*(argument.value_list or ()), *(argument.value_dict or ())):
            visit(nested)

    for argument in arguments:
        visit(argument)
    return paths


__all__ = [
    "BIND_PROP",
    "ELSE_COMPONENT",
    "ERROR_SLOT",
    "FOREACH_COMPONENT",
    "FOREACH_ITEMS_PROP",
    "FOREACH_KEY_PROP",
    "FOREACH_LET_PROP",
    "IF_COMPONENT",
    "SUSPENSE_COMPONENT",
    "WHEN_PROP",
    "TreeVisitor",
    "Where",
    "action_key_for",
    "argument_paths",
    "foreach_let",
    "into_sites",
    "is_callback_value",
    "is_effect",
    "iter_nodes",
    "node_context",
    "walk_tree",
]

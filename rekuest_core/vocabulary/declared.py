"""What a blok says of itself in its tree: its ``<Blok>`` header and its declarations.

The server's port of arkitekt-spec's ``blok/declared.py``, on rekuest_core's models. Every blok is
one ``<Blok kind=... context=...>`` element (``vocabulary.json`` ``blok``):

- the header (:func:`blok_header`): its kind, its context, the catalog it is written for and the
  host's chrome, all literals on the root;
- its declarations, each a node of its own that draws nothing: ``<local>`` anywhere (first among
  its parent's children, in scope in its parent's subtree), ``<query>`` and ``<subscription>``
  as children of the ``<Blok>``, each with ``<var>`` children for its variables.

Nothing here checks a rule beyond what makes a declaration readable at all; the checker
(:mod:`.check`) says the rest.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from pydantic import ValidationError

from rekuest_core.inputs.models import (
    ActionArgumentInputModel,
    BlokLocalInputModel,
    BlokQueryInputModel,
    BlokSubscriptionInputModel,
    ComponentNodeInputModel,
    ComponentPropInputModel,
)
from rekuest_core.vocabulary.vocabulary import BlokError, Finding, load_vocabulary

BLOK_COMPONENT = "Blok"
LOCAL_COMPONENT = "local"
QUERY_COMPONENT = "query"
SUBSCRIPTION_COMPONENT = "subscription"
VAR_COMPONENT = "var"
TEXT_NODE = "#text"
"""The component a run of text is stored as."""
DEPENDENCIES_COMPONENT = "dependencies"
DECLARATION_COMPONENTS = frozenset({LOCAL_COMPONENT, QUERY_COMPONENT, SUBSCRIPTION_COMPONENT, VAR_COMPONENT, DEPENDENCIES_COMPONENT})
REFERENCE_SEPARATOR = "__"
"""How a read through a declared ``<dependencies key="cells" />`` is stored: ``cells.microscope``
is the dependency ``cells__microscope`` of the blok (``deps.cells__microscope``), one segment."""
PICK_VERB = "pick"
"""``cells.pick(microscope: id)``: the one call a reference itself takes."""
PICK_FUNCTION = "deps.pick"
"""What it is stored as."""
"""The components that declare what a blok owns and draw nothing."""
NAME_PROP = "name"
"""The name a local, a query or a subscription is read by."""
LIKE_PROP = "like"
"""A local's borrowed port: ``self.<action>.<arg>`` or ``deps.<key>.<action>.<arg>``."""
KEY_PROP = "key"
VALUE_PROP = "value"
KIND_ATTRIBUTE = "kind"
CONTEXT_ATTRIBUTE = "context"
CATALOG_ATTRIBUTE = "catalog"
CHROME_ATTRIBUTE = "chrome"
_LOCAL_FIELDS = frozenset(BlokLocalInputModel.model_fields) - {"key"}
"""What a ``<local>`` may say beside its name: a port's fields and ``persist``."""
_DECLARED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FIELD_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_GRAPHQL_COMMENT = re.compile(r"#[^\n]*")


@dataclass(frozen=True)
class BlokHeader:
    """What a ``<Blok>`` says of itself."""

    kind: str
    """``agent``, ``custom``, ``task``, ``action`` or ``assign``."""
    context: str
    """``PANEL``, ``SCENE`` or ``LOGO`` (a ``ComponentContext`` value)."""
    catalog: str | None
    chrome: str | None
    node: str
    """The root's id."""


def _static(node: ComponentNodeInputModel, key: str) -> ComponentPropInputModel | None:
    return next((prop for prop in node.props or () if prop.key == key), None)


def _is_literal(prop: ComponentPropInputModel) -> bool:
    return prop.dynamic_value is None and prop.util_call is None and prop.agent_call is None


def header_problem(tree: ComponentNodeInputModel) -> Finding | None:
    """Why ``tree`` is no readable ``<Blok>``, or None: ``blok.root`` or ``blok.attribute``."""
    vocabulary = load_vocabulary()
    if tree.component != vocabulary.blok.component:
        return Finding.of("blok.root", tree.id, component=tree.component)
    kinds = "|".join(vocabulary.kinds)
    for key in (KIND_ATTRIBUTE, CONTEXT_ATTRIBUTE):
        prop = _static(tree, key)
        if prop is None:
            return Finding.of("blok.attribute", tree.id, problem=f'needs {key}="..."', kinds=kinds)
        if not _is_literal(prop) or not isinstance(prop.static_value, str):
            return Finding.of("blok.attribute", tree.id, problem=f"takes {key} as a literal string", kinds=kinds)
    kind = _static(tree, KIND_ATTRIBUTE)
    assert kind is not None
    if kind.static_value not in vocabulary.kinds:
        return Finding.of("blok.attribute", tree.id, problem=f"has no kind {kind.static_value!r}", kinds=kinds)
    context = _static(tree, CONTEXT_ATTRIBUTE)
    assert context is not None
    if context.static_value not in vocabulary.blok.contexts:
        return Finding.of(
            "blok.attribute",
            tree.id,
            problem=f"has no context {context.static_value!r} ({', '.join(vocabulary.blok.contexts)})",
            kinds=kinds,
        )
    for key in (CATALOG_ATTRIBUTE, CHROME_ATTRIBUTE):
        prop = _static(tree, key)
        if prop is not None and (not _is_literal(prop) or not isinstance(prop.static_value, str)):
            return Finding.of("blok.attribute", tree.id, problem=f"takes {key} as a literal string", kinds=kinds)
    return None


def blok_header(tree: ComponentNodeInputModel) -> BlokHeader:
    """The header of a blok: what its ``<Blok>`` says.

    Raises:
        BlokError: ``blok.root`` if the root is no ``<Blok>``, ``blok.attribute`` if its kind or
            context is missing, no literal or unknown.
    """
    problem = header_problem(tree)
    if problem is not None:
        raise BlokError.from_finding(problem)
    vocabulary = load_vocabulary()

    def text(key: str) -> str | None:
        prop = _static(tree, key)
        return prop.static_value if prop is not None and isinstance(prop.static_value, str) else None

    return BlokHeader(
        kind=text(KIND_ATTRIBUTE) or "",
        context=vocabulary.blok.contexts[text(CONTEXT_ATTRIBUTE) or ""],
        catalog=text(CATALOG_ATTRIBUTE),
        chrome=text(CHROME_ATTRIBUTE),
        node=tree.id,
    )


def is_declaration(node: ComponentNodeInputModel) -> bool:
    """Whether ``node`` declares something (a local, a query, ...) rather than drawing."""
    return node.component in DECLARATION_COMPONENTS


def content(node: ComponentNodeInputModel) -> tuple[ComponentNodeInputModel, ...]:
    """The children of ``node`` that draw: its declarations left out."""
    return tuple(child for child in node.children or () if not is_declaration(child))


def local_name(node: ComponentNodeInputModel) -> str | None:
    """The name a ``<local>`` declares, or None when it has none."""
    prop = _static(node, NAME_PROP)
    return prop.static_value if prop is not None and isinstance(prop.static_value, str) else None


def local_like(node: ComponentNodeInputModel) -> str | None:
    """The argument a ``<local>`` borrows its port from (``self.move.to``), or None."""
    prop = _static(node, LIKE_PROP)
    return prop.static_value if prop is not None and isinstance(prop.static_value, str) else None


def local_problem(node: ComponentNodeInputModel) -> Finding | None:
    """Why a ``<local>`` declares nothing readable, or None: ``local.attribute``."""
    name = local_name(node) or "?"
    if local_name(node) is None:
        return Finding.of("local.attribute", node.id, name=name, key="name", problem='is missing: write name="x"')
    for prop in node.props or ():
        if prop.key in (NAME_PROP, LIKE_PROP):
            continue
        if prop.key not in _LOCAL_FIELDS:
            return Finding.of(
                "local.attribute",
                node.id,
                name=name,
                key=prop.key,
                problem=f"is no field of a local ({', '.join(sorted(_LOCAL_FIELDS))}, like)",
            )
        if not _is_literal(prop):
            return Finding.of("local.attribute", node.id, name=name, key=prop.key, problem="is not a literal")
    if node.children:
        return Finding.of("local.attribute", node.id, name=name, key="children", problem="are not elements: a local holds nothing")
    try:
        declared_local(node)
    except BlokError as error:
        return Finding.of("local.attribute", node.id, name=name, key="its port", problem=f"is no valid port ({error})")
    return None


def declared_local(node: ComponentNodeInputModel) -> BlokLocalInputModel:
    """A ``<local>`` as the port it declares, its kind left out unless it gives one (``like`` is
    resolved where the blok is mounted).

    Raises:
        BlokError: ``local.attribute`` if it is no valid local.
    """
    name = local_name(node)
    if name is None:
        raise BlokError("local.attribute", name="?", key="name", problem='is missing: write name="x"')
    fields: dict[str, object] = {"key": name}
    for prop in node.props or ():
        if prop.key in (NAME_PROP, LIKE_PROP) or prop.key not in _LOCAL_FIELDS:
            continue
        if prop.static_value is not None:
            fields[prop.key] = prop.static_value
    try:
        return BlokLocalInputModel.model_validate(fields)
    except ValidationError as error:
        raise BlokError("local.attribute", name=name, key="its port", problem=f"is no valid port: {error.errors()[0]['msg']}") from None


def local_node(local: BlokLocalInputModel, node_id: str) -> ComponentNodeInputModel:
    """``local`` as the ``<local>`` node that declares it: every field it sets a literal prop."""
    fields = local.model_dump(mode="json", exclude_none=True, exclude={"key"})
    props = [ComponentPropInputModel(key=NAME_PROP, static_value=local.key, declares_value=local.key)]
    props.extend(
        ComponentPropInputModel(key=key, static_value=value)
        for key, value in fields.items()
        if value not in ((), [], False) or key == "default"
    )
    return ComponentNodeInputModel(id=node_id, component=LOCAL_COMPONENT, props=props)


def iter_with_parents(
    tree: ComponentNodeInputModel,
) -> Iterator[tuple[ComponentNodeInputModel, tuple[ComponentNodeInputModel, ...]]]:
    """Every node of ``tree`` with its ancestors, depth first."""

    def visit(
        node: ComponentNodeInputModel, ancestors: tuple[ComponentNodeInputModel, ...]
    ) -> Iterator[tuple[ComponentNodeInputModel, tuple[ComponentNodeInputModel, ...]]]:
        yield node, ancestors
        for child in node.children or ():
            yield from visit(child, (*ancestors, node))

    return visit(tree, ())


def declared_locals(tree: ComponentNodeInputModel) -> dict[str, BlokLocalInputModel]:
    """Every readable ``<local>`` of ``tree``, by its node id, in tree order (kinds as written)."""
    found: dict[str, BlokLocalInputModel] = {}
    for node, _ in iter_with_parents(tree):
        if node.component == LOCAL_COMPONENT and local_problem(node) is None:
            found[node.id] = declared_local(node)
    return found


def var_argument(node: ComponentNodeInputModel) -> ActionArgumentInputModel | None:
    """A ``<var key value>`` as the argument it is stored as, or None when it is none."""
    key = _static(node, KEY_PROP)
    if key is None or not isinstance(key.static_value, str):
        return None
    value = _static(node, VALUE_PROP)
    if value is None or (value.static_value is None and value.dynamic_value is None and value.util_call is None):
        return ActionArgumentInputModel(key=key.static_value)
    if value.agent_call is not None:
        return None
    if value.dynamic_value is not None and value.dynamic_value.path is not None:
        return ActionArgumentInputModel(key=key.static_value, value_path=value.dynamic_value.path)
    if value.util_call is not None:
        return ActionArgumentInputModel(key=key.static_value, util_call=value.util_call)
    return ActionArgumentInputModel(key=key.static_value, value_literal=value.static_value)


def declaration_problem(node: ComponentNodeInputModel) -> Finding | None:
    """Why a ``<query>`` or ``<subscription>`` declares nothing readable, or None: ``query.declaration``."""
    for key in (NAME_PROP, "service", "document"):
        prop = _static(node, key)
        if prop is None or not _is_literal(prop) or not isinstance(prop.static_value, str) or not prop.static_value:
            return Finding.of("query.declaration", node.id, component=node.component, problem=f'needs {key}="..." as a literal string')
    for prop in node.props or ():
        if prop.key not in (NAME_PROP, "service", "document") and not _is_literal(prop):
            return Finding.of("query.declaration", node.id, component=node.component, problem=f"takes {prop.key} as a literal")
    for child in node.children or ():
        if child.component != VAR_COMPONENT:
            return Finding.of("query.declaration", child.id, component=node.component, problem=f"holds only <var> children, not <{child.component}>")
        if var_argument(child) is None:
            return Finding.of("query.declaration", child.id, component=node.component, problem='has a <var> without key="..." or with a task call as its value')
    try:
        _declared(node)
    except BlokError as error:
        return Finding.of("query.declaration", node.id, component=node.component, problem=str(error))
    return None


def _declared(node: ComponentNodeInputModel) -> BlokQueryInputModel | BlokSubscriptionInputModel:
    fields: dict[str, object] = {}
    for prop in node.props or ():
        if prop.static_value is not None:
            fields["key" if prop.key == NAME_PROP else prop.key] = prop.static_value
    fields["variables"] = [argument for child in node.children or () if (argument := var_argument(child)) is not None]
    model = BlokQueryInputModel if node.component == QUERY_COMPONENT else BlokSubscriptionInputModel
    try:
        return model.model_validate(fields)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise BlokError("query.declaration", component=node.component, problem=f"{where}: {first['msg']}") from None


@dataclass(frozen=True)
class DeclaredDependencies:
    """A ``<dependencies key="cells" like="self.<action>" />`` of an agent blok: the
    dependencies of one of the app's own actions, each read and called as
    ``deps.<key>.<dependency>`` and handed to a run of that action with ``with <key>``."""

    key: str
    node: str
    like: str | None
    """As written; None when it is missing or no literal."""

    @property
    def target(self) -> tuple[str, str] | None:
        """Whose action it names: ``("self", action)`` for ``self.<action>``, ``(key, action)``
        for a custom blok's ``agents.<key>.<action>`` (stored ``deps.<key>.<action>``)."""
        parts = self.like.split(".") if self.like else []
        if len(parts) == 2 and parts[0] == "self" and parts[1]:
            return "self", parts[1]
        if len(parts) == 3 and parts[0] == "deps" and parts[1] and parts[1] != "self" and parts[2]:
            return parts[1], parts[2]
        return None

    @property
    def owner(self) -> str | None:
        """The dependency whose action it names: ``self``, or a custom blok's bound agent."""
        return self.target[0] if self.target else None

    @property
    def action(self) -> str | None:
        """The action ``like`` names."""
        return self.target[1] if self.target else None


def declared_dependencies(tree: ComponentNodeInputModel) -> tuple[DeclaredDependencies, ...]:
    """The keyed ``<dependencies>`` children of a blok's ``<Blok>``, in order."""
    declared: list[DeclaredDependencies] = []
    for child in tree.children or ():
        if child.component != DEPENDENCIES_COMPONENT:
            continue
        literals = {prop.key: prop.static_value for prop in child.props or ()}
        key, like = literals.get(KEY_PROP), literals.get(LIKE_PROP)
        if isinstance(key, str) and key:
            declared.append(DeclaredDependencies(key=key, node=child.id, like=like if isinstance(like, str) else None))
    return tuple(declared)


def declared_queries(tree: ComponentNodeInputModel) -> tuple[BlokQueryInputModel, ...]:
    """The readable ``<query>`` children of a blok's ``<Blok>``, in order."""
    return tuple(
        query
        for child in tree.children or ()
        if child.component == QUERY_COMPONENT and declaration_problem(child) is None
        for query in (_declared(child),)
        if isinstance(query, BlokQueryInputModel)
    )


def declared_subscriptions(tree: ComponentNodeInputModel) -> tuple[BlokSubscriptionInputModel, ...]:
    """The readable ``<subscription>`` children of a blok's ``<Blok>``, in order."""
    return tuple(
        subscription
        for child in tree.children or ()
        if child.component == SUBSCRIPTION_COMPONENT and declaration_problem(child) is None
        for subscription in (_declared(child),)
        if isinstance(subscription, BlokSubscriptionInputModel)
    )


def operation_keyword(document: str) -> str:
    """The kind of a GraphQL document's first operation: ``query`` (also for ``{ ... }``),
    ``mutation``, ``subscription``, or whatever word it starts with."""
    text = _GRAPHQL_COMMENT.sub("", document).lstrip()
    if text.startswith("{"):
        return "query"
    match = re.match(r"[A-Za-z_]+", text)
    return match.group(0) if match else ""


def declared_name_problem(name: str) -> bool:
    """Whether ``name`` is no name a local, a query or a subscription may have."""
    return _DECLARED_NAME.fullmatch(name) is None


def field_path_problem(path: str) -> bool:
    """Whether ``path`` is no field path into a query's data (``positions``, ``images.items``)."""
    return _FIELD_PATH.fullmatch(path) is None


__all__ = [
    "BLOK_COMPONENT",
    "DECLARATION_COMPONENTS",
    "LIKE_PROP",
    "LOCAL_COMPONENT",
    "NAME_PROP",
    "QUERY_COMPONENT",
    "SUBSCRIPTION_COMPONENT",
    "TEXT_NODE",
    "VAR_COMPONENT",
    "BlokHeader",
    "blok_header",
    "content",
    "declaration_problem",
    "declared_local",
    "declared_locals",
    "declared_name_problem",
    "declared_queries",
    "declared_subscriptions",
    "field_path_problem",
    "header_problem",
    "is_declaration",
    "iter_with_parents",
    "local_like",
    "local_name",
    "local_node",
    "local_problem",
    "operation_keyword",
    "var_argument",
]

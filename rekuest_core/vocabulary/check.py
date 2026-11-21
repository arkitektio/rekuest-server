"""Check a v2 blok tree against what its blok declares and its kind: the language alone.

A server never holds a blok to a catalog. What a component takes, where it may sit and which
catalog it is of is checked by whoever draws it; no catalog is read here.


The server's port of arkitekt-spec's ``declare/blok/validate.py`` and ``walk.py``, on rekuest_core's
models: every rule is a row of the vocabulary's diagnostic table and is driven by its data (the
one reserved list, the per-kind root matrix, the record shapes, the function signatures and the
theme). The spec corpus (``bsx_v2.json``, its ``checked`` cases) holds the two to the same codes.

- **The blok.** Its root is one ``<Blok kind context>``; the kind is drawn in the context, a
  scene holds one ``Scene`` or ``Space`` and a logo one ``Logo`` beside its declarations, and
  ``chrome`` is a panel's or a scene's. Mounted somewhere (``BlokScope.kinds``), the kind is
  the one the mount takes (``kind.placement``).
- **Mounted or not.** ``BlokScope.dependencies`` None is a blok checked on its own: what its
  mount provides (``deps.<key>``, ``Placed``/``Route`` keys, a local's ``like``) is taken as
  written. Mounted (every registration), all of it is checked.
- **Roots.** A path starts at a root its kind reads (``self``, ``deps``, ``args``, ``task``,
  ``queries``, ``subscriptions``, ``viewer``, ``then``), at a local in scope, a ``let`` or an
  ``into`` name; ``event`` only in a callback, ``error`` only in an ``<error>`` slot.
- **Declarations.** A ``<local>`` comes first among its parent's children and is in scope in its
  parent's subtree; it shadows nothing. Queries and subscriptions are children of the ``<Blok>``,
  each name declared once, a query's document a ``query``, a subscription's a ``subscription``.
- **Agents.** ``self`` / ``deps.<key>``: ``.<state>.<field>``, ``.tasks.<action>`` (a task
  record), ``.assets.<name>``, ``.id``/``.name``; calls ``.<action>(...)``.
- **Callbacks.** A callback prop holds one task call or one effect (``ui.copy``,
  ``task.answer``, ``query.refetch``, ``query.fetch_more``); nothing else holds them. A query
  effect names a declared query by a literal, and ``fetch_more`` one that declares ``more``.
- **Locals.** A local is a port: a control writes it only if the catalog kind of its ``bind``
  prop takes the local's kind (``bind_kinds``), a task call passes it whole only to an arg port
  of a kind that takes it (``arg_kinds``), where the action is known.
- **Records.** Two call sites never share an ``into``.
- **Literals.** A string literal on a ``BOOL``/``INT``/``FLOAT`` prop is refused, with a hint.
- **Loops.** A ``foreach`` without ``key`` over items known not to be scalars is a warning.
- **Returns.** Reading ``<record>.returns.return<N>`` (an unnamed return) is a warning.
- **Shadowing.** Using a component a module redefines over a standard catalog is a warning,
  once per name.
- **Theme.** A raw palette colour in a class is a warning naming the role; a logo paints with
  the pens only; a scene's ``color`` is a colour role (a warning otherwise).

The locals are resolved first (:mod:`.infer`): a local's left-out kind; dependencies are never
inferred. An ``ANY`` local skips the bind and argument kind checks and its default's, and a
path below it is taken as written. The walk and its scoping rules are :mod:`.walk`'s.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace

from rekuest_core import enums
from rekuest_core.inputs.models import (
    ActionArgumentInputModel,
    AgentDependencyInputModel,
    AgentProbeInputModel,
    ArgPortInputModel,
    BlokDemoInputModel,
    BlokLocalInputModel,
    BlokQueryInputModel,
    BlokSubscriptionInputModel,
    ComponentNodeInputModel,
    ComponentPropInputModel,
    PortMatchInputModel,
    StateDependencyInputModel,
    UtilCallInputModel,
)
from rekuest_core.vocabulary.declared import (
    DEPENDENCIES_COMPONENT,
    PICK_FUNCTION,
    PICK_VERB,
    REFERENCE_SEPARATOR,
    DeclaredDependencies,
    declared_dependencies,
    LIKE_PROP,
    LOCAL_COMPONENT,
    NAME_PROP,
    QUERY_COMPONENT,
    SUBSCRIPTION_COMPONENT,
    TEXT_NODE,
    VAR_COMPONENT,
    BlokHeader,
    blok_header,
    content,
    declaration_problem,
    declared_name_problem,
    declared_queries,
    declared_subscriptions,
    field_path_problem,
    header_problem,
    iter_with_parents,
    local_name,
    local_problem,
    operation_keyword,
)
from rekuest_core.vocabulary.infer import ResolvedLocal, resolve_locals
from rekuest_core.vocabulary.vocabulary import JSON, Finding, RecordField, Vocabulary, load_vocabulary
from rekuest_core.vocabulary.walk import (
    BIND_PROP,
    FOREACH_COMPONENT,
    FOREACH_ITEMS_PROP,
    FOREACH_KEY_PROP,
    Where,
    action_key_for,
    into_sites,
    is_callback_value,
    iter_nodes,
    walk_tree,
)

TOAST_COMPONENT = "Toast"
TOAST_TASK_PROP = "task"
CLASS_NAME = "className"
LOGO = "LOGO"
OPEN_IDENTIFIER = "*"
"""Marks a value nothing declares the shape of: any path below it is taken as written."""
ITEM = "..."
REFETCH_FUNCTION = "query.refetch"
FETCH_MORE_FUNCTION = "query.fetch_more"
QUERY_SUGAR = {"refetch": REFETCH_FUNCTION, "fetch_more": FETCH_MORE_FUNCTION}
"""The BSX sugar ``queries.<name>.<verb>(...)`` and the effect it is stored as."""
_VERBS = {function: verb for verb, function in QUERY_SUGAR.items()}
_COMPOUND_KINDS = frozenset({"DICT", "MODEL", "LIST", "UNION"})
"""Item kinds that are no scalar: a foreach over them is keyed by a stable field, not its position."""
_OPAQUE_KINDS = frozenset({"STRUCTURE", "MEMORY_STRUCTURE", "INTERFACE", "UNION"})
"""Port kinds whose fields the port does not declare: a path below such a local is taken as written."""

Match = PortMatchInputModel | None

_KINDS = {kind.value: kind for kind in enums.PortKind}
RETURNS_FIELD = "returns"
_UNNAMED_RETURN = re.compile(r"return[0-9]+")


def port_match(port: ArgPortInputModel, index: int = 0) -> PortMatchInputModel:
    """A port as the match a path into it descends (the spec's ``build_port_match``)."""
    children = [port_match(child, position) for position, child in enumerate(port.children or ())]
    return PortMatchInputModel(at=index, key=port.key, identifier=port.identifier, kind=port.kind, nullable=port.nullable, children=children or None)


def open_match(key: str | None) -> PortMatchInputModel:
    """A value of unknown shape that may still be looked into."""
    return PortMatchInputModel(key=key, identifier=OPEN_IDENTIFIER)


def is_open(match: Match) -> bool:
    """Whether ``match`` takes any path below it as written."""
    return match is not None and match.identifier == OPEN_IDENTIFIER


def record_match(key: str, fields: Iterable[RecordField], overrides: Mapping[str, PortMatchInputModel] | None = None) -> PortMatchInputModel:
    """A record of the vocabulary as a match; ``overrides`` replace fields by key (a typed ``value``)."""
    children: list[PortMatchInputModel] = []
    for record_field in fields:
        override = (overrides or {}).get(record_field.key)
        if override is not None:
            children.append(override.model_copy(update={"key": record_field.key}))
            continue
        if record_field.children:
            children.append(record_match(record_field.key, record_field.children).model_copy(update={"nullable": record_field.nullable or None}))
        elif record_field.kind in ("DICT", "ANY", "LIST"):
            children.append(open_match(record_field.key))
        else:
            children.append(PortMatchInputModel(key=record_field.key, kind=_KINDS.get(record_field.kind), nullable=record_field.nullable or None))
    return PortMatchInputModel(key=key, kind=enums.PortKind.DICT, children=children)


def structure_match(structure: PortMatchInputModel) -> PortMatchInputModel:
    """A STRUCTURE as what may be read below it: the ``structure`` record (``object``, ``__identifier``)."""
    record = record_match(structure.key or "structure", load_vocabulary().records["structure"].fields)
    return record.model_copy(update={"nullable": structure.nullable})


def task_record_match(key: str, returns: Sequence[PortMatchInputModel] | None = None) -> PortMatchInputModel:
    """The task record (``task``, ``.tasks.<action>``, an ``into`` name), ``returns`` typed when known."""
    record = load_vocabulary().records["task"]
    overrides = {"returns": PortMatchInputModel(key="returns", kind=enums.PortKind.DICT, children=list(returns))} if returns else None
    return record_match(key, record.fields, overrides)


# --------------------------------------------------------------------------- the scope


@dataclass(frozen=True)
class BlokScope:
    """Where a blok is mounted: what its mount provides, which its reads and calls resolve
    against. The default is a blok checked on its own, mounted nowhere.

    Args:
        kinds: The kinds the mount takes (an agent's blok: agent, or in a logo also action; an
            action's bloks: action or assign); empty takes any.
        mount: What the mount is, for a message (``bloks of process_slides``).
        dependencies: What ``self`` and ``deps.<key>`` resolve against; a ``self`` entry types
            ``self``, which is open without one. None: not mounted, every agent is taken as
            written (``deps.<key>``, ``Placed``/``Route`` keys and ``like`` go unchecked).
        args: The keys ``args.<key>`` may read; ``None`` reads any.
        demo: What a preview renders it with (a logo's loops are counted against it).
        query_values: Per declared query, the shape of its value as its service answers it.
        assets: The assets ``self.assets.<name>`` may name; ``None`` names any.
        port: The port a port blok is the widget of, as a match: what ``port.value`` is typed
            as. None leaves the value open.
    """

    kinds: tuple[str, ...] = ()
    mount: str = ""
    dependencies: tuple[AgentDependencyInputModel, ...] | None = None
    args: tuple[str, ...] | None = None
    demo: BlokDemoInputModel | None = None
    query_values: Mapping[str, PortMatchInputModel] = field(default_factory=dict[str, PortMatchInputModel])
    assets: frozenset[str] | None = None
    port: PortMatchInputModel | None = None
    workflows: Mapping[str, Sequence[AgentDependencyInputModel]] | None = None
    """Per action of the blok's own agent, that action's dependencies: what an agent blok's
    ``<dependencies key="cells" like="self.<action>" />`` declares as ``cells.<dependency>``.
    None: not known here, they are then taken as written."""

    @property
    def mounted(self) -> bool:
        """Whether the blok is checked where it is mounted (its dependencies are known)."""
        return self.dependencies is not None


@dataclass(frozen=True)
class LocalBinding:
    """A ``<local>`` in scope: its node and the port it resolved to."""

    node: str
    name: str
    local: BlokLocalInputModel


Binding = PortMatchInputModel | LocalBinding | None


# --------------------------------------------------------------------------- the checker


def _colour_pattern(vocabulary: Vocabulary) -> re.Pattern[str]:
    utilities = "|".join(sorted((re.escape(u) for u in vocabulary.theme.colour_utilities), key=len, reverse=True))
    colours = "|".join(vocabulary.theme.palette_colours)
    return re.compile(rf"^(?P<utility>{utilities})-(?P<colour>{colours})(?:-(?P<shade>\d{{2,3}}))?(?:/\d+)?$")


_ARBITRARY_COLOUR = re.compile(r"^(?P<utility>bg|text|border|ring|fill|stroke|from|via|to|outline|decoration|shadow)-\[(#|rgb|hsl|oklch|color)")
LITERAL_KINDS = (enums.CatalogValueKind.BOOL, enums.CatalogValueKind.INT, enums.CatalogValueKind.FLOAT)
"""Prop kinds a string literal never fits: ``fullWidth="true"`` is the string, write ``fullWidth={true}``."""
CALLBACK_KIND = enums.CatalogValueKind.CALLBACK
ASSET_KIND = enums.CatalogValueKind.ASSET


RUN_KINDS = ("action", "assign")
"""The kinds that have a run's dependencies: an action's own bloks, for a run and for starting one."""

ASSIGN_KIND = "assign"
PORT_ROOT = "port"
"""The root of a port blok: the port it is the widget of."""
PORT_VALUE_FIELD = "value"
"""The one field of ``port`` a control writes."""
GIVEN_FIELD = "given"
"""The field of ``assign`` that holds one flag per argument the caller decided."""
CONNECTED_FIELD = "connected"
"""The one field of an agent that is no string: whether it is connected right now."""
ASK_COMPONENT = "ask"
CHROME_PROP = "chrome"
CHROME_NONE = "none"
CONDITIONAL_COMPONENTS = frozenset({"if", "else", "match", "case", "default", "foreach", "suspense", "fallback", "error"})
"""The language's elements whose children do not always show: a host part inside one still claims its slot."""
START_EFFECT = "assign.start"
"""Starts the run an assign blok prepares."""

DEPENDENCY_KEY_PROPS = frozenset({("Placed", "dep"), ("Route", "through")})
"""Props whose value is the blok's dependency keys, written out (not paths): each must be a dependency."""


PICKER_COMPONENT = "deppicker"
OPTIONS_SEGMENT = "options"
"""``cells.options.<dependency>``: the agents that fit a declared dependency, to pick among."""
OPTION_FIELDS: tuple[Mapping[str, object], ...] = (
    {"key": "id", "kind": "STRING"},
    {"key": "name", "kind": "STRING", "nullable": True},
    {"key": "connected", "kind": "BOOL"},
    {"key": "picked", "kind": "BOOL"},
)
"""What one of them reads as."""


AGENTS_ROOT = "agents"
"""How a custom blok writes an agent it is bound to: ``agents.<key>``, stored ``deps.<key>``."""
CUSTOM_KIND = "custom"
DECLARING_KINDS = ("agent", CUSTOM_KIND)
"""The kinds whose blok may declare an action's dependencies itself (``<dependencies>``)."""


def as_written(text: str) -> str:
    """``text`` (a path, a call) as a custom blok writes it: a bound agent stored
    ``deps.<key>`` reads ``agents.<key>``; one through declared dependencies is left."""
    root, dot, rest = text.partition(".")
    if root != "deps" or not dot or REFERENCE_SEPARATOR in rest.split(".")[0].split("(")[0]:
        return text
    return f"{AGENTS_ROOT}.{rest}"


def reference_key(key: str, name: str) -> str:
    """The dependency ``<key>.<name>`` of declared dependencies, as it is stored."""
    return f"{key}{REFERENCE_SEPARATOR}{name}"


def borrowed_dependencies(declared: DeclaredDependencies, workflows: Mapping[str, Sequence[AgentDependencyInputModel]]) -> tuple[AgentDependencyInputModel, ...] | str:
    """The dependencies ``declared`` borrows, each keyed ``<key>__<dependency>``; or what is
    wrong with its ``like``."""
    action = declared.action
    if action is None:
        return "is not self.<action>"
    if action not in workflows:
        return f"names no action of the app ({action!r})"
    if not workflows[action]:
        return f"names an action with no dependencies ({action!r})"
    taken = next((d.key for d in workflows[action] if d.key in (OPTIONS_SEGMENT, PICK_VERB) or REFERENCE_SEPARATOR in d.key), None)
    if taken is not None:
        return f"names an action with a dependency called {taken!r}, which cannot be read through a reference: rename that parameter of {action}"
    return tuple(dependency.model_copy(update={"key": reference_key(declared.key, dependency.key)}) for dependency in workflows[action])


def bound_scope(tree: ComponentNodeInputModel, scope: BlokScope) -> BlokScope:
    """``scope`` with the dependencies an agent blok declares itself (``<dependencies>``)
    among its dependencies, keyed ``<key>__<dependency>`` and demanded as their action demands
    them. A declaration whose ``like`` does not resolve is left out (the checker says why), as
    is every one where the app's actions are not known (``workflows`` None)."""
    if scope.workflows is None or blok_header(tree).kind != "agent":
        return scope
    known = {dependency.key for dependency in scope.dependencies or ()}
    borrowed: list[AgentDependencyInputModel] = []
    for declared in declared_dependencies(tree):
        found = borrowed_dependencies(declared, scope.workflows)
        if isinstance(found, str):
            continue
        for dependency in found:
            if dependency.key not in known:
                known.add(dependency.key)
                borrowed.append(dependency)
    return dataclass_replace(scope, dependencies=(*(scope.dependencies or ()), *borrowed)) if borrowed else scope


class _Checker:
    """Walks a tree with a :class:`BlokScope` and collects findings."""

    def __init__(self, header: BlokHeader, scope: BlokScope, locals_: Mapping[str, ResolvedLocal]) -> None:
        self.header = header
        self.scope = scope
        self.vocabulary = load_vocabulary()
        self.kind_name = header.kind
        self.context = header.context
        self.kind = self.vocabulary.kind(header.kind).drawn_in(header.context)
        self.findings: list[Finding] = []
        self.mounted = scope.dependencies is not None
        self.dependencies = {dependency.key: dependency for dependency in scope.dependencies or ()}
        self.resolved = locals_
        """Per ``<local>`` node id, its port with its kind resolved."""
        self.queries: dict[str, BlokQueryInputModel] = {}
        self.subscriptions: dict[str, BlokSubscriptionInputModel] = {}
        self.bound_locals: set[str] = set()
        """The node ids of the locals a control binds."""
        self.into: set[str] = set()
        self.asks = False
        self.chromeless: str | None = header.node if header.chrome == CHROME_NONE else None
        """The id of the root that says ``chrome="none"``, when it does."""
        self.colour = _colour_pattern(self.vocabulary)
        self.declared: dict[str, DeclaredDependencies] = {}
        """The dependencies an agent blok declares itself, by key."""

    def add(self, code: str, at: str | None, /, **fields: object) -> None:
        if self.kind_name == CUSTOM_KIND:
            fields = {key: as_written(value) if isinstance(value, str) else value for key, value in fields.items()}
        self.findings.append(Finding.of(code, at, **fields))

    def reads(self, root: str) -> bool:
        """Whether the kind reads the stored ``root``: a custom blok's ``agents`` is stored ``deps``."""
        return self.kind.reads(root)

    def declares(self, dependency: str) -> bool:
        """Whether ``dependency`` (as stored) is one the blok declares in its own tree: every
        dependency of an agent blok, a ``<key>__<name>`` one of a custom blok."""
        return self.kind_name == "agent" or (self.kind_name == CUSTOM_KIND and REFERENCE_SEPARATOR in dependency)

    def roots(self) -> str:
        return ", ".join(sorted(self.kind.roots))

    # -- declarations

    def check_declarations(self, tree: ComponentNodeInputModel) -> None:
        vocabulary = self.vocabulary
        header = self.header
        kind = self.kind_name
        row = vocabulary.kind(kind)
        context_name = _spelled(header.context)
        if header.context not in row.contexts:
            self.add("context.kind", tree.id, n=_article(kind), kind=kind, contexts=", ".join(_spelled(c) for c in row.contexts), context=context_name)
        if self.scope.kinds and kind not in self.scope.kinds:
            expected = " or ".join(self.scope.kinds)
            self.add("kind.placement", tree.id, n=_article(kind), kind=kind, m=_article(self.scope.kinds[0]), expected=expected, where=self.scope.mount or "here")
        if header.chrome is not None and header.context not in vocabulary.blok.chrome:
            self.add("chrome.context", tree.id, context=context_name)
        allowed = vocabulary.blok.content.get(header.context)
        if allowed is not None:
            drawn = content(tree)
            if len(drawn) != 1 or drawn[0].component not in allowed:
                found = ", ".join(f"<{node.component}>" if node.component != TEXT_NODE else "text" for node in drawn) or "nothing"
                self.add("blok.content", tree.id, context=context_name, expected=" or ".join(f"one <{name}>" for name in allowed), found=found)
        self.check_dependencies(tree)
        self.check_local_positions(tree)
        self.check_queries(tree)
        for name, sites in into_sites(tree).items():
            self.into.add(name)
            if name in vocabulary.reserved:
                self.add("roots.reserved", sites[0][1], name=name)
            if len(sites) > 1:
                self.add("into.twice", sites[1][1], name=name, first=sites[0][1], second=sites[1][1])

    def check_local_positions(self, tree: ComponentNodeInputModel) -> None:
        """Each ``<local>`` reads, comes first among its parent's children, in a kind that has
        locals; a persisted one inside a keyless foreach is a warning. Each ``<var>`` sits in a
        query or a subscription. (Whether its parent takes children is its catalog's to say.)"""
        for node, ancestors in iter_with_parents(tree):
            if node.component == VAR_COMPONENT and (not ancestors or ancestors[-1].component not in (QUERY_COMPONENT, SUBSCRIPTION_COMPONENT)):
                self.add("query.declaration", node.id, component="var", problem="belongs inside a <query> or <subscription>")
            if node.component != LOCAL_COMPONENT:
                continue
            name = local_name(node) or "?"
            problem = local_problem(node)
            if problem is not None:
                self.findings.append(problem)
                continue
            if not self.kind.locals:
                self.add("roots.not_in_kind", node.id, path=f"<local name={name!r}>", n=_article(self.kind_name), kind=self.kind_name, root="locals", roots=self.roots())
            if name in self.vocabulary.reserved:
                self.add("roots.reserved", node.id, name=name)
            parent = ancestors[-1] if ancestors else None
            if parent is None:
                self.add("local.position", node.id, name=name, problem="is a blok's root; a blok is a <Blok>")
                continue
            siblings = parent.children or []
            before = siblings[: siblings.index(node)]
            other = next((sibling for sibling in before if sibling.component != LOCAL_COMPONENT), None)
            if other is not None:
                shown = "text" if other.component == TEXT_NODE else f"<{other.component}>"
                self.add("local.position", node.id, name=name, problem=f"comes after {shown} ({other.id}); locals come first among their parent's children")
            if parent.component in (QUERY_COMPONENT, SUBSCRIPTION_COMPONENT, LOCAL_COMPONENT, VAR_COMPONENT):
                self.add("local.position", node.id, name=name, problem=f"is inside <{parent.component}>, which declares, and draws nothing")
            resolved = self.resolved.get(node.id)
            if resolved is None:
                continue
            if resolved.like_problem is not None:
                like = next((prop.static_value for prop in node.props or () if prop.key == LIKE_PROP), None)
                self.add("local.like", node.id, name=name, like=like, problem=resolved.like_problem)
            self.check_local_default(node.id, resolved.local)
            if resolved.local.persist:
                loop = next((ancestor for ancestor in reversed(ancestors) if ancestor.component == FOREACH_COMPONENT), None)
                if loop is not None and not any(prop.key == FOREACH_KEY_PROP for prop in loop.props or ()):
                    self.add("local.persist_id", node.id, name=name)

    def check_dependencies(self, tree: ComponentNodeInputModel) -> None:
        """A ``<dependencies>`` is a child of an agent blok's ``<Blok>``, keyed once, and names
        one of the app's own actions that has dependencies."""
        for node, ancestors in iter_with_parents(tree):
            if node.component != DEPENDENCIES_COMPONENT:
                continue
            literals = {prop.key: prop.static_value for prop in node.props or ()}
            key = literals.get("key")
            if len(ancestors) != 1 or self.kind_name not in DECLARING_KINDS:
                at = ancestors[-1].id if len(ancestors) != 1 and ancestors else f"a{_article(self.kind_name)} {self.kind_name} blok"
                self.add("dependencies.position", node.id, key=key if isinstance(key, str) else "?", at=at)
                continue
            if not isinstance(key, str) or declared_name_problem(key):
                self.add("dependencies.key", node.id, node=node.id, problem="has no plain key")
                continue
            if key == "self" or key in self.declared:
                problem = "is keyed self, which is the blok's own agent" if key == "self" else f"declares {key!r} a second time"
                self.add("dependencies.key", node.id, node=node.id, problem=problem)
                continue
            like = literals.get("like")
            declared = DeclaredDependencies(key=key, node=node.id, like=like if isinstance(like, str) else None)
            self.declared[key] = declared
            owner = declared.owner
            if self.kind_name == CUSTOM_KIND:
                if owner is None or owner == "self":
                    self.add("dependencies.like", node.id, key=key, like=declared.like or "", problem="is not agents.<name>.<action>")
                elif self.mounted and owner not in self.dependencies:
                    self.add("dependencies.like", node.id, key=key, like=declared.like or "", problem=f"names the agent {owner!r}, which the blok is not bound to")
            elif owner != "self":
                self.add("dependencies.like", node.id, key=key, like=declared.like or "", problem="is not self.<action>")
            elif self.scope.workflows is not None:
                borrowed = borrowed_dependencies(declared, self.scope.workflows)
                if isinstance(borrowed, str):
                    self.add("dependencies.like", node.id, key=key, like=declared.like or "", problem=borrowed)

    def typed(self, key: str) -> bool:
        """Whether the dependencies declared as ``key`` are known here: its action is one of
        the app's, so what is read through it is held to that action's dependencies."""
        declared = self.declared.get(key)
        if declared is None or declared.owner != "self" or self.scope.workflows is None:
            return False  # a bound agent's actions are known only where the blok runs
        return not isinstance(borrowed_dependencies(declared, self.scope.workflows), str)

    def undeclared(self, dependency: str) -> bool:
        """Whether ``dependency`` (stored ``<key>__<name>``) is none this agent blok declares:
        its dependencies are the ones in its own tree, mounted or not."""
        if not self.declares(dependency) or dependency == "self":
            return False
        key, separator, name = dependency.partition(REFERENCE_SEPARATOR)
        if not separator or key not in self.declared:
            return True
        return self.typed(key) and name != OPTIONS_SEGMENT and dependency not in self.dependencies

    def options(self, key: str, rest: Sequence[str], path: str, node_id: str) -> Match:
        """``<key>.options.<dependency>``: the agents that fit it, each ``{id, name, connected, picked}``."""
        if not rest:
            self.add("roots.whole", node_id, path=path, root=f"{key}.{OPTIONS_SEGMENT}")
            return None
        if self.typed(key) and reference_key(key, rest[0]) not in self.dependencies:
            self.add("deps.unknown", node_id, path=path, key=f"{key}.{rest[0]}", keys=self.dependency_known(key), hint=dependency_hint(key, self.kind_name))
            return None
        listed = PortMatchInputModel.model_validate({"key": rest[0], "kind": "LIST", "children": [{"key": "...", "kind": "DICT", "children": list(OPTION_FIELDS)}]})
        return self.descend(listed, rest[1:], path, node_id)

    def check_pick(self, call: UtilCallInputModel, where: Where) -> None:
        """``<key>.pick(<dependency>: <agent id>)``: declared dependencies, and dependencies of them."""
        arguments = {argument.key: argument for argument in call.arguments or ()}
        named, picks = arguments.get("key"), arguments.get("picks")
        key = named.value_literal if named is not None else None
        if not isinstance(key, str) or key not in self.declared:
            shown = key if isinstance(key, str) else "?"
            self.add("deps.unknown", where.node.id, path=f"{shown}.{PICK_VERB}(...)", key=shown, keys=self.dependency_known(shown), hint=dependency_hint(shown, self.kind_name))
            return
        if not self.typed(key):
            return
        for pick in (picks.value_dict if picks is not None else None) or ():
            if pick.key is not None and reference_key(key, pick.key) not in self.dependencies:
                self.add("deps.unknown", where.node.id, path=f"{key}.{PICK_VERB}(...)", key=f"{key}.{pick.key}", keys=self.dependency_known(key), hint=dependency_hint(key, self.kind_name))

    def check_picker(self, node: ComponentNodeInputModel) -> None:
        """``<deppicker key="cells" />`` or ``key="cells.microscope"``: declared dependencies."""
        written = next((prop.static_value for prop in node.props or () if prop.key == "key"), None)
        if not isinstance(written, str):
            return  # catalog.prop says a key is needed
        key, _, name = written.partition(".")
        known = key in self.declared and (not name or not self.typed(key) or reference_key(key, name) in self.dependencies)
        if not known:
            self.add("deps.unknown", node.id, path=f"{node.component}.key", key=written, keys=self.dependency_known(key), hint=dependency_hint(key, self.kind_name))

    def check_with(self, call: AgentProbeInputModel, where: Where) -> None:
        """``with <key>``: dependencies the blok declares, of the action that is called."""
        given, action = call.given or "", action_key_for(call)
        declared = self.declared.get(given)
        if declared is None:
            self.add("deps.unknown", where.node.id, path=f"with {given}", key=given, keys=", ".join(sorted(self.declared)) or "none", hint=dependency_hint(given, self.kind_name))
        elif declared.target is not None and declared.target != (call.dependency, action):
            self.add("with.mismatch", where.node.id, path=where.text, key=given, action=action, of=declared.action)

    def dependency_known(self, key: str) -> str:
        """What ``key`` may read through, for a message: the dependencies declared under it,
        else the keys the blok declares."""
        if self.kind_name != "agent" and key not in self.declared:
            return ", ".join(sorted(self.dependencies)) or "none"
        if key in self.declared and self.typed(key):
            prefix = f"{key}{REFERENCE_SEPARATOR}"
            return ", ".join(sorted(f"{key}.{name.removeprefix(prefix)}" for name in self.dependencies if name.startswith(prefix))) or "none"
        return ", ".join(sorted(self.declared)) or "none"

    def check_queries(self, tree: ComponentNodeInputModel) -> None:
        """Queries and subscriptions are children of the ``<Blok>``, each declared once and
        readable, a query a ``query`` and a subscription a ``subscription`` (neither a mutation)."""
        first: dict[tuple[str, str], str] = {}
        for node, ancestors in iter_with_parents(tree):
            if node.component not in (QUERY_COMPONENT, SUBSCRIPTION_COMPONENT):
                continue
            name = next((prop.static_value for prop in node.props or () if prop.key == NAME_PROP and isinstance(prop.static_value, str)), "?")
            if len(ancestors) != 1:
                at = ancestors[-1].id if ancestors else "the root"
                self.add("query.position", node.id, component=node.component, name=name, at=at)
            problem = declaration_problem(node)
            if problem is not None:
                self.findings.append(problem)
                continue
            seen = first.get((node.component, name))
            if seen is not None:
                self.add("query.duplicate", node.id, what=node.component, name=name, first=seen, second=node.id)
            first.setdefault((node.component, name), node.id)
        self.queries = {query.key: query for query in declared_queries(tree)}
        self.subscriptions = {subscription.key: subscription for subscription in declared_subscriptions(tree)}
        declared: tuple[tuple[str, Iterable[BlokQueryInputModel | BlokSubscriptionInputModel]], ...] = (
            ("query", self.queries.values()),
            ("subscription", self.subscriptions.values()),
        )
        for what, each_declared in declared:
            for each in each_declared:
                at = f"{'queries' if what == 'query' else 'subscriptions'}.{each.key}"
                if declared_name_problem(each.key):
                    self.add("manifest.name", at, what=what, key=each.key)
                if each.key in self.vocabulary.reserved:
                    self.add("roots.reserved", at, name=each.key)
                operation = operation_keyword(each.document)
                if operation == "mutation":
                    self.add("query.read_only", at, kind=what, name=each.key, problem="is a mutation: a blok reads, and writes only through a task")
                elif operation != what:
                    self.add("query.kind", at, kind=what, name=each.key, operation=operation or "document without an operation")
        for query in self.queries.values():
            if query.more is not None and field_path_problem(query.more):
                self.add("query.more", f"queries.{query.key}", name=query.key, more=query.more, problem="is not a field path into its data (positions, images.items)")
        if self.kind_name == "action" and self.context == LOGO and self.dependencies:
            self.add("manifest.action_logo", self.header.node)
        for subscription in self.subscriptions.values():
            if subscription.history is not None and subscription.history not in self.queries:
                self.add("query.history", f"subscriptions.{subscription.key}", name=subscription.key, history=subscription.history)

    def check_local_default(self, node_id: str, local: BlokLocalInputModel) -> None:
        """A local's default fits its port: a number for a number, a list for a list, one of its choices."""
        if local.kind is None or local.kind == enums.LocalKind.ANY:
            return
        kind = local.kind.value
        kinds: dict[str, type | tuple[type, ...]] = {
            "STRING": str, "DATE": str, "STRUCTURE": str, "INT": int, "FLOAT": (int, float), "QUANTITY": (int, float),
            "BOOL": bool, "LIST": list, "DICT": dict, "MODEL": dict,
        }
        value: object = local.default
        if value is None:
            return
        expected = kinds.get(kind)
        wrong = expected is not None and (not isinstance(value, expected) or (kind in ("INT", "FLOAT", "QUANTITY") and isinstance(value, bool)))
        if kind == "ENUM" and local.choices is not None:
            wrong = value not in [choice.value for choice in local.choices]
        if wrong:
            self.add("local.default", node_id, name=local.key, default=value, kind=kind)

    def local_port(self, binding: LocalBinding, path: str) -> ArgPortInputModel | None:
        """The port a path into a local names (the local, or a field of it), or None when its
        shape does not say (an ``ANY`` local)."""
        port: ArgPortInputModel | None = binding.local.arg_port()
        for part in path.split(".")[1:]:
            if port is None:
                return None
            children = port.children or []
            child = next((candidate for candidate in children if candidate.key == part), None)
            if child is None and len(children) == 1 and children[0].key == ITEM:
                child = children[0]
            port = child
        return port

    def finish(self) -> None:
        for node_id, resolved in self.resolved.items():
            if resolved.local.persist and node_id not in self.bound_locals:
                self.add("local.unused_persist", node_id, name=resolved.local.key)
        if self.chromeless is not None and "task" in self.kind.roots and not self.asks:
            self.add("chrome.no_ask", self.chromeless)

    # -- the visitor

    def visit_component(self, node: ComponentNodeInputModel, ancestors: tuple[ComponentNodeInputModel, ...], scope: Mapping[str, Binding]) -> None:
        self.check_host_part(node, ancestors)
        if node.component == PICKER_COMPONENT:
            self.check_picker(node)
        for prop in node.props or ():
            self.check_prop(node, prop, scope)

    def check_host_part(self, node: ComponentNodeInputModel, ancestors: tuple[ComponentNodeInputModel, ...]) -> None:
        """What the language itself says of the host's parts, whatever catalog draws them: a part
        placed anywhere claims its slot."""
        if node.component == ASK_COMPONENT:
            self.asks = True
        if node.component in self.vocabulary.host_parts:
            inside = next((ancestor.component for ancestor in reversed(ancestors) if ancestor.component in CONDITIONAL_COMPONENTS), None)
            if inside is not None:
                self.add("part.conditional", node.id, component=node.component, inside=inside)

    def check_dependency_keys(self, node: ComponentNodeInputModel, prop: ComponentPropInputModel) -> None:
        """A ``Placed dep`` or ``Route through`` names dependencies of the blok, written out as keys."""
        if self.kind_name not in RUN_KINDS or not self.mounted:
            return  # which kinds a Space and its parts belong to is their catalog's to say
        path = f"{node.component}.{prop.key}"
        value = prop.static_value
        keys = [value] if isinstance(value, str) else [key for key in value if isinstance(key, str)] if isinstance(value, list) else []
        for key in keys:
            if key not in self.dependencies:
                self.add("deps.unknown", node.id, path=path, key=key, keys=", ".join(sorted(self.dependencies)) or "none", hint=dependency_hint(key, self.kind_name))

    def check_prop(self, node: ComponentNodeInputModel, prop: ComponentPropInputModel, scope: Mapping[str, Binding]) -> None:
        vocabulary = self.vocabulary
        operation = vocabulary.functions.get(prop.util_call.operation) if prop.util_call is not None else None
        if operation is not None and operation.task_operation and "task" not in self.kind.roots:
            self.add("roots.not_in_kind", node.id, path=operation.name, n=_article(self.kind_name), kind=self.kind_name, root="task", roots=self.roots())
        # An effect that is a root's own (port.set) belongs to the kinds that read that root.
        if operation is not None and operation.needs is not None and not self.reads(operation.needs):
            self.add("roots.not_in_kind", node.id, path=operation.name, n=_article(self.kind_name), kind=self.kind_name, root=operation.needs, roots=self.roots())
        if prop.key == BIND_PROP:
            path = prop.dynamic_value.path if prop.dynamic_value is not None else None
            if path is None:
                self.add("local.bind", node.id, what=_what(prop))
            else:
                root = path.split(".")[0]
                binding = scope.get(root)
                if root == PORT_ROOT and root not in scope and PORT_ROOT in self.kind.roots:
                    # The one thing besides a local a control writes: the value of the port a
                    # port blok stands for (and what is below it), never what the form says of it.
                    if path.split(".")[1:2] != [PORT_VALUE_FIELD]:
                        self.add("port.bind", node.id, path=path)
                elif not isinstance(binding, LocalBinding):
                    self.add("local.unknown", node.id, path=path, root=root)
                else:
                    # What kind the control writes is its catalog's to say: not checked here.
                    self.bound_locals.add(binding.node)
        if prop.util_call is not None and prop.util_call.operation == START_EFFECT and self.kind_name != ASSIGN_KIND:
            self.add("assign.kind", node.id, what="assign.start()", n=_article(self.kind_name), kind=self.kind_name)
        if (node.component, prop.key) in DEPENDENCY_KEY_PROPS:
            self.check_dependency_keys(node, prop)
        if node.component == TOAST_COMPONENT and prop.key == TOAST_TASK_PROP:
            path = prop.dynamic_value.path if prop.dynamic_value is not None else None
            if path is None or (path != "task" and path not in self.into):
                self.add("toast.task", node.id, what=path or _what(prop))
        if isinstance(prop.static_value, str):
            if prop.key == CLASS_NAME:
                self.check_classes(prop.static_value, node.id)
            if self.context == LOGO and prop.key in vocabulary.theme.logo_paint_props:
                self.check_paint(node, prop.key, prop.static_value)
        if prop.key == CLASS_NAME and prop.util_call is not None:
            for literal in _class_literals(prop.util_call):
                self.check_classes(literal, node.id)

    def check_classes(self, classes: str, owner: str) -> None:
        hints = self.vocabulary.theme.palette_role_hints
        for token in classes.split():
            base = token.rsplit(":", 1)[-1].lstrip("!")
            match = self.colour.match(base)
            if match is not None:
                role = hints.get(match.group("colour"), "primary")
                self.add("palette.raw_colour", owner, token=token, suggestion=f"{match.group('utility')}-{role}")
                continue
            arbitrary = _ARBITRARY_COLOUR.match(base)
            if arbitrary is not None:
                self.add("palette.raw_colour", owner, token=token, suggestion=f"{arbitrary.group('utility')}-<role> ({', '.join(self.vocabulary.theme.colour_roles[:6])}, ...)")
                continue
            # A colour that is no role is told as that; anything else outside the portable subset as this.
            unportable = unportable_class(token, self.vocabulary)
            if unportable is not None:
                self.add("class.not_portable", owner, token=token, what=unportable)

    def check_paint(self, node: ComponentNodeInputModel, key: str, value: str) -> None:
        paint = value.strip()
        if paint in self.vocabulary.theme.logo_pens or (paint.startswith("url(#") and paint.endswith(")")):
            return
        self.add("palette.logo_paint", node.id, component=node.component, prop=key, value=value, pens="|".join(self.vocabulary.theme.logo_pens))

    def visit_path(self, path: str, where: Where, scope: Mapping[str, Binding]) -> None:
        self.resolve(path, where, scope, owner=where.text)

    def visit_agent_call(self, call: AgentProbeInputModel, where: Where, scope: Mapping[str, Binding], into: str | None) -> None:
        kind = self.kind
        owner = where.text
        action = action_key_for(call)
        if not kind.calls:
            called = f"self.{action}(...)" if call.dependency == "self" else f"deps.{call.dependency}.{action}(...)"
            self.add("calls.not_in_kind", where.node.id, path=called, n=_article(self.kind_name), kind=self.kind_name)
            return
        if call.dependency == "self":
            if "self" not in kind.roots:
                self.add("roots.not_in_kind", where.node.id, path=f"self.{action}(...)", n=_article(self.kind_name), kind=self.kind_name, root="self", roots=self.roots())
                return
        elif not self.reads("deps"):
            self.add("roots.not_in_kind", where.node.id, path=f"deps.{call.dependency}.{action}(...)", n=_article(self.kind_name), kind=self.kind_name, root="deps", roots=self.roots())
            return
        if call.given is not None:
            self.check_with(call, where)
        dependency = None if self.undeclared(call.dependency) else self.dependencies.get(call.dependency)
        if dependency is None:
            if call.dependency != "self" and (self.undeclared(call.dependency) if self.declares(call.dependency) else self.mounted):
                written = call.dependency.partition(REFERENCE_SEPARATOR)[0]
                self.add("deps.unknown", where.node.id, path=owner, key=call.dependency.replace(REFERENCE_SEPARATOR, "."), keys=self.dependency_known(written), hint=dependency_hint(written, self.kind_name))
            return

        actions = dependency.action_dependencies
        if actions is not None and action not in {demand.key for demand in actions}:
            self.add("deps.unknown_action", where.node.id, path=owner, key=call.dependency, action=action, actions=", ".join(sorted(demand.key for demand in actions)) or "none")
            return
        demand = next((each.demand for each in actions or () if each.key == action), None)
        if demand is not None and demand.arg_matches:
            self.check_arg_kinds(call, demand.arg_matches, where, scope)

    def check_arg_kinds(self, call: AgentProbeInputModel, matches: Sequence[PortMatchInputModel], where: Where, scope: Mapping[str, Binding]) -> None:
        """A local passed whole to a task call fits the action's arg port (``arg_kinds``)."""
        ports = {match.key: match for match in matches if match.key is not None}
        name = f"self.{call.operation}" if call.dependency == "self" else f"deps.{call.dependency}.{call.operation}"
        for argument in call.arguments or ():
            path = argument.value_path
            match = ports.get(argument.key or "")
            if path is None or match is None or match.kind is None:
                continue
            binding = scope.get(path.split(".")[0])
            if not isinstance(binding, LocalBinding):
                continue
            port = self.local_port(binding, path)
            if port is None:
                continue
            kind = port.kind.value
            port_kind = match.kind.value
            allowed = self.vocabulary.arg_kinds.get(kind)
            identifiers = (port.identifier, match.identifier)
            other_structure = kind == "STRUCTURE" and port_kind == "STRUCTURE" and None not in identifiers and identifiers[0] != identifiers[1]
            if (allowed is not None and port_kind not in allowed) or other_structure:
                shown = f"{port_kind} {match.identifier}" if other_structure else port_kind
                self.add("local.arg_kind", where.node.id, call=name, argument=argument.key, port_kind=shown, path=path, kind=f"{kind} {port.identifier}" if other_structure else kind)

    def visit_util_call(self, call: UtilCallInputModel, where: Where, scope: Mapping[str, Binding], top: bool) -> None:
        function = self.vocabulary.functions.get(call.operation)
        if function is None:
            if call.operation.split(".", 1)[0] not in self.vocabulary.function_namespaces or "." not in call.operation:
                self.add("expr.unknown_callee", where.node.id, callee=call.operation)
            # A function of a namespace this vocabulary does not list may be a catalog's: not the server's to say.
            return
        if function.effect and not top:
            self.add("callback.nested", where.node.id, what=call.operation)
        if call.operation in _VERBS and top:
            self.check_query_effect(call, where)
        if call.operation == PICK_FUNCTION:
            self.check_pick(call, where)
        keys = {argument.key for argument in call.arguments or ()}
        unknown = sorted(key for key in keys if key is not None and key not in function.keys and not key.isdigit())
        if unknown:
            self.add("expr.arguments", where.node.id, function=call.operation, detail=f"has no argument {unknown[0]!r} ({', '.join(function.keys)})")
        missing = [key for key in function.required_keys if key not in keys]
        if missing:
            self.add("expr.arguments", where.node.id, function=call.operation, detail=f"needs {', '.join(missing)}")

    def check_query_effect(self, call: UtilCallInputModel, where: Where) -> None:
        """``query.refetch`` / ``query.fetch_more``: a declared query, named by a literal; fetch_more
        only of one that declares ``more``."""
        verb = _VERBS[call.operation]
        named = next((argument for argument in call.arguments or () if argument.key == "query"), None)
        if named is None:
            return  # expr.arguments says it is missing
        name = named.value_literal
        if not isinstance(name, str) or any(value is not None for value in (named.value_path, named.util_call, named.agent_call, named.value_list, named.value_dict)):
            self.add("query.effect_target", where.node.id, function=call.operation, verb=verb, what=_argument_what(named))
            return
        path = f"queries.{name}.{verb}()"
        if "queries" not in self.kind.roots:
            self.add("roots.not_in_kind", where.node.id, path=path, n=_article(self.kind_name), kind=self.kind_name, root="queries", roots=self.roots())
            return
        query = self.queries.get(name)
        if query is None:
            self.add("query.unknown", where.node.id, path=path, name=name, names=", ".join(sorted(self.queries)) or "none")
            return
        if call.operation == FETCH_MORE_FUNCTION and query.more is None:
            self.add("query.fetch_more", where.node.id, path=path, name=name)

    def bind_let(self, name: str, items: ComponentPropInputModel | None, where: Where, scope: Mapping[str, Binding]) -> Binding:
        if name in self.vocabulary.reserved:
            self.add("roots.reserved", where.node.id, name=name)
        elif name in scope:
            self.add("roots.shadow", where.node.id, name=name, first=_binding_what(name, scope[name], self.into), second=f"the let of <foreach> ({where.node.id})")
        keyed = any(prop.key == FOREACH_KEY_PROP for prop in where.node.props or ())
        if items is not None and isinstance(items.static_value, list) and not keyed:
            if any(isinstance(item, (dict, list)) for item in items.static_value):
                self.add("foreach.position_key", where.node.id, let=name)
        if items is None or items.dynamic_value is None or items.dynamic_value.path is None:
            return open_match(name)
        resolved = self.resolve(items.dynamic_value.path, None, scope, owner=where.text)
        if resolved is None or is_open(resolved):
            return open_match(name)
        children = resolved.children or []
        if len(children) == 1 and children[0].key == ITEM:
            item = children[0]
            if not keyed and (item.children or (item.kind is not None and item.kind.value in _COMPOUND_KINDS)):
                self.add("foreach.position_key", where.node.id, let=name)
            return item.model_copy(update={"key": name})
        if resolved.kind in (enums.PortKind.LIST, None):
            return open_match(name)
        self.add("path.unknown", where.node.id, path=items.dynamic_value.path, detail=f"{FOREACH_ITEMS_PROP} of a foreach reads a list, not this {str(resolved.kind.value).lower()}")
        return open_match(name)

    def bind_index(self, name: str, where: Where, scope: Mapping[str, Binding]) -> Binding:
        if name in self.vocabulary.reserved:
            self.add("roots.reserved", where.node.id, name=name)
        elif name in scope:
            self.add("roots.shadow", where.node.id, name=name, first=_binding_what(name, scope[name], self.into), second=f"the index of <foreach> ({where.node.id})")
        return PortMatchInputModel(key=name, kind=enums.PortKind.INT)

    def bind_local(self, name: str, node: ComponentNodeInputModel, scope: Mapping[str, Binding]) -> Binding:
        if name in scope and name not in self.vocabulary.reserved:
            self.add("roots.shadow", node.id, name=name, first=_binding_what(name, scope[name], self.into), second=f"the local {node.id}")
        resolved = self.resolved.get(node.id)
        if resolved is None:
            return open_match(name)
        return LocalBinding(node=node.id, name=name, local=resolved.local)

    def bind_into(self, name: str, call: AgentProbeInputModel) -> Binding:
        dependency = self.dependencies.get(call.dependency)
        action = action_key_for(call)
        returns: list[PortMatchInputModel] | None = None
        for demand in (dependency.action_dependencies or ()) if dependency is not None else ():
            if demand.key == action and demand.demand is not None:
                returns = demand.demand.return_matches
        return task_record_match(name, returns)

    # -- paths

    def resolve(self, path: str, where: Where | None, scope: Mapping[str, Binding], *, owner: str) -> Match:
        """What ``path`` holds, recording a finding (and returning None) when it holds nothing."""
        vocabulary = self.vocabulary
        node_id = where.node.id if where is not None else owner
        parts = path.split(".")
        root, rest = parts[0], parts[1:]
        if root in scope:
            binding = scope[root]
            if isinstance(binding, LocalBinding):
                port = binding.local.arg_port()
                if port is None or port.kind.value in _OPAQUE_KINDS:
                    return open_match(root)
                return self.descend(port_match(port).model_copy(update={"key": root, "at": None}), rest, path, node_id)
            if root in self.into:
                self.check_returns(path, rest, node_id)
            return self.descend(binding, rest, path, node_id)
        if root == "event":
            if where is None or not where.callback:
                self.add("roots.event_outside_callback", node_id, path=path)
                return None
            return self.event_field(rest, where)
        if root == "error":
            if where is None or not where.in_error:
                self.add("roots.error_outside_boundary", node_id, path=path)
                return None
            return self.descend(record_match("error", vocabulary.records["error"].fields), rest, path, node_id)
        if root not in vocabulary.data_roots:
            detail = "a function namespace" if root in vocabulary.function_namespaces else None
            self.add("roots.unknown", node_id, path=path, root=root if detail is None else f"{root} ({detail})")
            return None
        if not self.reads(root) or root == AGENTS_ROOT:
            # `agents` is how a custom blok writes `deps`: a stored tree never holds it.
            self.add("roots.not_in_kind", node_id, path=path, n=_article(self.kind_name), kind=self.kind_name, root=root, roots=self.roots())
            return None
        if root == "then":
            if rest:
                self.add("path.unknown", node_id, path=path, detail="then is a moment (null while live), it has no fields")
            return PortMatchInputModel(key="then", kind=enums.PortKind.STRING, nullable=True)
        if root == "self":
            return self.agent(self.dependencies.get("self"), "self", rest, path, node_id)
        if root == "deps":
            if not rest:
                self.add("roots.whole", node_id, path=path, root="deps")
                return None
            # An agent blok's dependencies are in its own tree (`cells.microscope`, stored
            # `deps.cells__microscope`): one it does not declare is unknown, mounted or not;
            # one it declares and this side cannot type is open.
            if self.declares(rest[0]) and rest[0] != "self":
                key, separator, name = rest[0].partition(REFERENCE_SEPARATOR)
                shown = f"{key}.{name}" if separator else rest[0]
                if separator and key in self.declared and name == OPTIONS_SEGMENT:
                    return self.options(key, rest[1:], path, node_id)
                if self.undeclared(rest[0]):
                    self.add("deps.unknown", node_id, path=path, key=shown, keys=self.dependency_known(key), hint=dependency_hint(key, self.kind_name))
                    return None
                found = self.dependencies.get(rest[0])
                return self.agent(found, rest[0], rest[1:], path, node_id) if found is not None else open_match(path)
            dependency = self.dependencies.get(rest[0])
            if dependency is None:
                if not self.mounted or self.declares(rest[0]):
                    return open_match(path)
                self.add("deps.unknown", node_id, path=path, key=rest[0], keys=", ".join(sorted(self.dependencies)) or "none", hint=dependency_hint(rest[0], self.kind_name))
                return None
            return self.agent(dependency, rest[0], rest[1:], path, node_id)
        if root == "args":
            if not rest:
                self.add("roots.whole", node_id, path=path, root="args")
                return None
            if self.scope.args is not None and rest[0] not in self.scope.args:
                self.add("args.unknown", node_id, path=path, key=rest[0], keys=", ".join(self.scope.args) or "none")
                return None
            return open_match(path)
        if root == "task":
            self.check_returns(path, rest, node_id)
            return self.descend(task_record_match("task"), rest, path, node_id)
        if root == "assign":
            # ``assign.given.<key>`` is open below ``given``: one flag per argument of the action.
            if len(rest) > 1 and rest[0] == GIVEN_FIELD:
                if self.scope.args is not None and rest[1] not in self.scope.args:
                    self.add("args.unknown", node_id, path=path, key=rest[1], keys=", ".join(self.scope.args) or "none")
                    return None
                return PortMatchInputModel(key=rest[1], kind=enums.PortKind.BOOL)
            return self.descend(record_match("assign", vocabulary.records["assign"].fields), rest, path, node_id)
        if root == PORT_ROOT:
            # ``port.value`` is the port's own type where the mount says it; the rest is the record.
            value = self.scope.port
            overrides = {PORT_VALUE_FIELD: value.model_copy(update={"at": None, "nullable": True})} if value is not None else None
            return self.descend(record_match(PORT_ROOT, vocabulary.records[PORT_ROOT].fields, overrides), rest, path, node_id)
        if root == "viewer":
            return self.descend(record_match("viewer", vocabulary.records["viewer"].fields), rest, path, node_id)
        if root == "queries":
            if not rest:
                self.add("roots.whole", node_id, path=path, root="queries")
                return None
            if rest[0] not in self.queries:
                self.add("query.unknown", node_id, path=path, name=rest[0], names=", ".join(sorted(self.queries)) or "none")
                return None
            value = self.scope.query_values.get(rest[0])
            match = record_match(rest[0], vocabulary.records["query"].fields, {"value": value} if value is not None else None)
            return self.descend(match, rest[1:], path, node_id)
        if root == "subscriptions":
            if not rest:
                self.add("roots.whole", node_id, path=path, root="subscriptions")
                return None
            if rest[0] not in self.subscriptions:
                self.add("subscription.unknown", node_id, path=path, name=rest[0], names=", ".join(sorted(self.subscriptions)) or "none")
                return None
            return self.descend(record_match(rest[0], vocabulary.records["subscription"].fields), rest[1:], path, node_id)
        return open_match(path)

    def check_returns(self, path: str, rest: Sequence[str], node_id: str) -> None:
        """``<record>.returns.return0``: a return the action left unnamed (a warning)."""
        if len(rest) > 1 and rest[0] == RETURNS_FIELD and _UNNAMED_RETURN.fullmatch(rest[1]):
            self.add("returns.unnamed", node_id, path=path, port=rest[1])

    def event_field(self, rest: list[str], where: Where) -> Match:
        """What a callback is called with is its component's to say: any field is taken as written."""
        return open_match(rest[0] if rest else "event")

    def agent(self, dependency: AgentDependencyInputModel | None, key: str, rest: list[str], path: str, node_id: str) -> Match:
        """What a path below an agent (``self``, ``deps.<key>``) holds."""
        vocabulary = self.vocabulary
        if not rest:
            return open_match(key)
        head, tail = rest[0], rest[1:]
        if head in vocabulary.agent_fields:
            if tail:
                self.add("path.unknown", node_id, path=path, detail=f"{head} of an agent has no fields")
            return PortMatchInputModel(key=head, kind=enums.PortKind.BOOL if head == CONNECTED_FIELD else enums.PortKind.STRING)
        if head == vocabulary.tasks_segment:
            if not tail:
                self.add("roots.whole", node_id, path=path, root=f"{key}.tasks")
                return None
            action = tail[0]
            returns: list[PortMatchInputModel] | None = None
            if dependency is not None and dependency.action_dependencies is not None:
                demand = next((demand for demand in dependency.action_dependencies if demand.key == action), None)
                if demand is None:
                    self.add("deps.unknown_action", node_id, path=path, key=key, action=action, actions=", ".join(sorted(d.key for d in dependency.action_dependencies)) or "none")
                    return None
                returns = demand.demand.return_matches if demand.demand is not None else None
            self.check_returns(path, tail[1:], node_id)
            return self.descend(task_record_match(action, returns), tail[1:], path, node_id)
        if head == vocabulary.assets_segment:
            if len(tail) != 1:
                self.add("deps.asset", node_id, path=path)
                return None
            if key == "self" and self.scope.assets is not None and tail[0] not in self.scope.assets:
                self.add("deps.asset", node_id, path=path)
                return None
            return PortMatchInputModel(key=tail[0], kind=enums.PortKind.STRING)
        if dependency is None or dependency.state_dependencies is None:
            return open_match(path)
        state = next((demand for demand in dependency.state_dependencies if demand.key == head), None)
        if state is None:
            self.add("deps.unknown_state", node_id, path=path, key=key, state=head, states=", ".join(sorted(d.key for d in dependency.state_dependencies)) or "none")
            return None
        return self.descend(_state_match(state), tail, path, node_id)

    def descend(self, match: Match, rest: Sequence[str], path: str, node_id: str) -> Match:
        """Follow ``rest`` down ``match``'s children."""
        current = match
        for index, part in enumerate(rest):
            if current is None or is_open(current):
                return current
            if getattr(current.kind, "value", current.kind) == "STRUCTURE" and not current.children:
                # A structure reads below it as the reference it travels as.
                current = structure_match(current)
            children = current.children or []
            child = next((candidate for candidate in children if candidate.key == part), None)
            if child is None and len(children) == 1 and children[0].key == ITEM:
                child = children[0]
            if child is None:
                available = sorted(candidate.key for candidate in children if candidate.key is not None and candidate.key != ITEM)
                where = ".".join(rest[:index]) or current.key or "it"
                detail = f"{part!r} does not exist in {where}" + (f" (it has: {', '.join(available)})" if available else " (it has no fields)")
                self.add("path.unknown", node_id, path=path, detail=detail)
                return None
            current = child
        return current


def unportable_class(token: str, vocabulary: Vocabulary) -> str | None:
    """What of the class ``token`` is outside the portable subset (``the before variant``), or None."""
    *variants, utility = _split_class(token)
    for variant in variants:
        if variant.startswith("["):
            return "an arbitrary variant"
        name = variant.split("/", 1)[0]
        if name in vocabulary.unportable_variants or name.split("-[", 1)[0] in vocabulary.unportable_variants:
            return f"the {name} variant"
    utility = utility.lstrip("!-")
    if utility.startswith("["):
        return "an arbitrary property"
    if "[" in utility:
        prefix = utility.split("-[", 1)[0]
        if prefix not in vocabulary.arbitrary_values:
            return f"an arbitrary value of {prefix}"
        return None
    for prefix in vocabulary.unportable_utilities:
        if utility == prefix or utility.startswith(f"{prefix}-"):
            return f"the {prefix} utility"
    return None


def _split_class(token: str) -> list[str]:
    """A class at its variant colons, those inside brackets left alone (``[&>*]:p-2``)."""
    parts: list[str] = []
    depth = 0
    current = ""
    for character in token:
        if character == "[":
            depth += 1
        elif character == "]":
            depth = max(0, depth - 1)
        if character == ":" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += character
    parts.append(current)
    return parts


def dependency_hint(key: str, kind: str) -> str:
    """What a blok of ``kind`` that reads an undeclared ``deps.<key>`` does about it.

    - custom and task bloks declare it, to copy as it is: ``dependencies=[AgentDependencyInput(key="laser")]``;
    - an action blok's dependencies are its action's: ``add a dependency parameter `laser` to the action``;
    - an agent blok declares it in its own tree: ``<dependencies key="laser" like="self.<action>" />``.
    """
    if kind in ("action", "assign", "port"):
        return f"add a dependency parameter `{key}` to the action"
    if kind == "agent":
        return f'declare it in the blok: <dependencies key="{key}" like="self.<action>" />, then read {key}.<dependency>'
    return f'dependencies=[AgentDependencyInput(key="{key}")]'


def literal_hint(key: str, value: str, kind: str) -> str:
    """How a string on a typed prop is written as the literal it meant: ``fullWidth={true}``."""
    written = value.strip()
    if kind == "BOOL":
        literal = written.lower() if written.lower() in ("true", "false") else "true"
    else:
        try:
            number = float(written)
        except ValueError:
            number = 0.0
        if kind == "INT":
            literal = str(int(number))
        else:
            literal = str(int(number)) if number.is_integer() and "." not in written else repr(number)
    return f"{key}={{{literal}}}"


def _article(kind: str) -> str:
    return "n" if kind[:1] in "aeiou" else ""


def _argument_what(argument: ActionArgumentInputModel) -> str:
    if argument.value_path is not None:
        return f"the path {argument.value_path}"
    if argument.util_call is not None:
        return f"a call of {argument.util_call.operation}"
    if argument.agent_call is not None:
        return "a task call"
    if argument.value_list is not None or argument.value_dict is not None:
        return "a list or object"
    return f"the value {argument.value_literal!r}"


def _what(prop: ComponentPropInputModel) -> str:
    if prop.agent_call is not None:
        return "a task call"
    if prop.util_call is not None:
        return prop.util_call.operation
    if prop.dynamic_value is not None:
        return f"the path {prop.dynamic_value.path}"
    return f"the value {prop.static_value!r}"


def _class_literals(call: UtilCallInputModel) -> list[str]:
    """Every literal string of a ``str.classes`` call: list entries, object keys, branches."""
    literals: list[str] = []

    def visit_arguments(arguments: Iterable[ActionArgumentInputModel]) -> None:
        for argument in arguments:
            if isinstance(argument.value_literal, str):
                literals.append(argument.value_literal)
            for nested in argument.value_dict or ():
                if nested.key:
                    literals.append(nested.key)
            if argument.util_call is not None:
                visit_arguments(argument.util_call.arguments or ())
            visit_arguments(argument.value_list or ())

    visit_arguments(call.arguments or ())
    return literals


def _state_match(state: StateDependencyInputModel) -> PortMatchInputModel:
    return PortMatchInputModel(key=state.key, kind=enums.PortKind.DICT, children=state.demand.matches if state.demand else None)


def check_logo(tree: ComponentNodeInputModel, demo: BlokDemoInputModel | None = None) -> list[Finding]:
    """A logo is an icon: few nodes, shallow, little path data, and loops over few items."""
    limits = load_vocabulary().limits
    findings: list[Finding] = []
    nodes = 0
    path_data = 0

    def visit(current: ComponentNodeInputModel, depth: int) -> None:
        nonlocal nodes, path_data
        nodes += 1
        if depth > limits.logo_max_depth:
            findings.append(Finding.of("logo.complexity", current.id, detail=f"a logo nests at most {limits.logo_max_depth} deep"))
        for prop in current.props or ():
            if prop.key in limits.logo_path_data_props and isinstance(prop.static_value, str):
                path_data += len(prop.static_value)
            if current.component == FOREACH_COMPONENT and prop.key == FOREACH_ITEMS_PROP:
                items = _demo_items(prop, demo)
                if items > limits.logo_max_foreach_items:
                    findings.append(Finding.of("logo.complexity", current.id, detail=f"a foreach in a logo draws at most {limits.logo_max_foreach_items} items, its demo has {items}"))
        for child in current.children or ():
            visit(child, depth + 1)

    visit(tree, 1)
    if nodes > limits.logo_max_nodes:
        findings.append(Finding.of("logo.complexity", tree.id, detail=f"a logo has at most {limits.logo_max_nodes} components, this one {nodes}"))
    if path_data > limits.logo_max_path_data:
        findings.append(Finding.of("logo.complexity", tree.id, detail=f"a logo holds at most {limits.logo_max_path_data} characters of path data, this one {path_data}"))
    return findings


def _demo_items(prop: ComponentPropInputModel, demo: BlokDemoInputModel | None) -> int:
    if isinstance(prop.static_value, list):
        return len(prop.static_value)
    path = prop.dynamic_value.path if prop.dynamic_value is not None else None
    if path is None or demo is None:
        return 0
    parts = path.split(".")
    roots: dict[str, JSON] = {"task": demo.task, "args": demo.args}
    value: JSON
    if parts[0] == "self":
        deps = demo.deps if isinstance(demo.deps, dict) else {}
        value, parts = deps.get("self"), parts[1:]
    elif parts[0] == "deps":
        value, parts = demo.deps, parts[1:]
    else:
        value, parts = roots.get(parts[0]), parts[1:]
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            return 0
        value = value[part]
    return len(value) if isinstance(value, list) else 0


def check_blok_tree(tree: ComponentNodeInputModel, scope: BlokScope | None = None) -> list[Finding]:
    """Every finding of ``tree`` mounted as ``scope`` says: errors and warnings, in tree order. A
    root that is no readable ``<Blok>`` is the one finding.

    Its locals are resolved first (:func:`~rekuest_core.vocabulary.infer.resolve_locals`). No
    catalog is read: the language alone is checked here.

    Args:
        tree: The blok: its ``<Blok>``.
        scope: Where it is mounted (:class:`BlokScope`); left out, nowhere.
    """
    scope = scope or BlokScope()
    problem = header_problem(tree)
    if problem is not None:
        return [problem]
    header = blok_header(tree)
    scope = bound_scope(tree, scope)
    checker = _Checker(header, scope, resolve_locals(tree, scope.dependencies))
    checker.check_declarations(tree)
    walk_tree(tree, checker)
    if header.context == LOGO:
        checker.findings.extend(check_logo(tree, scope.demo))
    checker.finish()
    return checker.findings


def _spelled(context: str) -> str:
    """A context as a ``<Blok context>`` spells it (``PANEL`` -> ``panel``)."""
    return context.lower()


def _binding_what(name: str, binding: Binding, into: set[str]) -> str:
    if isinstance(binding, LocalBinding):
        return f"the local {binding.node}"
    if name in into:
        return "a call's record"
    return "a let"


__all__ = [
    "FETCH_MORE_FUNCTION",
    "OPEN_IDENTIFIER",
    "QUERY_SUGAR",
    "REFETCH_FUNCTION",
    "BlokScope",
    "LocalBinding",
    "check_blok_tree",
    "check_logo",
    "into_sites",
    "is_callback_value",
    "is_open",
    "iter_nodes",
    "literal_hint",
    "open_match",
    "port_match",
    "record_match",
    "task_record_match",
]

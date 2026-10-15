"""Validate blok manifests, port calls and port widgets against the base catalog and registered UI catalogs.

Every definition and blok is checked against the *base catalog* (``rekuest_core.catalogs``): the
pure operations every UI implements. A ``UICatalog`` a UI app has registered *extends* it with the
components it can render and further operations; it may not redefine base names.

Two kinds of finding:

- hard errors (``ValueError``, registration aborts): a known operation called with argument keys it
  does not accept or without a required one; an unknown component, prop or child once a catalog
  has registered components; two catalogs defining one operation or component differently.
- warnings (returned as :class:`DiagnosticModel`, registration succeeds, the caller stores them):
  an operation neither the base catalog nor the named catalogs provide. UI apps roll out new
  operations independently of agents, so this must not block registration.

Components are only checked once a named catalog has registered any (before that the catalog is
just a name), so a CUSTOM widget or blok component in an unregistered catalog is accepted silently.

Widgets are validated exactly like blok components: a CUSTOM widget is a one-node manifest, and
every call a widget carries (custom props, ``state_call``, state accessors, optimistic pointers)
goes through the same operation check as validators and effects.
"""

from typing import Iterable, Iterator, Mapping, Sequence

from facade import models
from rekuest_core import enums
from rekuest_core.catalogs import BASE_CATALOG_ID, base_operation_names, base_operations
from rekuest_core.inputs import models as rimodels
from rekuest_core.inputs.models import iter_util_calls
from rekuest_core.objects.models import DiagnosticModel

UNKNOWN_OPERATION = "unknown_operation"
# The one component a blok of each of these contexts may have as its root.
CONTEXT_ROOTS = {
    enums.ComponentContext.SCENE: "Scene",
    enums.ComponentContext.LOGO: "SVG",
    enums.ComponentContext.ACTION_LOGO: "ActionLogo",
}
# The blok contexts held to the size of an icon. ACTION_LOGO is a blok's context only: its
# shapes are logo components, placed under an ActionLogo as under an SVG.
LOGO_CONTEXTS = (enums.ComponentContext.LOGO, enums.ComponentContext.ACTION_LOGO)
# What an action logo paints with: the viewer's palette, or a url(#id) to a gradient or clip path.
LOGO_PAINT_TOKENS = ("ink", "muted", "brand", "brand-strong", "brand-soft", "surface", "none")
LOGO_PAINT_PROPS = ("fill", "stroke", "stopColor")
# A logo is an icon: what a logo blok may hold at most.
LOGO_MAX_NODES = 200
LOGO_MAX_DEPTH = 12
LOGO_MAX_PATH_DATA = 16 * 1024
LOGO_MAX_FOREACH_ITEMS = 64
LOGO_PATH_DATA_PROPS = ("d", "points")
Widget = rimodels._AssignWidgetBase | rimodels._ReturnWidgetBase


def resolve_operations(catalogs: Sequence[models.UICatalog]) -> dict[str, rimodels.CatalogOperationInputModel]:
    """The operations in force: the base catalog plus everything the given catalogs registered.

    Two catalogs may register the same operation only if they define it identically; a
    conflicting definition is a hard error, because the UI could not know which one to run.
    """
    operations = dict(base_operations())
    provider: dict[str, str] = {}
    for catalog in catalogs:
        for operation in catalog.get_operations():
            previous = operations.get(operation.name)
            if previous is not None and operation.name in provider and previous.model_dump() != operation.model_dump():
                raise ValueError(f"operation {operation.name!r} is defined differently by catalogs {provider[operation.name]!r} and {catalog.name!r}")
            operations[operation.name] = operation
            provider.setdefault(operation.name, catalog.name)
    return operations


def resolve_components(catalogs: Sequence[models.UICatalog]) -> dict[str, rimodels.CatalogComponentInputModel] | None:
    """The components in force: the union of what the given catalogs registered.

    ``None`` when no catalog has registered components yet, meaning component names cannot be
    checked at all (the base catalog has no components). Two catalogs may register the same
    component only if they define it identically.
    """
    components: dict[str, rimodels.CatalogComponentInputModel] = {}
    provider: dict[str, str] = {}
    registered = False
    for catalog in catalogs:
        if not catalog.components:
            continue
        registered = True
        for component in catalog.get_components():
            previous = components.get(component.name)
            if previous is not None and previous.model_dump() != component.model_dump():
                raise ValueError(f"component {component.name!r} is defined differently by catalogs {provider[component.name]!r} and {catalog.name!r}")
            components[component.name] = component
            provider.setdefault(component.name, catalog.name)
    return components if registered else None


def check_extension_does_not_shadow_base(operations: Iterable[rimodels.CatalogOperationInputModel], catalog_name: str) -> None:
    """A UI catalog may add operations but never redefine a base one."""
    shadowed = sorted({operation.name for operation in operations} & base_operation_names())
    if shadowed:
        raise ValueError(f"catalog {catalog_name!r} cannot redefine base operations {shadowed}")


def _catalog_label(catalogs: Sequence[models.UICatalog]) -> str:
    return " + ".join([BASE_CATALOG_ID, *(catalog.name for catalog in catalogs)])


def _check_call(call: rimodels.UtilCallInputModel, operations: dict[str, rimodels.CatalogOperationInputModel], catalog_label: str, owner: str) -> DiagnosticModel | None:
    """A known operation must be called with exactly its argument keys; an unknown one is a warning."""
    spec = operations.get(call.operation)
    if spec is None:
        return DiagnosticModel(
            level=enums.DiagnosticLevel.WARNING,
            code=UNKNOWN_OPERATION,
            message=f"{owner}: operation {call.operation!r} is not provided by the catalog ({catalog_label}); the UI cannot evaluate this call until it is registered",
            path=owner,
        )
    accepted = {argument.key: argument for argument in spec.arguments}
    passed = {argument.key for argument in call.arguments or []}
    unknown = sorted(key for key in passed - set(accepted) if key is not None)
    if unknown:
        raise ValueError(f"{owner}: operation {call.operation!r} does not accept arguments {unknown}")
    missing = sorted(key for key, argument in accepted.items() if argument.required and key not in passed)
    if missing:
        raise ValueError(f"{owner}: operation {call.operation!r} requires arguments {missing}")
    return None


def _check_calls(calls: Iterable[rimodels.UtilCallInputModel], operations: dict[str, rimodels.CatalogOperationInputModel], catalog_label: str, owner: str) -> list[DiagnosticModel]:
    diagnostics: list[DiagnosticModel] = []
    for call in calls:
        for candidate in (call, *iter_util_calls(call.arguments)):
            finding = _check_call(candidate, operations, catalog_label, owner)
            if finding is not None:
                diagnostics.append(finding)
    return diagnostics


def validate_calls_against_catalogs(catalogs: Sequence[models.UICatalog], calls: Iterable[rimodels.UtilCallInputModel], owner: str) -> list[DiagnosticModel]:
    """Check every call (and every call nested in its arguments) against base plus ``catalogs``.

    Returns the warnings for unknown operations; raises for argument-key mismatches on known ones
    and for operations the catalogs define differently.
    """
    return _check_calls(calls, resolve_operations(catalogs), _catalog_label(catalogs), owner)


def validate_calls_against_catalog(catalog: models.UICatalog | None, calls: Iterable[rimodels.UtilCallInputModel], owner: str) -> list[DiagnosticModel]:
    """Single-catalog form of :func:`validate_calls_against_catalogs` (``None`` means base only)."""
    return validate_calls_against_catalogs([] if catalog is None else [catalog], calls, owner)


def _check_component(
    node: rimodels.ComponentNodeInputModel,
    component_specs: dict[str, rimodels.CatalogComponentInputModel] | None,
    operations: dict[str, rimodels.CatalogOperationInputModel],
    catalog_label: str,
    owner: str,
) -> list[DiagnosticModel]:
    """One component node: structure against the registered specs (if any), calls against the operations."""
    spec = component_specs.get(node.component) if component_specs is not None else None
    if component_specs is not None:
        if spec is None:
            raise ValueError(f"{owner}: component {node.component!r} is not registered in catalog ({catalog_label})")
        if node.children and not spec.accepts_children:
            raise ValueError(f"{owner}: component {node.component!r} does not accept children")

        prop_specs = {prop.key: prop for prop in spec.props}
        present = {prop.key for prop in node.props or []}
        unknown = sorted(present - set(prop_specs))
        if unknown:
            raise ValueError(f"{owner}: component {node.component!r} has no props {unknown}")
        missing = sorted(key for key, prop in prop_specs.items() if prop.required and key not in present)
        if missing:
            raise ValueError(f"{owner}: component {node.component!r} requires props {missing}")

    diagnostics: list[DiagnosticModel] = []
    for prop in node.props or []:
        prop_owner = f"prop {prop.key!r} of {owner}"
        if spec is not None:
            kind = next((p.kind for p in spec.props if p.key == prop.key), None)
            if kind == enums.CatalogValueKind.CALLBACK and prop.agent_call is None and prop.util_call is None:
                raise ValueError(f"{prop_owner} is a CALLBACK prop and must be bound via agent_call or util_call")
            _check_asset_prop(kind, prop, prop_owner)
        diagnostics.extend(_check_calls(_prop_calls(prop), operations, catalog_label, prop_owner))
    return diagnostics


def _check_asset_prop(kind: enums.CatalogValueKind | None, prop: rimodels.ComponentPropInputModel, prop_owner: str) -> None:
    """An ASSET prop names a file an app registered: it is bound to ``<dependency>.assets.<name>``."""
    if kind != enums.CatalogValueKind.ASSET:
        return
    path = prop.dynamic_value.path if prop.dynamic_value is not None else None
    if path is None or rimodels.blok_asset_path(path) is None:
        raise ValueError(f"{prop_owner} is an ASSET prop and must name an asset the app registered, as in {prop.key}=\"@self.assets.<name>\"")


def validate_components_against_catalogs(
    catalogs: Sequence[models.UICatalog],
    components: list[rimodels.ComponentNodeInputModel] | None,
    context: enums.ComponentContext = enums.ComponentContext.HTML,
    demo_state: Mapping[str, object] | None = None,
) -> list[DiagnosticModel]:
    """Every component, prop and operation a component tree uses is provided by base plus ``catalogs``.

    ``context`` is the blok's own: a scene blok has one root, the scene, a logo blok one
    ``SVG`` and an action logo one ``ActionLogo``, whatever is registered. Both logos are held
    to the size of an icon (:func:`check_logo_complexity`), their loops counted in
    ``demo_state``, and an action logo to the palette (:func:`check_logo_palette`).
    """
    operations = resolve_operations(catalogs)
    component_specs = resolve_components(catalogs)
    catalog_label = _catalog_label(catalogs)
    diagnostics: list[DiagnosticModel] = []

    def visit(nodes: list[rimodels.ComponentNodeInputModel] | None, ancestors: tuple[str, ...], context: enums.ComponentContext) -> None:
        for node in nodes or []:
            owner = f"component {node.id!r}"
            diagnostics.extend(_check_component(node, component_specs, operations, catalog_label, owner))
            inner = _check_placement(node.component, ancestors, context, component_specs, owner)
            visit(node.children, (*ancestors, node.component), inner)

    root = CONTEXT_ROOTS.get(context)
    if root is not None:
        roots = components or []
        kind = context.value.lower()
        if len(roots) != 1:
            raise ValueError(f"a {kind} blok has exactly one root, a {root!r}, not {len(roots)} roots")
        if roots[0].component != root:
            raise ValueError(f"component {roots[0].id!r}: the root of a {kind} blok must be a {root!r}, not {roots[0].component!r}")
    if context in LOGO_CONTEXTS:
        check_logo_complexity(components or [], demo_state)
    if context == enums.ComponentContext.ACTION_LOGO:
        check_logo_palette(components or [], demo_state)
    visit(components, (), enums.ComponentContext.HTML)
    return diagnostics


def check_logo_complexity(components: list[rimodels.ComponentNodeInputModel], demo_state: Mapping[str, object] | None = None) -> None:
    """A logo has at most LOGO_MAX_NODES components, nests at most LOGO_MAX_DEPTH deep, holds at most
    LOGO_MAX_PATH_DATA characters of path data, and a foreach in it draws at most LOGO_MAX_FOREACH_ITEMS
    items of the blok's demo state (a list it does not find there draws nothing)."""
    nodes = 0
    path_data = 0

    def visit(node: rimodels.ComponentNodeInputModel, depth: int) -> None:
        nonlocal nodes, path_data
        nodes += 1
        if depth > LOGO_MAX_DEPTH:
            raise ValueError(f"component {node.id!r}: a logo nests at most {LOGO_MAX_DEPTH} deep")
        for prop in node.props or []:
            if prop.key in LOGO_PATH_DATA_PROPS and isinstance(prop.static_value, str):
                path_data += len(prop.static_value)
            if node.component == "foreach" and prop.key == "items":
                items = _demo_items(prop, demo_state)
                if items > LOGO_MAX_FOREACH_ITEMS:
                    raise ValueError(f"component {node.id!r}: a foreach in a logo draws at most {LOGO_MAX_FOREACH_ITEMS} items, its demo state has {items}")
        for child in node.children or []:
            visit(child, depth + 1)

    for root in components:
        visit(root, 1)
    if nodes > LOGO_MAX_NODES:
        raise ValueError(f"a logo has at most {LOGO_MAX_NODES} components, this one {nodes}")
    if path_data > LOGO_MAX_PATH_DATA:
        raise ValueError(f"a logo holds at most {LOGO_MAX_PATH_DATA} characters of path data, this one {path_data}")


def check_logo_palette(components: list[rimodels.ComponentNodeInputModel], demo_state: Mapping[str, object] | None = None) -> None:
    """An action logo paints only with LOGO_PAINT_TOKENS or a url(#id): a static paint as written, a
    bound one as the demo state resolves it, for every item when it reads a foreach's let. A path the
    demo state does not hold draws nothing and is not checked."""

    def visit(node: rimodels.ComponentNodeInputModel, scope: Mapping[str, list[object]]) -> None:
        for prop in node.props or []:
            if prop.key not in LOGO_PAINT_PROPS:
                continue
            if prop.static_value is not None:
                values: list[object] = [prop.static_value]
            elif prop.dynamic_value is not None and prop.dynamic_value.path:
                values = _resolve(prop.dynamic_value.path, demo_state, scope)
            else:
                values = []
            for value in values:
                if not is_logo_paint(value):
                    raise ValueError(f"component {node.id!r}: a logo paints with {'|'.join(LOGO_PAINT_TOKENS)}|url(#…), not {value!r} in {prop.key!r}")
        inner = scope
        if node.component == "foreach":
            let = next((prop.declares_value for prop in node.props or [] if prop.key == "let" and prop.declares_value), None)
            items = next((prop.dynamic_value.path for prop in node.props or [] if prop.key == "items" and prop.dynamic_value is not None and prop.dynamic_value.path), None)
            if let and items:
                inner = {**scope, let: [item for found in _resolve(items, demo_state, scope) if isinstance(found, list) for item in found]}
        for child in node.children or []:
            visit(child, inner)

    for root in components:
        visit(root, {})


def is_logo_paint(value: object) -> bool:
    """A palette token, or a url(#id) reference to a gradient or clip path."""
    if not isinstance(value, str):
        return False
    paint = value.strip()
    return paint in LOGO_PAINT_TOKENS or (paint.startswith("url(#") and paint.endswith(")"))


def _resolve(path: str, demo_state: Mapping[str, object] | None, scope: Mapping[str, list[object]]) -> list[object]:
    """What ``path`` holds: one value from the demo state, or one per item of a foreach's let."""
    segments = [segment for segment in path.replace("/", ".").split(".") if segment]
    if not segments:
        return []
    roots: list[object]
    if segments[0] in scope:
        roots, segments = list(scope[segments[0]]), segments[1:]
    else:
        roots = [demo_state or {}]
    found: list[object] = []
    for value in roots:
        for segment in segments:
            if not isinstance(value, Mapping) or segment not in value:
                break
            value = value[segment]
        else:
            found.append(value)
    return found


def _demo_items(prop: rimodels.ComponentPropInputModel, demo_state: Mapping[str, object] | None) -> int:
    """How many items a foreach's ``items`` gives in the demo state; 0 when it names none."""
    if isinstance(prop.static_value, list):
        return len(prop.static_value)
    path = prop.dynamic_value.path if prop.dynamic_value is not None else None
    value: object = demo_state or {}
    for segment in (path or "").replace("/", ".").split("."):
        if not segment:
            continue
        if not isinstance(value, Mapping) or segment not in value:
            return 0
        value = value[segment]
    return len(value) if isinstance(value, list) else 0


def _check_placement(
    component: str,
    ancestors: tuple[str, ...],
    context: enums.ComponentContext,
    component_specs: dict[str, rimodels.CatalogComponentInputModel] | None,
    owner: str,
) -> enums.ComponentContext:
    """Where a component sits in the tree, against what it declares; returns the context of its children.

    A tree starts in the html context. A component whose ``children_context`` differs from its
    own bridges two renderers, and everything below it is checked in the new one. One that
    ``inherits_context`` sits in whichever context is in force and passes it on.
    """
    spec = component_specs.get(component) if component_specs is not None else None
    if spec is None:
        return context
    if not spec.inherits_context and spec.context != context:
        raise ValueError(f"{owner}: component {component!r} is a {spec.context.value.lower()} component and cannot be placed in the {context.value.lower()} context")
    if spec.downstream_of and not set(spec.downstream_of) & set(ancestors):
        raise ValueError(f"{owner}: component {component!r} must be inside one of {sorted(spec.downstream_of)}")
    forbidden = sorted(set(spec.not_downstream_of) & set(ancestors))
    if forbidden:
        raise ValueError(f"{owner}: component {component!r} must not be inside {forbidden}")
    if spec.inherits_context:
        return context
    return spec.children_context or spec.context


def validate_manifest_against_catalog(
    catalog: models.UICatalog,
    components: list[rimodels.ComponentNodeInputModel] | None,
    context: enums.ComponentContext = enums.ComponentContext.HTML,
    demo_state: Mapping[str, object] | None = None,
) -> list[DiagnosticModel]:
    """Single-catalog form of :func:`validate_components_against_catalogs` for blok manifests."""
    return validate_components_against_catalogs([catalog], components, context, demo_state)


def _prop_calls(prop: rimodels.ComponentPropInputModel) -> Iterator[rimodels.UtilCallInputModel]:
    if prop.util_call is not None:
        yield prop.util_call
    if prop.agent_call is not None:
        yield from iter_util_calls(prop.agent_call.arguments)


# --------------------------------------------------------------------------- widgets


def iter_widget_calls(widget: Widget) -> Iterator[rimodels.UtilCallInputModel]:
    """Every call a widget carries besides its CUSTOM props: STATE_CHOICE pointers and accessors."""
    if isinstance(widget, rimodels.StateChoiceAssignWidgetInputModel):
        if widget.state_call is not None:
            yield widget.state_call
        for accessor in widget.state_accessors or []:
            if accessor.call is not None:
                yield accessor.call


def validate_widgets_against_catalogs(catalogs: Sequence[models.UICatalog], widgets: Iterable[tuple[str, Widget]]) -> list[DiagnosticModel]:
    """Widgets are validated like blok components: a CUSTOM widget is a one-node manifest, and every call it carries is checked."""
    operations = resolve_operations(catalogs)
    component_specs = resolve_components(catalogs)
    catalog_label = _catalog_label(catalogs)
    diagnostics: list[DiagnosticModel] = []
    for owner, widget in widgets:
        if isinstance(widget, (rimodels.CustomAssignWidgetInputModel, rimodels.CustomReturnWidgetInputModel)):
            node = rimodels.ComponentNodeInputModel(id="widget", component=widget.component, props=widget.props)
            diagnostics.extend(_check_component(node, component_specs, operations, catalog_label, f"widget of {owner}"))
        diagnostics.extend(_check_calls(iter_widget_calls(widget), operations, catalog_label, f"widget of {owner}"))
    return diagnostics


def dump_diagnostics(diagnostics: Iterable[DiagnosticModel]) -> list[dict]:
    """Diagnostics as the JSON stored on Implementation/Blok rows."""
    return [diagnostic.model_dump(mode="json") for diagnostic in diagnostics]

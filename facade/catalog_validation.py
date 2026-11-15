"""Check blok trees against the blok language, and port calls and port widgets against base@1.

**Blok trees** (blok language v2) are held to the language alone, by ``rekuest_core.vocabulary``'s
checker: what a tree reads and calls resolves against what its blok declares and its kind. The
server reads no catalog. What a component takes, where it may sit, which catalog it is of and
whether a frontend draws it is checked by the frontend that draws the blok, and by an author's
tooling. The scope is resolved first (``rekuest_core.vocabulary.infer``): the tree is checked
against its locals resolved, which are stored beside the locals as written
(``resolved_locals``); a local only a control's ``bind`` would type stays ``ANY``, and
dependencies are never inferred. Every finding carries its vocabulary code (``roots.unknown``,
``palette.raw_colour``, ...): an error refuses the blok (``BlokError``), a warning is stored
with it (``Blok.diagnostics``) and shown by the UI.

**Port calls and widgets** (effects, validators, CUSTOM widgets) feed an action's definition hash,
so they stay on the base@1 catalog (``rekuest_core.catalogs``): a ``Module`` a UI app has
registered *extends* it with the components it can render and further operations, and may not
redefine base names. Those findings are warnings with the codes below; nothing there refuses a
definition except two catalogs defining one operation differently where no findings are collected.

The same rules run in takt (``crates/facade/src/catalog_validation.rs``), which registers agents'
bloks; the spec corpus (``bsx_v2.json``) holds both blok checkers to what it says the language
alone finds.
"""

from typing import Iterable, Iterator, Sequence

from facade import models
from rekuest_core import enums
from rekuest_core.catalogs import BASE_CATALOG_ID, base_component_names, base_components, base_operation_names, base_operations
from rekuest_core.inputs import models as rimodels
from rekuest_core.inputs.models import iter_util_calls
from rekuest_core.objects.models import DiagnosticModel
from rekuest_core.vocabulary import BlokError, BlokScope, Finding, check_blok_tree, resolve_scope

UNKNOWN_OPERATION = "unknown_operation"
OPERATION_ARGUMENTS = "operation_arguments"
CATALOG_CONFLICT = "catalog_conflict"
UNKNOWN_COMPONENT = "unknown_component"
UNKNOWN_PROP = "unknown_prop"
MISSING_PROP = "missing_prop"
CHILDREN_NOT_ACCEPTED = "children_not_accepted"
UNBOUND_CALLBACK = "unbound_callback"
MISPLACED_COMPONENT = "misplaced_component"


def _warning(code: str, message: str, path: str | None) -> DiagnosticModel:
    return DiagnosticModel(level=enums.DiagnosticLevel.WARNING, code=code, message=message, path=path)
Widget = rimodels._AssignWidgetBase | rimodels._ReturnWidgetBase


def resolve_operations(
    catalogs: Sequence[models.Module], findings: list[DiagnosticModel] | None = None
) -> dict[str, rimodels.CatalogOperationInputModel]:
    """The operations in force: the base catalog plus everything the given catalogs registered.

    Two catalogs may register the same operation only if they define it identically. A
    conflicting definition is reported in ``findings`` (``catalog_conflict``) and the first
    catalog's is kept; without ``findings`` it is raised.
    """
    operations = dict(base_operations())
    provider: dict[str, str] = {}
    for catalog in catalogs:
        for operation in catalog.get_operations():
            # Base wins: a catalog registered before an operation joined base still lists it.
            if operation.name in base_operation_names():
                continue
            previous = operations.get(operation.name)
            if previous is not None and operation.name in provider and previous.model_dump() != operation.model_dump():
                message = f"operation {operation.name!r} is defined differently by catalogs {provider[operation.name]!r} and {catalog.name!r}"
                if findings is None:
                    raise ValueError(message)
                findings.append(_warning(CATALOG_CONFLICT, message, None))
                continue
            operations[operation.name] = operation
            provider.setdefault(operation.name, catalog.name)
    return operations


def resolve_components(
    catalogs: Sequence[models.Module], findings: list[DiagnosticModel] | None = None
) -> dict[str, rimodels.CatalogComponentInputModel] | None:
    """The components in force: the base catalog's plus the union of what the given catalogs registered.

    ``None`` when no catalog has registered components yet, meaning component names cannot be
    checked at all (the base catalog's control flow alone says nothing about what a UI draws).
    Two catalogs may register the same component only if they define it identically; a conflict
    is reported or raised as in :func:`resolve_operations`.
    """
    components: dict[str, rimodels.CatalogComponentInputModel] = dict(base_components())
    provider: dict[str, str] = {name: BASE_CATALOG_ID for name in components}
    registered = False
    for catalog in catalogs:
        if not catalog.components:
            continue
        registered = True
        for component in catalog.get_components():
            # Base wins, as for operations: an older registration may still list it.
            if component.name in components and provider[component.name] == BASE_CATALOG_ID:
                continue
            previous = components.get(component.name)
            if previous is not None and previous.model_dump() != component.model_dump():
                message = f"component {component.name!r} is defined differently by catalogs {provider[component.name]!r} and {catalog.name!r}"
                if findings is None:
                    raise ValueError(message)
                findings.append(_warning(CATALOG_CONFLICT, message, None))
                continue
            components[component.name] = component
            provider.setdefault(component.name, catalog.name)
    return components if registered else None


def check_extension_does_not_shadow_base(
    operations: Iterable[rimodels.CatalogOperationInputModel],
    catalog_name: str,
    components: Iterable[rimodels.CatalogComponentInputModel] = (),
) -> None:
    """A UI catalog may add operations and components but never redefine a base one."""
    shadowed = sorted({operation.name for operation in operations} & base_operation_names())
    if shadowed:
        raise ValueError(f"catalog {catalog_name!r} cannot redefine base operations {shadowed}")
    shadowed = sorted({component.name for component in components} & base_component_names())
    if shadowed:
        raise ValueError(f"catalog {catalog_name!r} cannot redefine base components {shadowed}")


def _catalog_label(catalogs: Sequence[models.Module]) -> str:
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
        return _warning(OPERATION_ARGUMENTS, f"{owner}: operation {call.operation!r} does not accept arguments {unknown}", owner)
    missing = sorted(key for key, argument in accepted.items() if argument.required and key not in passed)
    if missing:
        return _warning(OPERATION_ARGUMENTS, f"{owner}: operation {call.operation!r} requires arguments {missing}", owner)
    return None


def _check_calls(calls: Iterable[rimodels.UtilCallInputModel], operations: dict[str, rimodels.CatalogOperationInputModel], catalog_label: str, owner: str) -> list[DiagnosticModel]:
    diagnostics: list[DiagnosticModel] = []
    for call in calls:
        for candidate in (call, *iter_util_calls(call.arguments)):
            finding = _check_call(candidate, operations, catalog_label, owner)
            if finding is not None:
                diagnostics.append(finding)
    return diagnostics


def validate_calls_against_catalogs(catalogs: Sequence[models.Module], calls: Iterable[rimodels.UtilCallInputModel], owner: str) -> list[DiagnosticModel]:
    """Check every call (and every call nested in its arguments) against base plus ``catalogs``.

    Returns the findings: unknown operations, argument-key mismatches on known ones, and
    operations the catalogs define differently.
    """
    findings: list[DiagnosticModel] = []
    operations = resolve_operations(catalogs, findings)
    return [*findings, *_check_calls(calls, operations, _catalog_label(catalogs), owner)]


def validate_calls_against_catalog(catalog: models.Module | None, calls: Iterable[rimodels.UtilCallInputModel], owner: str) -> list[DiagnosticModel]:
    """Single-catalog form of :func:`validate_calls_against_catalogs` (``None`` means base only)."""
    return validate_calls_against_catalogs([] if catalog is None else [catalog], calls, owner)


def _check_component(
    node: rimodels.ComponentNodeInputModel,
    component_specs: dict[str, rimodels.CatalogComponentInputModel] | None,
    operations: dict[str, rimodels.CatalogOperationInputModel],
    catalog_label: str,
    owner: str,
) -> list[DiagnosticModel]:
    """One component node: structure against the registered specs (if any), calls against the operations.

    What the catalog says of the node is a finding; only an ASSET prop that names no asset is raised.
    """
    spec = component_specs.get(node.component) if component_specs is not None else None
    diagnostics: list[DiagnosticModel] = []
    if component_specs is not None:
        if spec is None:
            diagnostics.append(_warning(UNKNOWN_COMPONENT, f"{owner}: component {node.component!r} is not registered in catalog ({catalog_label})", owner))
        else:
            if node.children and not spec.accepts_children:
                diagnostics.append(_warning(CHILDREN_NOT_ACCEPTED, f"{owner}: component {node.component!r} does not accept children", owner))
            prop_specs = {prop.key: prop for prop in spec.props}
            present = {prop.key for prop in node.props or []}
            unknown = sorted(present - set(prop_specs))
            if unknown:
                diagnostics.append(_warning(UNKNOWN_PROP, f"{owner}: component {node.component!r} has no props {unknown}", owner))
            missing = sorted(key for key, prop in prop_specs.items() if prop.required and key not in present)
            if missing:
                diagnostics.append(_warning(MISSING_PROP, f"{owner}: component {node.component!r} requires props {missing}", owner))

    for prop in node.props or []:
        prop_owner = f"prop {prop.key!r} of {owner}"
        if spec is not None:
            kind = next((p.kind for p in spec.props if p.key == prop.key), None)
            if kind == enums.CatalogValueKind.CALLBACK and prop.agent_call is None and prop.util_call is None:
                diagnostics.append(_warning(UNBOUND_CALLBACK, f"{prop_owner} is a CALLBACK prop and must be bound via agent_call or util_call", prop_owner))
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


# --------------------------------------------------------------------------- blok trees (v2)


def resolved_scope(module: models.Module | None, components: list[rimodels.ComponentNodeInputModel] | None, scope: BlokScope) -> BlokScope:
    """``scope`` resolved against its one-root tree: each local's left-out kind (stored as the
    blok's ``resolved_locals``). Unchanged without one root. ``module`` is not read: no catalog is."""
    roots = components or []
    if len(roots) != 1:
        return scope
    return resolve_scope(roots[0], scope)


def diagnostic_of(finding: Finding) -> DiagnosticModel:
    """A finding as a blok row stores it."""
    level = enums.DiagnosticLevel.ERROR if finding.level == "error" else enums.DiagnosticLevel.WARNING
    return DiagnosticModel(level=level, code=finding.code, message=finding.message, path=finding.path)


def blok_findings_against_module(
    module: models.Module | None,
    components: list[rimodels.ComponentNodeInputModel] | None,
    scope: BlokScope,
) -> list[DiagnosticModel]:
    """Every finding of a v2 tree against ``scope``, errors included and nothing refused: what an
    editor's dry run shows of the language. ``module`` is not read: the frontend that draws the
    blok checks it against its catalog."""
    roots = components or []
    if len(roots) != 1:
        raise ValueError(f"a blok has exactly one root, not {len(roots)}")
    return [diagnostic_of(finding) for finding in check_blok_tree(roots[0], scope)]


def validate_blok_against_module(
    module: models.Module | None,
    components: list[rimodels.ComponentNodeInputModel] | None,
    scope: BlokScope,
) -> list[DiagnosticModel]:
    """A v2 blok tree against what its blok declares (``scope``): the language alone.

    Refused (``BlokError``, with the vocabulary code) on its first error; its warnings returned.
    ``module`` is not read: no catalog is.
    """
    roots = components or []
    if len(roots) != 1:
        raise ValueError(f"a blok has exactly one root, not {len(roots)}")
    findings = check_blok_tree(roots[0], scope)
    for finding in findings:
        if finding.level == "error":
            raise BlokError.from_finding(finding)
    return [diagnostic_of(finding) for finding in findings]


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


def validate_widgets_against_catalogs(catalogs: Sequence[models.Module], widgets: Iterable[tuple[str, Widget]]) -> list[DiagnosticModel]:
    """Widgets are validated like blok components: a CUSTOM widget is a manifest rooted at its component, and every call it carries is checked."""
    diagnostics: list[DiagnosticModel] = []
    operations = resolve_operations(catalogs, diagnostics)
    component_specs = resolve_components(catalogs, diagnostics)
    catalog_label = _catalog_label(catalogs)
    for owner, widget in widgets:
        if isinstance(widget, (rimodels.CustomAssignWidgetInputModel, rimodels.CustomReturnWidgetInputModel)):
            root = rimodels.ComponentNodeInputModel(id="widget", component=widget.component, props=widget.props, children=getattr(widget, "children", None))
            for node in rimodels.iter_component_nodes([root]):
                diagnostics.extend(_check_component(node, component_specs, operations, catalog_label, f"widget of {owner}"))
        diagnostics.extend(_check_calls(iter_widget_calls(widget), operations, catalog_label, f"widget of {owner}"))
    return diagnostics


def dump_diagnostics(diagnostics: Iterable[DiagnosticModel]) -> list[dict]:
    """Diagnostics as the JSON stored on Implementation/Blok rows."""
    return [diagnostic.model_dump(mode="json") for diagnostic in diagnostics]

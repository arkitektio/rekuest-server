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

from typing import Iterable, Iterator, Sequence

from facade import models
from rekuest_core import enums
from rekuest_core.catalogs import BASE_CATALOG_ID, base_operation_names, base_operations
from rekuest_core.inputs import models as rimodels
from rekuest_core.inputs.models import iter_component_nodes, iter_util_calls
from rekuest_core.objects.models import DiagnosticModel

UNKNOWN_OPERATION = "unknown_operation"
UNKNOWN_CATALOG = "unknown_catalog"

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
    unknown = sorted(passed - set(accepted))
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
        diagnostics.extend(_check_calls(_prop_calls(prop), operations, catalog_label, prop_owner))
    return diagnostics


def validate_components_against_catalogs(catalogs: Sequence[models.UICatalog], components: list[rimodels.ComponentNodeInputModel] | None) -> list[DiagnosticModel]:
    """Every component, prop and operation a component tree uses is provided by base plus ``catalogs``."""
    operations = resolve_operations(catalogs)
    component_specs = resolve_components(catalogs)
    catalog_label = _catalog_label(catalogs)
    diagnostics: list[DiagnosticModel] = []
    for node in iter_component_nodes(components):
        diagnostics.extend(_check_component(node, component_specs, operations, catalog_label, f"component {node.id!r}"))
    return diagnostics


def validate_manifest_against_catalog(catalog: models.UICatalog, components: list[rimodels.ComponentNodeInputModel] | None) -> list[DiagnosticModel]:
    """Single-catalog form of :func:`validate_components_against_catalogs` for blok manifests."""
    return validate_components_against_catalogs([catalog], components)


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
        if widget.kind == "CUSTOM":
            node = rimodels.ComponentNodeInputModel(id="widget", component=widget.component, props=widget.props)
            diagnostics.extend(_check_component(node, component_specs, operations, catalog_label, f"widget of {owner}"))
        diagnostics.extend(_check_calls(iter_widget_calls(widget), operations, catalog_label, f"widget of {owner}"))
    return diagnostics


def dump_diagnostics(diagnostics: Iterable[DiagnosticModel]) -> list[dict]:
    """Diagnostics as the JSON stored on Implementation/Blok rows."""
    return [diagnostic.model_dump(mode="json") for diagnostic in diagnostics]

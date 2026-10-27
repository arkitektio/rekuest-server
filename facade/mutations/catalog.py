"""Module registration: an app's own UI extras."""

from typing import cast

from kante.types import Info

from facade import inputs, models, types
from facade.catalog_validation import check_extension_does_not_shadow_base, validate_widgets_against_catalogs


def register_module(info: Info, input: inputs.RegisterModuleInput) -> types.Module:
    """Upsert a module by name in the caller's organization with the components, operations, default widgets and catalogs a UI app announces."""
    model = input.to_pydantic()
    check_extension_does_not_shadow_base(model.operations, model.name, model.components)

    components = [component.model_dump() for component in model.components]
    operations = [operation.model_dump() for operation in model.operations]

    # A default widget is validated against the module being registered (plus base). A UI that
    # announces a default it cannot render is a client bug, so warnings are errors here.
    transient = models.Module(name=model.name, components=components, operations=operations)
    widgets = [(f"widget_defaults[{index}] ({default.kind.value if default.kind else '*'}, {default.identifier or '*'})", widget) for index, default in enumerate(model.widget_defaults) for widget in (default.widget, default.return_widget) if widget is not None]
    for diagnostic in validate_widgets_against_catalogs([transient], widgets):
        raise ValueError(f"module {model.name!r}: {diagnostic.message}")

    module, _ = models.Module.objects.update_or_create(
        name=model.name,
        organization=info.context.request.organization,
        defaults=dict(
            description=model.description,
            components=components,
            operations=operations,
            widget_defaults=[default.model_dump(mode="json") for default in model.widget_defaults],
            catalogs=list(model.catalogs),
            registered_by=info.context.request.client,
        ),
    )
    return cast("types.Module", module)

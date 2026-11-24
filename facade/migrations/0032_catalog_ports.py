"""A module's stored props and operations are typed by catalog ports, not a flat value kind.

STRING/INT/FLOAT/BOOL keep their kind (``values`` become choices), CALLBACK takes its ``event``
fields as children, ASSET stays, a ``bind`` prop becomes a BINDING of what it wrote; LIST and
DICT get an open ``...`` item, ANY is open. A component prop has no lossless port for a LIST,
DICT or ANY (a renderer types every prop), so a module holding one keeps no components until
its app registers again; its operations are converted. Irreversible.
"""

from django.db import migrations

SCALARS = ("STRING", "INT", "FLOAT", "BOOL")
LOSSY = ("LIST", "DICT", "ANY")
ITEM = "..."


def port(
    kind: str | None, values: list | None = None, event: list | None = None
) -> dict | None:
    """The port of an old value kind; None is open."""
    if kind is None or kind == "ANY":
        return None
    if kind in ("LIST", "DICT"):
        return {"kind": kind, "children": [{"key": ITEM, "kind": None}]}
    out: dict = {"kind": kind}
    if kind in SCALARS and values:
        out["choices"] = [{"value": value} for value in values]
    if kind == "CALLBACK" and event:
        out["children"] = [
            child(field["key"], field.get("kind"), field.get("description"))
            for field in event
        ]
    return out


def child(key: str, kind: str | None, description: str | None = None) -> dict:
    """A child port: its key, the port's fields, its description."""
    return {
        "key": key,
        **(port(kind) or {"kind": None}),
        **({"description": description} if description else {}),
    }


def prop_port(prop: dict) -> dict | None:
    if prop.get("key") == "bind":
        return {"kind": "BINDING", "children": [child(ITEM, prop.get("kind"))]}
    return port(prop.get("kind"), prop.get("values"), prop.get("event"))


def is_lossy(prop: dict) -> bool:
    """Whether the prop, or a field of its event, is a LIST, DICT or ANY."""
    kinds = [
        prop.get("kind"),
        *(field.get("kind") for field in prop.get("event") or []),
    ]
    return any(kind in LOSSY for kind in kinds)


def converted_prop(prop: dict) -> dict:
    if "kind" not in prop:
        return prop
    out = {
        key: value
        for key, value in prop.items()
        if key not in ("kind", "event", "values")
    }
    out["port"] = prop_port(prop)
    return out


def converted_components(components: list) -> list:
    """The components with ported props; [] when one prop has no lossless port."""
    props = [
        prop
        for component in components or []
        for prop in component.get("props") or []
        if "kind" in prop
    ]
    if any(is_lossy(prop) for prop in props):
        return []
    return [
        {
            **component,
            "props": [converted_prop(prop) for prop in component.get("props") or []],
        }
        for component in components or []
    ]


def converted_operations(operations: list) -> list:
    out = []
    for operation in operations or []:
        arguments = [
            {
                **{k: v for k, v in argument.items() if k != "kind"},
                "port": port(argument.get("kind")),
            }
            if "kind" in argument
            else argument
            for argument in operation.get("arguments") or []
        ]
        returns = operation.get("returns")
        out.append(
            {
                **operation,
                "arguments": arguments,
                "returns": port(returns) if isinstance(returns, str) else returns,
            }
        )
    return out


def forwards(apps, schema_editor):  # noqa: ANN001, ANN201  Django's signature
    Module = apps.get_model("facade", "Module")
    for module in Module.objects.all().iterator():
        components = converted_components(module.components)
        operations = converted_operations(module.operations)
        if components != module.components or operations != module.operations:
            module.components = components
            module.operations = operations
            module.save(update_fields=["components", "operations"])


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0031_agent_supervisor_from_token"),
    ]

    operations = [
        # Irreversible: the old kinds are not kept.
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

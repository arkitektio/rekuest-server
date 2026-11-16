"""A module's stored components speak the contexts of the self-contained blok root.

0017 deleted every blok, but a module (an app's or a frontend's UI catalog, registered with
``registerModule``) keeps its components until it registers again, and takt refuses to parse a
stored component whose ``context`` or ``children_context`` is ``HTML`` or ``ACTION_LOGO``. Those
become PANEL and LOGO, and a module claiming the base catalog ``base@3`` claims ``base@4``.
"""

from django.db import migrations

RENAMED = {"HTML": "PANEL", "ACTION_LOGO": "LOGO"}
CONTEXT_KEYS = ("context", "children_context")
RENAMED_CATALOGS = {"base@3": "base@4"}


def renamed(components: list) -> list:
    """``components`` with every old context renamed."""
    out = []
    for component in components or []:
        if isinstance(component, dict):
            component = {**component, **{key: RENAMED[component[key]] for key in CONTEXT_KEYS if component.get(key) in RENAMED}}
        out.append(component)
    return out


def forwards(apps, schema_editor):  # noqa: ANN001, ANN201  Django's signature
    """Rename the stored contexts and the base catalog every module claims."""
    Module = apps.get_model("facade", "Module")
    for module in Module.objects.all().iterator():
        components = renamed(module.components)
        catalogs = [RENAMED_CATALOGS.get(catalog, catalog) for catalog in module.catalogs or []]
        if components != module.components or catalogs != module.catalogs:
            module.components = components
            module.catalogs = catalogs
            module.save(update_fields=["components", "catalogs"])


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0018_blok_self_contained_columns"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

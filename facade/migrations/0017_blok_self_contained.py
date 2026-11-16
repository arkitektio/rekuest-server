"""Every stored blok goes: blok language v2 makes each one a self-contained ``<Blok kind context>``.

Before, a blok's root named its context (``<Html>``, ``<Scene>``/``<Space>``, ``<AgentLogo>``,
``<ActionLogo>``) and its locals, queries and subscriptions were columns beside the tree. Nothing
stored in that shape is rewritten: every blok is deleted, and with it its revisions, action bloks,
arg ports, dependencies and materializations (CASCADE), and the interactions that show it
(PROTECT, so they go first). An agent's bloks come back when it registers with an SDK that speaks
v2; a custom blok is written again.

The columns ``locals``, ``queries`` and ``subscriptions`` go in 0018: Postgres refuses to alter a
table whose rows this transaction changed while deferred constraint triggers are pending.
"""

from django.db import migrations


def forwards(apps, schema_editor):  # noqa: ANN001, ANN201  Django's signature
    """Delete every blok, the interactions that would protect one first."""
    apps.get_model("facade", "ImplementationInteraction").objects.all().delete()
    apps.get_model("facade", "Blok").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0016_protocol_2"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

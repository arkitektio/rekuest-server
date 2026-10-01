"""What agentd assumes about this server, checked from this side.

agentd writes the tables these migrations define with its own SQL. It names the migrations it
was written against in ``agentd/schema-migrations.txt`` and will not serve a database that
lacks one; a migration added here without that file changing is one agentd was never run
against.
"""

from pathlib import Path

from django.db.migrations.loader import MigrationLoader

REQUIRED = Path(__file__).resolve().parents[1] / "agentd" / "schema-migrations.txt"
#: The apps whose tables agentd reads and writes and this repository migrates.
APPS = ("datalayer", "facade")


def test_agentd_names_the_latest_migrations() -> None:
    """The file lists the leaf migration of each app agentd depends on."""
    leaves = sorted(f"{app} {name}" for app, name in MigrationLoader(None, ignore_no_migrations=True).graph.leaf_nodes() if app in APPS)
    named = [line for line in REQUIRED.read_text().splitlines() if line and not line.startswith("#")]
    assert named == leaves, "agentd/schema-migrations.txt is behind: after checking agentd's SQL against the new migration, list\n" + "\n".join(leaves)

"""takt inserts into these tables with SQL: a default it would have to repeat belongs in the database.

A column whose default lives only in Python is one takt must name in every INSERT, and one a
migration cannot add without breaking the takt already running. So every defaulted column of
this server's own tables carries the default in the schema too.
"""

import pytest
from django.apps import apps
from django.db import connection
from django.db.models.fields import NOT_PROVIDED

#: Columns whose value is never a default in practice.
REQUIRED_DESPITE_A_PYTHON_DEFAULT = {
    ("facade_task", "reference"),  # the caller's idempotency key: every writer names it
}


def defaulted_columns() -> list[tuple[str, str, bool]]:
    """``(table, column, has a database default)`` for every column that has a Python default."""
    columns = []
    for app in ("facade", "datalayer"):
        for model in apps.get_app_config(app).get_models():
            for field in model._meta.local_concrete_fields:
                automatic = getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False)
                if (field.has_default() or automatic) and (model._meta.db_table, field.column) not in REQUIRED_DESPITE_A_PYTHON_DEFAULT:
                    columns.append((model._meta.db_table, field.column, field.db_default is not NOT_PROVIDED))
    return columns


def test_every_python_default_is_declared_for_the_database_too() -> None:
    """A new field with ``default=`` or ``auto_now`` needs ``db_default=`` beside it."""
    missing = [f"{table}.{column}" for table, column, declared in defaulted_columns() if not declared]
    assert not missing, f"add db_default= (Now() for auto_now fields) to: {', '.join(missing)}"


@pytest.mark.django_db
def test_the_migrated_schema_carries_them() -> None:
    """What the models declare is what Postgres holds: no migration is missing."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema() AND column_default IS NOT NULL")
        in_schema = set(cursor.fetchall())
    missing = [f"{table}.{column}" for table, column, _ in defaulted_columns() if (table, column) not in in_schema]
    assert not missing, f"no database default on: {', '.join(missing)}"

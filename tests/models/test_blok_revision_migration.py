"""0018: every custom blok starts its history where it is; a provided one gets none."""

import pytest
from authentikate.models import App
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from facade import models
from facade.blok_revisions import commit_revision, content_of_blok
from tests.factories import _build_task, create_registry_bundle

BEFORE = ("facade", "0017_blok_origin")


@pytest.mark.django_db(transaction=True)
def test_every_custom_blok_starts_its_history_where_it_is() -> None:
    user, _, organization, _ = create_registry_bundle("history")
    catalog = models.UICatalog.objects.create(name="orkestrator", organization=organization)
    app = App.objects.create(identifier="history-app")

    executor = MigrationExecutor(connection)
    executor.migrate([BEFORE])
    old = executor.loader.project_state([BEFORE]).apps
    Blok, BlokDependency = old.get_model("facade", "Blok"), old.get_model("facade", "BlokDependency")
    tree = [{"id": "root", "component": "Box", "props": None, "children": None}]
    custom = Blok.objects.create(name="panel", organization_id=organization.pk, creator_id=user.pk, catalog_id=catalog.pk, components=tree, demo_state={"x": 1})
    BlokDependency.objects.create(blok=custom, key="stage", optional=True)
    provided = Blok.objects.create(name="panel", organization_id=organization.pk, creator_id=user.pk, catalog_id=catalog.pk, app_id=app.pk, declaration_hash="a" * 64)

    # Forward to the latest: every test after this one runs on what is left.
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes("facade"))

    blok = models.Blok.objects.get(pk=custom.pk)
    head = blok.head
    assert head is not None
    assert (head.number, head.parent_id, head.author_id) == (1, None, user.pk)
    assert head.components == tree and head.demo_state == {"x": 1}
    assert [(d["key"], d["optional"]) for d in head.dependencies] == [("stage", True)]
    assert models.Blok.objects.get(pk=provided.pk).head is None

    # The first revision is hashed as the server hashes: writing what it holds commits nothing.
    assert commit_revision(blok, content_of_blok(blok), base=head, author=None).pk == head.pk


@pytest.mark.django_db(transaction=True)
def test_a_task_deleted_by_sql_that_knows_no_revisions_lets_go_of_them() -> None:
    """A takt from before revisions deletes tasks without touching them (a rollback runs one)."""
    task = _build_task("revision-task")
    user, _, organization, _ = create_registry_bundle("revision-task-blok")
    catalog = models.UICatalog.objects.create(name="orkestrator", organization=organization)
    blok = models.Blok.objects.create(name="panel", organization=organization, creator=user, catalog=catalog)
    revision = commit_revision(blok, content_of_blok(blok), base=None, author=user, task=task)

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM facade_task WHERE id = %s", [task.pk])

    revision.refresh_from_db()
    assert revision.task_id is None

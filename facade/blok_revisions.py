"""A custom blok's history: every write commits a ``BlokRevision`` and copies it onto the ``Blok``.

The ``Blok`` row and its ``BlokDependency`` rows stay what everything reads (search, demands,
takt, the UI); they are the head revision, projected. A write names the revision it started from
and is refused when another write landed in between, so an assistant that read a blok at the start
of a long turn never overwrites what a user (or another assistant) changed meanwhile. An undo is a
new revision with an old one's content: history only grows.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable

from authentikate.models import User
from django.db import transaction
from django.db.models import Max

from facade import models
from facade.catalog_validation import dump_diagnostics, validate_manifest_against_catalog
from facade.json_types import JSONObject, json_object
from rekuest_core import enums as renums
from rekuest_core.inputs import models as rimodels


class StaleRevisionError(ValueError):
    """The blok was changed after the revision a write started from."""

    def __init__(self, blok: models.Blok, base: models.BlokRevision | None, head: models.BlokRevision | None) -> None:
        self.head = head
        started = "no revision" if base is None else f"revision {base.number} ({base.pk})"
        now = "none" if head is None else f"revision {head.number} ({head.pk})"
        super().__init__(f"Blok '{blok.name}' changed since {started}: its head is {now}. Read it again and redo the change on top of it.")


@dataclasses.dataclass(frozen=True)
class BlokContent:
    """What a revision holds: everything that decides what a blok draws and which agents it needs."""

    components: list[JSONObject]
    """The component tree as stored: a copy keeps it as it is, a check parses it."""
    demo_state: JSONObject
    context: renums.ComponentContext
    catalog: models.UICatalog
    dependencies: list[rimodels.AgentDependencyInputModel]

    def but(
        self,
        *,
        components: list[JSONObject] | None = None,
        demo_state: JSONObject | None = None,
        catalog: models.UICatalog | None = None,
        dependencies: list[rimodels.AgentDependencyInputModel] | None = None,
    ) -> BlokContent:
        """This content with every part that is given replaced; ``None`` keeps a part."""
        return BlokContent(
            components=self.components if components is None else components,
            demo_state=self.demo_state if demo_state is None else demo_state,
            context=self.context,
            catalog=self.catalog if catalog is None else catalog,
            dependencies=self.dependencies if dependencies is None else dependencies,
        )

    def parsed_components(self) -> list[rimodels.ComponentNodeInputModel]:
        return [rimodels.ComponentNodeInputModel.model_validate(component) for component in self.components]

    def stored_dependencies(self) -> list[dict]:
        return [dependency.model_dump(mode="json") for dependency in self.dependencies]

    def hash(self) -> str:
        """sha256 of canonical JSON of every part; the catalog by its id."""
        canonical = json.dumps(
            {
                "components": self.components,
                "demo_state": self.demo_state,
                "context": self.context.value,
                "catalog": self.catalog.pk,
                "dependencies": self.stored_dependencies(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def check(self) -> None:
        """The component tree coherent with the dependencies and the local state; ``ValueError`` otherwise."""
        rimodels.check_blok_manifest(self.parsed_components(), {dependency.key for dependency in self.dependencies}, set(self.demo_state))


def dependency_input_of(row: models.BlokDependency) -> rimodels.AgentDependencyInputModel:
    """A dependency row as the input that declares it."""
    return rimodels.AgentDependencyInputModel(
        key=row.key,
        app=row.app_filter,
        version=row.version_filter,
        description=row.description,
        optional=row.optional,
        action_dependencies=row.get_action_dependencies(),
        state_dependencies=row.get_state_dependencies(),
        auto_resolvable=row.auto_resolvable,
        min_viable_instances=row.min_viable_instances,
        max_viable_instances=row.max_viable_instances,
    )


def content_of_blok(blok: models.Blok) -> BlokContent:
    """What a blok holds now, read off its row and its dependency rows."""
    return BlokContent(
        components=[json_object(component) for component in blok.components],
        demo_state=json_object(blok.demo_state or {}),
        context=renums.ComponentContext(blok.context),
        catalog=blok.catalog,
        dependencies=[dependency_input_of(row) for row in blok.dependencies.order_by("key")],
    )


def content_of_revision(revision: models.BlokRevision) -> BlokContent:
    return BlokContent(
        components=[json_object(component) for component in revision.components],
        demo_state=json_object(revision.demo_state or {}),
        context=renums.ComponentContext(revision.context),
        catalog=revision.catalog,
        dependencies=[rimodels.AgentDependencyInputModel(**dependency) for dependency in revision.dependencies],
    )


def sync_blok_dependencies(blok: models.Blok, dependencies: Iterable[rimodels.AgentDependencyInputModel]) -> None:
    """Upsert ``BlokDependency`` rows by ``(blok, key)`` and delete every key not declared any more.

    Upserting keeps a kept key's row, so the agent mappings that point at it stay bound. A
    ``BlokAgentMapping`` of a deleted key keeps its row with ``dependency=NULL`` (``SET_NULL``).
    """
    existing = {dep.key: dep for dep in blok.dependencies.all()}
    for declared in dependencies:
        dependency, _ = models.BlokDependency.objects.update_or_create(
            blok=blok,
            key=declared.key,
            defaults=dict(
                action_demands=[d.model_dump() for d in declared.action_dependencies or []],
                state_demands=[d.model_dump() for d in declared.state_dependencies or []],
                app_filter=declared.app,
                version_filter=declared.version,
                optional=declared.optional,
                description=declared.description,
                auto_resolvable=declared.auto_resolvable,
                min_viable_instances=declared.min_viable_instances,
                max_viable_instances=declared.max_viable_instances,
            ),
        )
        existing.pop(declared.key, None)
        # A key declared again (an undo of its removal) takes back the bindings that lost it.
        models.BlokAgentMapping.objects.filter(materialized_blok__blok=blok, key=declared.key, dependency__isnull=True).update(dependency=dependency)
    for stale in existing.values():
        stale.delete()


def _diagnostics(content: BlokContent) -> list[dict]:
    """The non-fatal findings on ``content``, as a blok row stores them."""
    return dump_diagnostics(validate_manifest_against_catalog(content.catalog, content.parsed_components(), content.context, content.demo_state))


def lock_blok(blok_id: int) -> models.Blok:
    """The blok row, locked until the transaction ends: writes to one blok queue behind each other."""
    return models.Blok.objects.select_for_update(of=("self",)).select_related("head", "catalog").get(pk=blok_id)


@transaction.atomic
def commit_revision(
    blok: models.Blok,
    content: BlokContent,
    *,
    base: models.BlokRevision | None,
    guarded: bool = True,
    author: User | None,
    task: models.Task | None = None,
    message: str | None = None,
) -> models.BlokRevision:
    """Commit ``content`` as the custom blok's next revision and copy it onto the blok.

    Joins the caller's transaction, if any. With ``guarded`` the write is refused
    (:class:`StaleRevisionError`) unless ``base`` is still the head; an unguarded write (the
    old ``updateBlok`` / ``createBlok``) lands on whatever the head is. Content equal to the
    head's commits nothing and returns the head.
    """
    if blok.app_id is not None:
        raise ValueError(f"Blok '{blok.name}' is provided by its agents and has no revisions; fork it to edit it.")
    locked = lock_blok(blok.pk)
    head = locked.head
    if guarded and (None if base is None else base.pk) != (None if head is None else head.pk):
        raise StaleRevisionError(locked, base, head)

    content_hash = content.hash()
    if head is not None and head.content_hash == content_hash:
        # Nothing to commit, but the findings depend on the catalog as it is now.
        locked.diagnostics = _diagnostics(content)
        locked.save(update_fields=["diagnostics"])
        return head

    number = (locked.revisions.aggregate(last=Max("number"))["last"] or 0) + 1
    revision = models.BlokRevision.objects.create(
        blok=locked,
        number=number,
        parent=head,
        components=content.components,
        demo_state=content.demo_state,
        context=content.context.value,
        catalog=content.catalog,
        dependencies=content.stored_dependencies(),
        content_hash=content_hash,
        author=author,
        task=task,
        message=message,
    )

    locked.components = revision.components
    locked.demo_state = revision.demo_state
    locked.context = revision.context
    locked.catalog = content.catalog
    locked.diagnostics = _diagnostics(content)
    locked.head = revision
    locked.save(update_fields=["components", "demo_state", "context", "catalog", "diagnostics", "head"])
    sync_blok_dependencies(locked, content.dependencies)

    # The caller's instance reads what was written.
    blok.refresh_from_db()
    return revision

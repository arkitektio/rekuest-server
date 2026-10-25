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
from facade.descriptors import compile_descriptors_to_jsonpath
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
    args: list[rimodels.ArgPortInputModel] = dataclasses.field(default_factory=list)
    """The items the blok is opened for, read under ``@args.<key>``."""
    services: list[rimodels.BlokServiceInputModel] = dataclasses.field(default_factory=list)
    """The backend services the blok's queries read from, frozen at their versions."""
    persist: list[rimodels.BlokPersistInputModel] = dataclasses.field(default_factory=list)
    """The locals a renderer may keep between openings, on the device."""

    def but(
        self,
        *,
        components: list[JSONObject] | None = None,
        demo_state: JSONObject | None = None,
        catalog: models.UICatalog | None = None,
        dependencies: list[rimodels.AgentDependencyInputModel] | None = None,
        args: list[rimodels.ArgPortInputModel] | None = None,
        services: list[rimodels.BlokServiceInputModel] | None = None,
        persist: list[rimodels.BlokPersistInputModel] | None = None,
    ) -> BlokContent:
        """This content with every part that is given replaced; ``None`` keeps a part."""
        return BlokContent(
            components=self.components if components is None else components,
            demo_state=self.demo_state if demo_state is None else demo_state,
            context=self.context,
            catalog=self.catalog if catalog is None else catalog,
            dependencies=self.dependencies if dependencies is None else dependencies,
            args=self.args if args is None else args,
            services=self.services if services is None else services,
            persist=self.persist if persist is None else persist,
        )

    def parsed_components(self) -> list[rimodels.ComponentNodeInputModel]:
        return [rimodels.ComponentNodeInputModel.model_validate(component) for component in self.components]

    def stored_dependencies(self) -> list[dict]:
        return [dependency.model_dump(mode="json") for dependency in self.dependencies]

    def stored_args(self) -> list[dict]:
        return [arg.model_dump(mode="json") for arg in self.args]

    def stored_services(self) -> list[dict]:
        return [service.model_dump(mode="json") for service in self.services]

    def stored_persist(self) -> list[dict]:
        return [local.model_dump(mode="json") for local in self.persist]

    def hash(self) -> str:
        """sha256 of canonical JSON of every part; the catalog by its id.

        Args, services and persisted locals count only when there are some, so a revision written
        before revisions held them keeps its hash."""
        parts = {
            "components": self.components,
            "demo_state": self.demo_state,
            "context": self.context.value,
            "catalog": self.catalog.pk,
            "dependencies": self.stored_dependencies(),
        }
        if self.args:
            parts["args"] = self.stored_args()
        if self.services:
            parts["services"] = self.stored_services()
        if self.persist:
            parts["persist"] = self.stored_persist()
        canonical = json.dumps(
            parts,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def check(self) -> None:
        """The component tree coherent with the dependencies and the local state; ``ValueError`` otherwise."""
        dependency_keys = {dependency.key for dependency in self.dependencies}
        rimodels.check_blok_manifest(self.parsed_components(), dependency_keys, set(self.demo_state))
        rimodels.check_blok_args(self.context, self.args, dependency_keys)
        rimodels.check_blok_persist(self.persist, dependency_keys, set(self.demo_state))
        rimodels.check_blok_services(self.parsed_components(), self.services)


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
        args=[rimodels.ArgPortInputModel(**arg) for arg in blok.args or []],
        services=[rimodels.BlokServiceInputModel(**service) for service in blok.services or []],
        persist=[rimodels.BlokPersistInputModel(**local) for local in blok.persist or []],
    )


def content_of_revision(revision: models.BlokRevision) -> BlokContent:
    return BlokContent(
        components=[json_object(component) for component in revision.components],
        demo_state=json_object(revision.demo_state or {}),
        context=renums.ComponentContext(revision.context),
        catalog=revision.catalog,
        dependencies=[rimodels.AgentDependencyInputModel(**dependency) for dependency in revision.dependencies],
        args=[rimodels.ArgPortInputModel(**arg) for arg in revision.args or []],
        services=[rimodels.BlokServiceInputModel(**service) for service in revision.services or []],
        persist=[rimodels.BlokPersistInputModel(**local) for local in revision.persist or []],
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


def sync_blok_arg_ports(blok: models.Blok, args: Iterable[rimodels.ArgPortInputModel]) -> None:
    """Replace the blok's ``BlokArgPort`` rows with ``args``' port trees, parents before children.

    The rows are what ``demands`` match; ``key_path`` is dotted from the root, ``requires``
    compiled to the JSONPath an item's descriptors are tested with, as takt writes them."""

    def row(port: rimodels.ArgPortInputModel, parent: models.BlokArgPort | None, index: int, parent_path: str) -> tuple[rimodels.ArgPortInputModel, str, models.BlokArgPort]:
        path = f"{parent_path}.{port.key}" if parent_path else port.key
        return port, path, models.BlokArgPort(
            blok=blok,
            parent=parent,
            index=index,
            key=port.key,
            key_path=path,
            kind=getattr(port.kind, "value", port.kind),
            identifier=port.identifier,
            compiled_jsonpath=compile_descriptors_to_jsonpath(port.requires or []),
            nullable=port.nullable,
            dimension=port.dimension,
        )

    blok.arg_ports.all().delete()
    level = [row(port, None, index, "") for index, port in enumerate(args)]
    while level:
        models.BlokArgPort.objects.bulk_create([port_row for _, _, port_row in level])
        level = [row(child, port_row, index, path) for port, path, port_row in level for index, child in enumerate(port.children or [])]


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
        args=content.stored_args(),
        services=content.stored_services(),
        persist=content.stored_persist(),
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
    # The rows are rewritten only when the args change: a fork copied its provided blok's already.
    args_changed = locked.args != revision.args
    locked.args = revision.args
    locked.services = revision.services
    locked.persist = revision.persist
    locked.save(update_fields=["components", "demo_state", "context", "catalog", "args", "services", "persist", "diagnostics", "head"])
    sync_blok_dependencies(locked, content.dependencies)
    if args_changed:
        sync_blok_arg_ports(locked, content.args)

    # The caller's instance reads what was written.
    blok.refresh_from_db()
    return revision

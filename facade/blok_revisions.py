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
from facade.catalog_validation import dump_diagnostics, resolved_scope, validate_blok_against_module
from facade.descriptors import compile_descriptors_to_jsonpath
from facade.json_types import JSONObject, json_object
from rekuest_core import enums as renums
from rekuest_core.inputs import models as rimodels
from rekuest_core.vocabulary import BlokScope
from rekuest_core.vocabulary.infer import resolve_local


class StaleRevisionError(ValueError):
    """The blok was changed after the revision a write started from."""

    def __init__(self, blok: models.Blok, base: models.BlokRevision | None, head: models.BlokRevision | None) -> None:
        self.head = head
        started = "no revision" if base is None else f"revision {base.number} ({base.pk})"
        now = "none" if head is None else f"revision {head.number} ({head.pk})"
        super().__init__(f"Blok '{blok.name}' changed since {started}: its head is {now}. Read it again and redo the change on top of it.")


@dataclasses.dataclass(frozen=True)
class BlokContent:
    """What a revision holds: everything that decides what a blok draws and which agents it needs
    (blok language v2)."""

    components: list[JSONObject]
    """The component tree as stored (one root): a copy keeps it as it is, a check parses it."""
    context: renums.ComponentContext
    module: models.Module
    dependencies: list[rimodels.AgentDependencyInputModel]
    args: list[rimodels.ArgPortInputModel] = dataclasses.field(default_factory=list)
    """The items the blok is opened for, read under ``args.<key>``."""
    catalogs: list[str] = dataclasses.field(default_factory=list)
    locals: list[rimodels.BlokLocalInputModel] = dataclasses.field(default_factory=list)
    """Its locals as written: a kind left out stays out. What is stored as ``locals`` and hashed."""
    demo: rimodels.BlokDemoInputModel | None = None
    queries: list[rimodels.BlokQueryInputModel] = dataclasses.field(default_factory=list)
    subscriptions: list[rimodels.BlokSubscriptionInputModel] = dataclasses.field(default_factory=list)
    vocab: int = 2
    resolved_locals: list[rimodels.BlokLocalInputModel] | None = None
    """Its locals as its tree resolves them (:meth:`resolved`), each with a kind; None until
    resolved. Derived from the rest: never hashed, and dropped by :meth:`but`."""

    def but(
        self,
        *,
        components: list[JSONObject] | None = None,
        module: models.Module | None = None,
        dependencies: list[rimodels.AgentDependencyInputModel] | None = None,
        args: list[rimodels.ArgPortInputModel] | None = None,
        catalogs: list[str] | None = None,
        locals: list[rimodels.BlokLocalInputModel] | None = None,
        demo: rimodels.BlokDemoInputModel | None = None,
        queries: list[rimodels.BlokQueryInputModel] | None = None,
        subscriptions: list[rimodels.BlokSubscriptionInputModel] | None = None,
    ) -> BlokContent:
        """This content with every part that is given replaced; ``None`` keeps a part. The
        resolved locals are not kept: they follow from the new content."""
        return BlokContent(
            components=self.components if components is None else components,
            context=self.context,
            module=self.module if module is None else module,
            dependencies=self.dependencies if dependencies is None else dependencies,
            args=self.args if args is None else args,
            catalogs=self.catalogs if catalogs is None else catalogs,
            locals=self.locals if locals is None else locals,
            demo=self.demo if demo is None else demo,
            queries=self.queries if queries is None else queries,
            subscriptions=self.subscriptions if subscriptions is None else subscriptions,
        )

    def parsed_components(self) -> list[rimodels.ComponentNodeInputModel]:
        return [rimodels.ComponentNodeInputModel.model_validate(component) for component in self.components]

    def stored_dependencies(self) -> list[dict]:
        return [dependency.model_dump(mode="json") for dependency in self.dependencies]

    def stored_args(self) -> list[dict]:
        return [arg.model_dump(mode="json") for arg in self.args]

    def stored_locals(self) -> list[dict]:
        return [local.model_dump(mode="json", exclude_none=True) for local in self.locals]

    def stored_resolved_locals(self) -> list[dict]:
        return [local.model_dump(mode="json", exclude_none=True) for local in self.locals_resolved()]

    def stored_demo(self) -> dict:
        return self.demo.model_dump(mode="json", exclude_none=True) if self.demo is not None else {}

    def stored_queries(self) -> list[dict]:
        return [query.model_dump(mode="json", exclude_none=True) for query in self.queries]

    def stored_subscriptions(self) -> list[dict]:
        return [subscription.model_dump(mode="json", exclude_none=True) for subscription in self.subscriptions]

    def rendering(self) -> dict[str, object]:
        """What decides what the blok draws: what :func:`blok_hash` hashes."""
        return rendering_of(self.components, self.context.value, self.vocab, self.catalogs, self.stored_locals(), self.stored_demo(), self.stored_queries(), self.stored_subscriptions())

    def hash(self) -> str:
        """sha256 of canonical JSON of every part; the module by its id. Parts that are empty are left out."""
        parts = {**self.rendering(), "module": self.module.pk, "dependencies": self.stored_dependencies()}
        if self.args:
            parts["args"] = self.stored_args()
        canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def scope(self, kind: str = "custom") -> BlokScope:
        """What its tree is checked against: its locals resolved."""
        return BlokScope(
            kind=kind,
            context=self.context.value,
            dependencies=tuple(self.dependencies),
            locals=tuple(self.locals_resolved()),
            queries=tuple(self.queries),
            subscriptions=tuple(self.subscriptions),
            args=tuple(arg.key for arg in self.args) if self.args else None,
            catalogs=tuple(self.catalogs),
            demo=self.demo,
        )

    def locals_resolved(self) -> list[rimodels.BlokLocalInputModel]:
        """Its locals as its tree (against its module's view) resolves them, each with a kind
        (``rekuest_core.vocabulary.infer``): the ones :meth:`resolved` found, or found now.
        Without a tree a local's kind comes from its default alone, ``ANY`` otherwise."""
        if self.resolved_locals is not None:
            return self.resolved_locals
        if not self.components:
            return [resolve_local(local, (), ()) for local in self.locals]
        written = BlokScope(context=self.context.value, dependencies=tuple(self.dependencies), locals=tuple(self.locals))
        return list(resolved_scope(self.module, self.parsed_components(), written).locals)

    def resolved(self) -> BlokContent:
        """This content with its locals resolved beside the written ones (``resolved_locals``):
        what every write checks against and stores. Dependencies are never inferred."""
        return dataclasses.replace(self, resolved_locals=self.locals_resolved())

    def check_declarations(self) -> None:
        """What it declares beside its tree well-formed; ``ValueError`` otherwise."""
        rimodels.check_blok_declarations(
            kind="custom",
            catalogs=self.catalogs,
            locals=self.locals_resolved(),
            queries=self.queries,
            subscriptions=self.subscriptions,
            dependency_keys={dependency.key for dependency in self.dependencies},
            demo=self.demo,
        )
        rimodels.check_blok_args(self.context, self.args)

    def check(self) -> None:
        """What it declares well-formed and its tree coherent with it (no catalog); ``ValueError`` otherwise."""
        rimodels.check_blok_declarations(
            kind="custom",
            catalogs=self.catalogs,
            locals=self.locals_resolved(),
            queries=self.queries,
            subscriptions=self.subscriptions,
            dependency_keys={dependency.key for dependency in self.dependencies},
            demo=self.demo,
        )
        rimodels.check_blok_args(self.context, self.args)
        if not self.components:
            # A custom blok created before it draws anything (createBlok without a tree): nothing to check yet.
            return
        scope = self.scope()
        rimodels.check_blok_manifest(
            self.parsed_components(),
            kind=scope.kind,
            context=self.context,
            dependencies=self.dependencies,
            locals=list(scope.locals),
            queries=self.queries,
            subscriptions=self.subscriptions,
            args=list(scope.args) if scope.args is not None else None,
            catalogs=self.catalogs,
            demo=self.demo,
        )


def rendering_of(
    components: object,
    context: str,
    vocab: int,
    catalogs: list[str],
    locals: list[dict],
    demo: dict,
    queries: list[dict],
    subscriptions: list[dict],
) -> dict[str, object]:
    """A blok's rendering parts as hashed: the tree, the context and the language version always,
    the rest only when set."""
    parts: dict[str, object] = {"components": components, "context": context, "vocab": vocab}
    optional: dict[str, object] = {"catalogs": catalogs, "locals": locals, "demo": demo, "queries": queries, "subscriptions": subscriptions}
    parts.update({key: value for key, value in optional.items() if value not in (None, [], {})})
    return parts


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
        context=renums.ComponentContext(blok.context),
        module=blok.module,
        dependencies=[dependency_input_of(row) for row in blok.dependencies.order_by("key")],
        args=[rimodels.ArgPortInputModel(**arg) for arg in blok.args or []],
        catalogs=list(blok.catalogs or []),
        locals=[rimodels.BlokLocalInputModel(**local) for local in blok.locals or []],
        demo=rimodels.BlokDemoInputModel(**blok.demo) if blok.demo else None,
        queries=[rimodels.BlokQueryInputModel(**query) for query in blok.queries or []],
        subscriptions=[rimodels.BlokSubscriptionInputModel(**subscription) for subscription in blok.subscriptions or []],
    )


def content_of_revision(revision: models.BlokRevision) -> BlokContent:
    return BlokContent(
        components=[json_object(component) for component in revision.components],
        context=renums.ComponentContext(revision.context),
        module=revision.module,
        dependencies=[rimodels.AgentDependencyInputModel(**dependency) for dependency in revision.dependencies],
        args=[rimodels.ArgPortInputModel(**arg) for arg in revision.args or []],
        catalogs=list(revision.catalogs or []),
        locals=[rimodels.BlokLocalInputModel(**local) for local in revision.locals or []],
        demo=rimodels.BlokDemoInputModel(**revision.demo) if revision.demo else None,
        queries=[rimodels.BlokQueryInputModel(**query) for query in revision.queries or []],
        subscriptions=[rimodels.BlokSubscriptionInputModel(**subscription) for subscription in revision.subscriptions or []],
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
    """The warnings on ``content`` against its module, as a blok row stores them; an error refuses it."""
    if not content.components:
        return []
    return dump_diagnostics(validate_blok_against_module(content.module, content.parsed_components(), content.scope()))


def lock_blok(blok_id: int) -> models.Blok:
    """The blok row, locked until the transaction ends: writes to one blok queue behind each other."""
    return models.Blok.objects.select_for_update(of=("self",)).select_related("head", "module").get(pk=blok_id)


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
    """Commit ``content`` as the custom blok's next revision and copy it onto the blok, its
    locals as written and as resolved (recomputed here when ``content`` is not resolved yet).

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

    content = content.resolved()
    content_hash = content.hash()
    if head is not None and head.content_hash == content_hash:
        # Nothing to commit, but the findings and the resolved locals depend on the module as it is now.
        locked.diagnostics = _diagnostics(content)
        locked.resolved_locals = content.stored_resolved_locals()
        locked.save(update_fields=["diagnostics", "resolved_locals"])
        blok.refresh_from_db()
        return head

    number = (locked.revisions.aggregate(last=Max("number"))["last"] or 0) + 1
    revision = models.BlokRevision.objects.create(
        blok=locked,
        number=number,
        parent=head,
        components=content.components,
        context=content.context.value,
        module=content.module,
        dependencies=content.stored_dependencies(),
        args=content.stored_args(),
        vocab=content.vocab,
        catalogs=content.catalogs,
        locals=content.stored_locals(),
        resolved_locals=content.stored_resolved_locals(),
        demo=content.stored_demo(),
        queries=content.stored_queries(),
        subscriptions=content.stored_subscriptions(),
        content_hash=content_hash,
        author=author,
        task=task,
        message=message,
    )

    locked.components = revision.components
    locked.context = revision.context
    locked.module = content.module
    locked.diagnostics = _diagnostics(content)
    locked.head = revision
    # The rows are rewritten only when the args change: a fork copied its provided blok's already.
    args_changed = locked.args != revision.args
    locked.args = revision.args
    locked.vocab = revision.vocab
    locked.catalogs = revision.catalogs
    locked.locals = revision.locals
    locked.resolved_locals = revision.resolved_locals
    locked.demo = revision.demo
    locked.queries = revision.queries
    locked.subscriptions = revision.subscriptions
    locked.save(update_fields=["components", "context", "module", "args", "vocab", "catalogs", "locals", "resolved_locals", "demo", "queries", "subscriptions", "diagnostics", "head"])
    sync_blok_dependencies(locked, content.dependencies)
    if args_changed:
        sync_blok_arg_ports(locked, content.args)

    # The caller's instance reads what was written.
    blok.refresh_from_db()
    return revision

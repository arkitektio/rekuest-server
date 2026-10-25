"""Blok mutations: create, update, delete, fork.

These write custom bloks only. A provided blok (``app`` set) is its agents' and changes only with
their registrations; a user who wants to change it, or bind other agents to it, forks it.

Every read here is scoped to the caller's organization. ``scoped_get`` raises ``PermissionError``
for a wrong-tenant id so it is indistinguishable from a missing one; deletes use a scoped filter
for the same reason.
"""

from typing import cast

from authentikate.models import User
from django.db import transaction
from kante.types import Info

from facade import inputs, models, takt, takt_api, types
from facade.blok_revisions import BlokContent, StaleRevisionError, commit_revision, content_of_blok, content_of_revision, lock_blok
from facade.caller_context import CallerContext
from facade.catalog_validation import validate_manifest_against_catalog
from facade.mutations.materialized_blok import check_agent_mappings, organization_of, write_mappings
from facade.types.base import scoped_get
from facade.json_types import JSONObject, json_object
from rekuest_core.inputs import models as rimodels
from rekuest_core.objects import types as rtypes


STANDARD_CATALOG_NAME = "orkestrator"
"""The catalog a blok naming none is checked against: the one the Orkestrator app registers.

The client's offline check uses the same name, and takt's ``register_blok`` falls back to it too.
"""


def _catalog_for(info: Info, name: str | None) -> models.UICatalog:
    """The named catalog in the caller's organization, created empty if it does not exist yet."""
    return models.UICatalog.objects.get_or_create(name=name or STANDARD_CATALOG_NAME, organization=info.context.request.organization)[0]


def _custom(info: Info, id: str) -> models.Blok:
    """The caller's blok ``id``, refused when an app provides it."""
    blok = scoped_get(models.Blok, info, id, field="organization")
    if blok.app_id is not None:
        raise ValueError(f"Blok '{blok.name}' is provided by its agents and changes with them; fork it to edit it.")
    return blok


def _parsed(source: str) -> list[rimodels.ComponentNodeInputModel]:
    """The tree a blok's BSX source describes, parsed by takt (the one server-side BSX parser)."""
    answer = takt.call(takt_api.PARSE_BLOK, takt_api.ParseBlokRequest(source=source))
    return [rimodels.ComponentNodeInputModel(**component) for component in answer.components]


@transaction.atomic
def create_blok(info: Info, input: inputs.CreateBlokInput) -> types.Blok:
    """Create or replace a custom blok in the caller's organization: by its name, or by its ``origin`` when it is installed from one."""
    model = input.to_pydantic()
    if model.source is not None:
        components = _parsed(model.source)
        rimodels.check_blok_manifest(components, {dep.key for dep in model.dependencies or []}, None if model.demo_state is None else set(model.demo_state))
        model = model.model_copy(update={"components": components, "source": None})
    # Every query in the tree runs on a service the blok names.
    rimodels.check_blok_services(model.components, list(model.services or []))
    organization = info.context.request.organization
    catalog = _catalog_for(info, model.catalog)

    # ``organization`` is part of the lookup, not the defaults: keyed on name alone this was a
    # global upsert, so any user could overwrite another organization's blok by name.
    # Only a custom blok is ever replaced: one an app provides under the same name is its own row.
    # A blok installed from an origin is found again by it, whatever it is named, and never
    # replaces a blok made by hand or installed from elsewhere under its name.
    if model.origin is not None:
        if models.Blok.objects.filter(organization=organization, name=model.name, app__isnull=True).exclude(origin=model.origin).exists():
            raise ValueError(f"A custom blok named '{model.name}' exists already; rename or delete it to install this one.")
        lookup = dict(origin=model.origin)
        installed = dict(name=model.name, origin_hash=model.origin_hash)
    else:
        lookup = dict(name=model.name)
        installed = {}
    blok, _ = models.Blok.objects.update_or_create(
        **lookup,
        organization=organization,
        app=None,
        defaults=dict(
            **installed,
            description=model.description,
            creator=info.context.request.user,
            catalog=catalog,
            for_model=model.for_model,
            for_node=model.for_node,
        ),
    )

    # create is an upsert on (name, organization): it commits what it is given as the blok's next
    # revision, dependencies a previous one declared but this one does not included.
    content = BlokContent(
        components=[json_object(component.model_dump()) for component in model.components or []],
        demo_state=model.demo_state or {},
        context=model.context,
        catalog=catalog,
        dependencies=list(model.dependencies or []),
        # createBlok declares no args: a blok created again keeps the ones it has.
        args=[rimodels.ArgPortInputModel(**arg) for arg in blok.args or []],
        services=list(model.services or []),
        # Kept when omitted, as an edit keeps them: a blok created again keeps its persisted locals.
        persist=[rimodels.BlokPersistInputModel(**local) for local in blok.persist or []] if model.persist is None else list(model.persist),
    )
    # Neither the upsert nor the commit checks the content: the persisted locals must still be locals.
    rimodels.check_blok_persist(content.persist, {dependency.key for dependency in content.dependencies}, set(content.demo_state))
    commit_revision(blok, content, base=None, guarded=False, author=CallerContext.from_info(info).user, message="Created" if blok.head_id is None else "Created again")

    return cast("types.Blok", blok)


def delete_blok(info: Info, input: inputs.DeleteBlokInput) -> bool:
    """Delete a custom blok in the caller's organization; False when there is none."""
    if models.Blok.objects.filter(id=input.id, organization=info.context.request.organization, app__isnull=False).exists():
        raise ValueError("A provided blok goes when no agent declares it any more; it cannot be deleted by hand.")
    deleted, _ = models.Blok.objects.filter(id=input.id, organization=info.context.request.organization).delete()
    return deleted > 0


def _fork(provided: models.Blok, name: str, user: User) -> models.Blok:
    """A custom copy of a provided blok, its first revision the provided content."""
    if models.Blok.objects.filter(organization=provided.organization, name=name, app__isnull=True).exists():
        raise ValueError(f"A custom blok named '{name}' exists already.")
    blok = models.Blok.objects.create(
        organization=provided.organization,
        name=name,
        description=provided.description,
        creator=user,
        catalog=provided.catalog,
        context=provided.context,
        for_model=provided.for_model,
        for_node=provided.for_node,
        args=provided.args,
        services=provided.services,
        persist=provided.persist,
    )
    # Rows are written parents first, so in id order every parent is copied before its children.
    copies: dict[int, models.BlokArgPort] = {}
    for port in provided.arg_ports.order_by("id"):
        source_id, parent_id = port.pk, port.parent_id
        port.pk = None
        port.blok = blok
        port.parent = None if parent_id is None else copies[parent_id]
        port.save()
        copies[source_id] = port
    commit_revision(blok, content_of_blok(provided), base=None, author=user, message=f"Forked from the blok '{provided.name}' its agents provide")
    return blok


def _free_name(organization_id: int, name: str) -> str:
    """``name``, or the first ``name (n)`` no custom blok of the organization has."""
    taken = set(models.Blok.objects.filter(organization_id=organization_id, app__isnull=True, name__startswith=name).values_list("name", flat=True))
    if name not in taken:
        return name
    number = 2
    while f"{name} ({number})" in taken:
        number += 1
    return f"{name} ({number})"


@transaction.atomic
def fork_blok(info: Info, input: inputs.ForkBlokInput) -> types.Blok:
    """Copy a provided blok into a custom one, which lives on whatever its agents do."""
    provided = scoped_get(models.Blok, info, input.id, field="organization")
    if provided.app_id is None:
        raise ValueError(f"Blok '{provided.name}' is already custom.")
    return cast("types.Blok", _fork(provided, input.name or provided.name, CallerContext.from_info(info).user))


@transaction.atomic
def update_blok(info: Info, input: inputs.UpdateBlokInput) -> types.Blok:
    """Partially update a custom blok in the caller's organization: a new revision on top of its head."""
    model = input.to_pydantic()
    blok = _custom(info, model.id)

    if model.name is not None:
        blok.name = model.name
    if model.description is not None:
        blok.description = model.description
    blok.save(update_fields=["name", "description"])

    # A partial update is validated as the content it results in: new components against the
    # dependencies and local state that will be in force after the update.
    content = _edited(info, lock_blok(blok.pk), model.source, model.components, model.demo_state, model.catalog, model.dependencies, None)
    content.check()
    commit_revision(blok, content, base=None, guarded=False, author=CallerContext.from_info(info).user)
    return cast("types.Blok", blok)


def _edited(
    info: Info,
    blok: models.Blok,
    source: str | None,
    components: list[rimodels.ComponentNodeInputModel] | None,
    demo_state: JSONObject | None,
    catalog: str | None,
    dependencies: list[rimodels.AgentDependencyInputModel] | None,
    args: list[rimodels.ArgPortInputModel] | None,
    services: list[rimodels.BlokServiceInputModel] | None = None,
    persist: list[rimodels.BlokPersistInputModel] | None = None,
) -> BlokContent:
    """The blok's head content with every part that is given replaced."""
    current = content_of_revision(blok.head) if blok.head is not None else content_of_blok(blok)
    tree = _parsed(source) if source is not None else components
    return current.but(
        components=None if tree is None else [json_object(component.model_dump()) for component in tree],
        demo_state=demo_state,
        catalog=None if catalog is None else _catalog_for(info, catalog),
        dependencies=dependencies,
        args=args,
        services=services,
        persist=persist,
    )


def _task_of(caller: CallerContext, task_id: str | None) -> models.Task | None:
    """The task a write is made by, which must be one the calling client's agent runs."""
    if task_id is None:
        return None
    try:
        return models.Task.objects.get(id=task_id, agent__client=caller.client, agent__organization=caller.organization)
    except models.Task.DoesNotExist:
        raise PermissionError(f"Task {task_id} is not one of this client's agents runs.")


@transaction.atomic
def edit_blok(info: Info, input: inputs.EditBlokInput) -> types.BlokEdit:
    """Edit a blok in one write: on the blok itself, or through a materialization with its bindings there.

    The edit starts from ``base``, which must still be the blok's head. A provided blok is
    forked first (its agents' slot is left as it is); an edit made through the slot gets a new
    materialization of the fork, holding the slot's bindings. The new content is checked in
    full; with ``dry_run`` nothing is kept.
    """
    model = input.to_pydantic()
    caller = CallerContext.from_info(info)
    task = _task_of(caller, model.task)
    if model.materialized_blok is not None:
        mblok = scoped_get(models.MaterializedBlok, info, model.materialized_blok, field="blok__organization")
        blok = mblok.blok
    elif model.blok is not None:
        mblok = None
        blok = scoped_get(models.Blok, info, model.blok, field="organization")
    else:
        raise ValueError("Edit a blok, or a materialization of it: send `blok` or `materialized_blok`, one of the two.")

    provided = blok.app_id is not None
    if provided:
        base = None
    else:
        # Locked from here, so the head cannot move between this check and the write.
        blok = lock_blok(blok.pk)
        try:
            base = None if model.base is None else blok.revisions.get(id=model.base)
        except models.BlokRevision.DoesNotExist:
            raise ValueError(f"Blok '{blok.name}' has no revision {model.base}.")
        # No base names a blok without history; one that has some must be edited from its head.
        if (None if base is None else base.pk) != blok.head_id:
            raise StaleRevisionError(blok, base, blok.head)

    content = _edited(info, blok, model.source, model.components, model.demo_state, model.catalog, model.dependencies, model.args, model.services, model.persist)
    content.check()

    # The bindings an edit through a materialization leaves: the ones kept for keys still
    # declared, the given ones over them. An edit of the blok itself binds nothing; a required
    # dependency it adds shows as unbound on every materialization.
    bound: dict[str, models.Agent] | None = None
    if mblok is not None:
        declared = {dependency.key for dependency in content.dependencies}
        wanted = {mapping.key: str(mapping.agent_id) for mapping in mblok.agent_mappings.all() if mapping.key in declared}
        wanted.update({mapping.key: mapping.agent for mapping in model.agent_mappings or []})
        bound = check_agent_mappings(organization_of(info), blok.name, content.dependencies, wanted)
    diagnostics = cast("list[rtypes.Diagnostic]", validate_manifest_against_catalog(content.catalog, content.parsed_components(), content.context, content.demo_state))

    if model.dry_run:
        # A catalog named for the first time was created on the way: nothing of it is kept.
        transaction.set_rollback(True)
        return types.BlokEdit(applied=False, forked=provided, blok=cast("types.Blok", blok), revision=None, materialized_blok=cast("types.MaterializedBlok | None", mblok), diagnostics=diagnostics)

    target = mblok
    if provided:
        blok = _fork(blok, _free_name(blok.organization_id, blok.name), caller.user)
        base = blok.head
        if mblok is not None:
            target = models.MaterializedBlok.objects.create(blok=blok, name=mblok.name, description=mblok.description)

    revision = commit_revision(blok, content, base=base, author=caller.user, task=task, message=model.message)
    if target is not None and bound is not None:
        # Edited where it is shown, so shown as edited: an edit unpins the materialization it came through.
        if target.pinned_revision_id is not None:
            target.pinned_revision = None
            target.save(update_fields=["pinned_revision", "updated_at"])
        write_mappings(target, bound)
    return types.BlokEdit(applied=True, forked=provided, blok=cast("types.Blok", blok), revision=cast("types.BlokRevision", revision), materialized_blok=cast("types.MaterializedBlok | None", target), diagnostics=diagnostics)


@transaction.atomic
def revert_blok(info: Info, input: inputs.RevertBlokInput) -> types.BlokRevision:
    """Make an earlier revision's content the blok's head again, as a new revision on top of ``base``."""
    caller = CallerContext.from_info(info)
    blok = _custom(info, input.blok)
    try:
        earlier = blok.revisions.get(id=input.to)
        base = blok.revisions.get(id=input.base)
    except models.BlokRevision.DoesNotExist:
        raise ValueError(f"Blok '{blok.name}' has no revision {input.to} or {input.base}.")
    message = input.message or f"Back to revision {earlier.number}"
    revision = commit_revision(blok, content_of_revision(earlier), base=base, author=caller.user, task=_task_of(caller, input.task), message=message)
    return cast("types.BlokRevision", revision)

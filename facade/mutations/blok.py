"""Blok mutations: create, update, delete.

Every read here is scoped to the caller's organization. ``scoped_get`` raises ``PermissionError``
for a wrong-tenant id so it is indistinguishable from a missing one; deletes use a scoped filter
for the same reason.
"""

from typing import Iterable

from django.db import transaction
from kante.types import Info

from facade import inputs, models, types
from facade.catalog_validation import dump_diagnostics, validate_manifest_against_catalog
from facade.types.base import scoped_get
from rekuest_core.inputs import models as rimodels


def _sync_blok_dependencies(blok: models.Blok, dependencies: Iterable[rimodels.AgentDependencyInputModel] | None, *, replace: bool) -> list[models.BlokDependency]:
    """Upsert ``BlokDependency`` rows by ``(blok, key)`` from a manifest.

    Writes every declared field (``optional`` included: before this helper only demands and the
    app/version filters were persisted, so ``optional`` could never be honoured downstream).
    With ``replace`` every existing key that is not declared any more is deleted; a
    ``BlokAgentMapping`` pointing at it keeps its row with ``dependency=NULL`` (``SET_NULL``).
    """
    existing = {dep.key: dep for dep in blok.dependencies.all()}
    synced: list[models.BlokDependency] = []
    for declared in dependencies or []:
        dep, _ = models.BlokDependency.objects.update_or_create(
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
                prefered_instances=declared.prefered_instances,
                assign_policy=declared.assign_policy,
            ),
        )
        synced.append(dep)
        existing.pop(declared.key, None)

    if replace:
        for stale in existing.values():
            stale.delete()

    return synced


def _catalog_for(info: Info, name: str | None) -> models.UICatalog:
    """The named catalog in the caller's organization, created empty if it does not exist yet."""
    return models.UICatalog.objects.get_or_create(name=name or "default", organization=info.context.request.organization)[0]


@transaction.atomic
def create_blok(info: Info, input: inputs.CreateBlokInput) -> types.Blok:
    """Create or replace (by name) a blok in the caller's organization."""
    model = input.to_pydantic()
    organization = info.context.request.organization
    catalog = _catalog_for(info, model.catalog)
    diagnostics = validate_manifest_against_catalog(catalog, model.components)

    # ``organization`` is part of the lookup, not the defaults: keyed on name alone this was a
    # global upsert, so any user could overwrite another organization's blok by name.
    blok, _ = models.Blok.objects.update_or_create(
        name=model.name,
        organization=organization,
        defaults=dict(
            components=[c.model_dump() for c in model.components or []],
            description=model.description,
            creator=info.context.request.user,
            catalog=catalog,
            demo_state=model.demo_state or {},
            diagnostics=dump_diagnostics(diagnostics),
        ),
    )

    # create is an upsert on (name, organization); dependencies a previous registration declared
    # but this one does not are stale and go too.
    _sync_blok_dependencies(blok, model.dependencies, replace=True)

    return blok


def delete_blok(info: Info, input: inputs.DeleteBlokInput) -> bool:
    """Delete a blok in the caller's organization; False when there is none."""
    deleted, _ = models.Blok.objects.filter(id=input.id, organization=info.context.request.organization).delete()
    return deleted > 0


@transaction.atomic
def update_blok(info: Info, input: inputs.UpdateBlokInput) -> types.Blok:
    """Partially update a blok in the caller's organization."""
    model = input.to_pydantic()
    blok = scoped_get(models.Blok, info, model.id, field="organization")

    if model.name is not None:
        blok.name = model.name
    if model.description is not None:
        blok.description = model.description
    if model.components is not None:
        blok.components = [c.model_dump() for c in model.components]
    if model.demo_state is not None:
        blok.demo_state = model.demo_state
    if model.catalog is not None:
        blok.catalog = _catalog_for(info, model.catalog)

    # A partial update is validated as the manifest it results in: new components against the
    # dependencies and demo state that will be in force after the update.
    dependency_keys = {dep.key for dep in model.dependencies} if model.dependencies is not None else set(blok.dependencies.values_list("key", flat=True))
    components = [rimodels.ComponentNodeInputModel(**c) for c in blok.components]
    rimodels.check_blok_manifest(components, dependency_keys, set(blok.demo_state or {}))
    blok.diagnostics = dump_diagnostics(validate_manifest_against_catalog(blok.catalog, components))

    blok.save()

    if model.dependencies is not None:
        _sync_blok_dependencies(blok, model.dependencies, replace=True)

    return blok

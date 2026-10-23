"""Materialized blok mutations: materialize, update mappings, delete.

A materialization binds every declared ``BlokDependency`` of a blok to a concrete ``Agent``.
Mappings are validated in full before anything is written, and each mutation runs in one
transaction, so a failed materialization leaves no ``MaterializedBlok``, mapping or placement
behind. All lookups are scoped to the caller's organization.
"""

from collections.abc import Sequence
from typing import cast

from authentikate.models import Organization
from django.db import transaction
from kante.types import Info

from facade import enums, inputs, models, types
from facade.blok_revisions import dependency_input_of
from facade.caller_context import CallerContext
from facade.types.base import scoped_get
from rekuest_core.inputs import models as rimodels


def organization_of(info: Info) -> Organization:
    organization = CallerContext.from_info(info).organization
    if organization is None:
        raise PermissionError("This request is not made for an organization.")
    return organization


def check_agent_mappings(organization: Organization, blok_name: str, declared: Sequence[rimodels.AgentDependencyInputModel], wanted: dict[str, str]) -> dict[str, models.Agent]:
    """Validate the wanted bindings (dependency key → agent id) against declared dependencies without writing.

    Rules: every mapped key must be a declared dependency; every non-optional dependency must be
    mapped; every mapped agent must belong to ``organization`` and satisfy the dependency's
    ``app`` / ``version`` filter when set. Returns ``{key: agent}``.
    """
    by_key = {dep.key: dep for dep in declared}

    unknown = sorted(set(wanted) - set(by_key))
    if unknown:
        raise ValueError(f"Blok '{blok_name}' declares no dependency named {unknown}.")

    resolved: dict[str, models.Agent] = {}
    for key, dep in by_key.items():
        agent_id = wanted.get(key)
        if agent_id is None:
            if dep.optional:
                continue
            raise ValueError(f"Dependency '{key}' of blok '{blok_name}' is required but no agent was mapped.")

        try:
            agent = models.Agent.objects.select_related("app", "release").get(id=agent_id, organization=organization)
        except models.Agent.DoesNotExist:
            raise PermissionError(f"No Agent {agent_id} in this organization.")

        if dep.app and agent.app.identifier != dep.app:
            raise ValueError(f"Agent {agent.pk} runs app '{agent.app.identifier}' but dependency '{key}' requires '{dep.app}'.")
        if dep.version and dep.version != "*" and agent.release.version != dep.version:
            raise ValueError(f"Agent {agent.pk} runs version '{agent.release.version}' but dependency '{key}' requires '{dep.version}'.")

        resolved[key] = agent

    return resolved


def _resolve_agent_mappings(organization: Organization, blok: models.Blok, wanted: dict[str, str]) -> dict[str, models.Agent]:
    """:func:`check_agent_mappings` against the dependencies the blok declares now."""
    return check_agent_mappings(organization, blok.name, [dependency_input_of(dep) for dep in blok.dependencies.all()], wanted)


def _refuse_declared(mblok: models.MaterializedBlok | None) -> None:
    """An agent's own materialization follows its registrations, not a user."""
    if mblok is not None and mblok.declared_by_id is not None:
        raise ValueError(f"Materialized blok '{mblok.name}' is its agent's and changes with its registrations.")


def write_mappings(mblok: models.MaterializedBlok, resolved: dict[str, models.Agent]) -> None:
    """Replace the materialization's bindings with ``resolved``, each pointing at its dependency row."""
    mblok.agent_mappings.all().delete()
    dependencies = {dep.key: dep for dep in mblok.blok.dependencies.all()}
    for key, agent in resolved.items():
        # ``key`` is what ``unique_dependency_per_materialized_blok`` is built on; omitting it
        # (as this code once did) collapsed every mapping onto the default key and made the
        # second dependency of a blok violate the constraint.
        models.BlokAgentMapping.objects.create(materialized_blok=mblok, key=key, dependency=dependencies[key], agent=agent)


@transaction.atomic
def materialize_blok(info: Info, input: inputs.MaterializeBlokInput) -> types.MaterializedBlok:
    """Create a materialization of a blok with validated agent bindings, optionally placed on a dashboard."""
    organization = organization_of(info)
    blok = scoped_get(models.Blok, info, input.blok, field="organization")
    for_message = input.scope == enums.MaterializationScope.MESSAGE
    # A message shows a provided blok as it is now, bound for that one message: nothing to fork.
    if blok.app_id is not None and not for_message:
        raise ValueError(f"Blok '{blok.name}' is provided by its agents, each materializing it for itself; fork it to bind other agents.")
    if for_message and input.dashboard:
        raise ValueError("A materialization made for a message is shown in it, not placed on a dashboard.")
    dashboard = scoped_get(models.Dashboard, info, input.dashboard, field="organization") if input.dashboard else None

    resolved = _resolve_agent_mappings(organization, blok, {mapping.key: str(mapping.agent) for mapping in input.agent_mappings or []})

    # Each materialization is its own instance: the same blok can live on several dashboards
    # with different agent bindings.
    mblok = models.MaterializedBlok.objects.create(
        blok=blok,
        name=input.name or blok.name,
        description=input.description or blok.description or "",
        scope=input.scope.value,
    )
    write_mappings(mblok, resolved)

    if dashboard is not None:
        models.DashboardPlacement.objects.create(dashboard=dashboard, blok=mblok)

    return cast("types.MaterializedBlok", mblok)


def delete_materialized_blok(info: Info, input: inputs.DeleteMaterializedBlokInput) -> bool:
    """Delete a materialization in the caller's organization; False when there is none."""
    rows = models.MaterializedBlok.objects.filter(id=input.id, blok__organization=info.context.request.organization)
    _refuse_declared(rows.first())
    deleted, _ = rows.delete()
    return deleted > 0


@transaction.atomic
def update_materialized_blok(info: Info, input: inputs.UpdateMaterializedBlokInput) -> types.MaterializedBlok:
    """Replace the agent bindings of a materialization in the caller's organization."""
    mblok = scoped_get(models.MaterializedBlok, info, input.id, field="blok__organization")
    _refuse_declared(mblok)

    if input.agent_mappings is not None:
        # Full replacement, validated before the old mappings are dropped.
        wanted = {mapping.key: mapping.agent for mapping in (given.to_pydantic() for given in input.agent_mappings)}
        resolved = _resolve_agent_mappings(organization_of(info), mblok.blok, wanted)
        write_mappings(mblok, resolved)

    mblok.save()
    return cast("types.MaterializedBlok", mblok)


@transaction.atomic
def pin_materialized_blok(info: Info, input: inputs.PinMaterializedBlokInput) -> types.MaterializedBlok:
    """Keep a materialization at one revision of its blok, or let it follow the head again."""
    mblok = scoped_get(models.MaterializedBlok, info, input.id, field="blok__organization")
    _refuse_declared(mblok)
    if input.revision is None:
        mblok.pinned_revision = None
    else:
        try:
            mblok.pinned_revision = models.BlokRevision.objects.get(id=input.revision, blok_id=mblok.blok_id)
        except models.BlokRevision.DoesNotExist:
            raise ValueError(f"Blok '{mblok.blok.name}' has no revision {input.revision}.")
    mblok.save(update_fields=["pinned_revision", "updated_at"])
    return cast("types.MaterializedBlok", mblok)

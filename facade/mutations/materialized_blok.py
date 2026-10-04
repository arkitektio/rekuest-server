"""Materialized blok mutations: materialize, update mappings, delete.

A materialization binds every declared ``BlokDependency`` of a blok to a concrete ``Agent``.
Mappings are validated in full before anything is written, and each mutation runs in one
transaction, so a failed materialization leaves no ``MaterializedBlok``, mapping or placement
behind. All lookups are scoped to the caller's organization.
"""

from typing import cast

from authentikate.models import Organization
from django.db import transaction
from kante.types import Info

from facade import inputs, models, types
from facade.caller_context import CallerContext
from facade.types.base import scoped_get


def _organization(info: Info) -> Organization:
    organization = CallerContext.from_info(info).organization
    if organization is None:
        raise PermissionError("This request is not made for an organization.")
    return organization


def _resolve_agent_mappings(organization: Organization, blok: models.Blok, wanted: dict[str, str]) -> dict[models.BlokDependency, models.Agent]:
    """Validate the wanted bindings (dependency key → agent id) against the blok's declared dependencies without writing.

    Rules: every mapped key must be a declared dependency; every non-optional dependency must be
    mapped; every mapped agent must belong to ``organization`` and satisfy the dependency's
    ``app_filter`` / ``version_filter`` when set. Returns ``{dependency: agent}``.
    """
    declared = {dep.key: dep for dep in blok.dependencies.all()}

    unknown = sorted(set(wanted) - set(declared))
    if unknown:
        raise ValueError(f"Blok '{blok.name}' declares no dependency named {unknown}.")

    resolved: dict[models.BlokDependency, models.Agent] = {}
    for key, dep in declared.items():
        agent_id = wanted.get(key)
        if agent_id is None:
            if dep.optional:
                continue
            raise ValueError(f"Dependency '{key}' of blok '{blok.name}' is required but no agent was mapped.")

        try:
            agent = models.Agent.objects.select_related("app", "release").get(id=agent_id, organization=organization)
        except models.Agent.DoesNotExist:
            raise PermissionError(f"No Agent {agent_id} in this organization.")

        if dep.app_filter and agent.app.identifier != dep.app_filter:
            raise ValueError(f"Agent {agent.pk} runs app '{agent.app.identifier}' but dependency '{key}' requires '{dep.app_filter}'.")
        if dep.version_filter and dep.version_filter != "*" and agent.release.version != dep.version_filter:
            raise ValueError(f"Agent {agent.pk} runs version '{agent.release.version}' but dependency '{key}' requires '{dep.version_filter}'.")

        resolved[dep] = agent

    return resolved


def _write_mappings(mblok: models.MaterializedBlok, resolved: dict[models.BlokDependency, models.Agent]) -> None:
    for dep, agent in resolved.items():
        # ``key`` is what ``unique_dependency_per_materialized_blok`` is built on; omitting it
        # (as this code once did) collapsed every mapping onto the default key and made the
        # second dependency of a blok violate the constraint.
        models.BlokAgentMapping.objects.create(materialized_blok=mblok, key=dep.key, dependency=dep, agent=agent)


@transaction.atomic
def materialize_blok(info: Info, input: inputs.MaterializeBlokInput) -> types.MaterializedBlok:
    """Create a materialization of a blok with validated agent bindings, optionally placed on a dashboard."""
    organization = _organization(info)
    blok = scoped_get(models.Blok, info, input.blok, field="organization")
    dashboard = scoped_get(models.Dashboard, info, input.dashboard, field="organization") if input.dashboard else None

    resolved = _resolve_agent_mappings(organization, blok, {mapping.key: str(mapping.agent) for mapping in input.agent_mappings or []})

    # Each materialization is its own instance: the same blok can live on several dashboards
    # with different agent bindings.
    mblok = models.MaterializedBlok.objects.create(
        blok=blok,
        name=input.name or blok.name,
        description=input.description or blok.description or "",
    )
    _write_mappings(mblok, resolved)

    if dashboard is not None:
        models.DashboardPlacement.objects.create(dashboard=dashboard, blok=mblok)

    return cast("types.MaterializedBlok", mblok)


def delete_materialized_blok(info: Info, input: inputs.DeleteMaterializedBlokInput) -> bool:
    """Delete a materialization in the caller's organization; False when there is none."""
    deleted, _ = models.MaterializedBlok.objects.filter(id=input.id, blok__organization=info.context.request.organization).delete()
    return deleted > 0


@transaction.atomic
def update_materialized_blok(info: Info, input: inputs.UpdateMaterializedBlokInput) -> types.MaterializedBlok:
    """Replace the agent bindings of a materialization in the caller's organization."""
    mblok = scoped_get(models.MaterializedBlok, info, input.id, field="blok__organization")

    if input.agent_mappings is not None:
        # Full replacement, validated before the old mappings are dropped.
        wanted = {mapping.key: mapping.agent for mapping in (given.to_pydantic() for given in input.agent_mappings)}
        resolved = _resolve_agent_mappings(_organization(info), mblok.blok, wanted)
        mblok.agent_mappings.all().delete()
        _write_mappings(mblok, resolved)

    mblok.save()
    return cast("types.MaterializedBlok", mblok)

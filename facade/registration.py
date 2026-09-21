"""What registering an agent means, independent of the transport that asked.

An agent registers twice over: it *ensures* its row (identity from the token's client,
user and organization; a memory shelve beside it) and it *implements* what it offers
(implementations, states, locks and bloks, reconciled in one transaction). Shelving is the
runtime counterpart: a value the agent holds in memory gets a drawer on its shelve.

Both the GraphQL mutations (``ensureAgent``, ``implementAgent``, ``shelveInMemoryDrawer``,
``unshelveMemoryDrawer``) and the agent socket (``Register``, ``Implement``, ``Shelve``,
``Unshelve`` in :mod:`facade.message_router`) call these functions, so the two transports
cannot drift. Everything here is synchronous ORM code: the socket wraps it in
``database_sync_to_async``.

Only :mod:`facade.models` is imported at module scope. ``implement_agent`` reaches into
:mod:`facade.mutations` lazily: importing that package pulls in the consumers, which import
the router, which is imported by the consumers this module serves.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from django.db import transaction

from facade import models, unique
from rekuest_core.objects.models import DiagnosticModel

if TYPE_CHECKING:
    from authentikate.models import Client, Organization, User

    from facade.mutations.agent import ImplementAgentInputModel
    from rekuest_core.inputs.models import StateImplementationInputModel

logger = logging.getLogger(__name__)


def ensure_agent(client: "Client", user: "User", organization: "Organization", *, name: str | None = None) -> models.Agent:
    """The agent for this identity, created with its memory shelve if it did not exist.

    ``name`` names a *new* agent (without it, the client's id does); an existing agent keeps
    its name, which ``updateAgent`` owns. Idempotent: nothing an existing agent holds is
    touched (see :func:`clear_drawers` for what a fresh process discards).
    """
    agent, _ = models.Agent.objects.get_or_create(
        client=client,
        user=user,
        organization=organization,
        defaults=dict(
            name=name or f"{client.client_id}",
            app=client.release.app,
            release=client.release,
        ),
    )

    models.MemoryShelve.objects.get_or_create(
        agent=agent,
        defaults=dict(
            name=f"{str(agent)} memory shelve",
            creator=user,
            organization=agent.organization,
        ),
    )
    return agent


def clear_drawers(agent: models.Agent) -> int:
    """Forget every drawer on the agent's shelve: a process that just started holds nothing.

    Returns how many drawers were dropped.
    """
    drawers = models.MemoryDrawer.objects.filter(shelve__agent=agent)
    count = drawers.count()
    for drawer in drawers:
        drawer.delete()
    return count


def _register_state(agent: models.Agent, inputstate: "StateImplementationInputModel") -> models.State:
    """Upsert one of the agent's states, defaulting the identity fields: ``key`` falls back
    to the interface, ``app_identifier`` to the agent's app identifier."""
    state_definition, _ = models.StateDefinition.objects.update_or_create(
        hash=unique.hash_state_definition(inputstate.definition),
        organization=agent.organization,
        defaults=dict(
            name=inputstate.definition.name,
            ports=[i.model_dump() for i in inputstate.definition.ports],
            description="A state definition",
        ),
    )

    state, _ = models.State.objects.update_or_create(
        interface=inputstate.interface,
        agent=agent,
        defaults=dict(
            definition=state_definition,
            key=inputstate.key or inputstate.interface,
            app_identifier=inputstate.app or agent.app.identifier,
        ),
    )
    return state


@transaction.atomic
def implement_agent(client: "Client", user: "User", organization: "Organization", payload: "ImplementAgentInputModel") -> tuple[models.Agent, list[DiagnosticModel]]:
    """Reconcile an agent's declared implementations/states/locks/bloks in one transaction.

    Atomicity matters here: a validation error on the Nth implementation (e.g. a malformed
    requires/provides descriptor key) must not leave the agent half-registered with the
    stale-implementation reap skipped -- either the whole declared set lands, or none of it.
    A catalog mismatch is such an error: the registration is refused as a whole.

    Returns the agent and the non-fatal findings of this registration (the diagnostics
    stored on its implementations and bloks), so the transport can hand them back.
    """
    from facade.catalog_validation import dump_diagnostics, validate_manifest_against_catalog
    from facade.mutations.blok import _sync_blok_dependencies
    from facade.mutations.implementation import _create_implementation
    from facade.registration_lock import lock_organization

    # Before ANY row is read or written: a registration writes org-shared rows (actions,
    # protocols, collections, bloks) interleaved with agent-owned ones, so two agents of one
    # fleet registering at once would lock them in declaration order and deadlock. Taking it
    # before the Agent upsert also keeps that row's lock short — it is the row every heartbeat
    # renewal and every lease claim for this agent needs.
    lock_organization(organization)

    agent, _ = models.Agent.objects.update_or_create(
        client=client,
        user=user,
        organization=organization,
        defaults=dict(
            name=payload.name or f"{client.client_id}",
            app=client.release.app,
            release=client.release,
            hash=payload.hash or str(uuid.uuid4()),
        ),
    )

    diagnostics: list[DiagnosticModel] = []
    created_implementations_id = []
    created_states_id = []

    for lock in payload.locks or []:
        # update_or_create: a redeclared description takes effect instead of being
        # silently kept from the first registration.
        models.Lock.objects.update_or_create(
            agent=agent,
            key=lock.key,
            defaults=dict(
                description=lock.definition.description,
            ),
        )

    # Batch prefetch for the per-implementation loop: one Action query + one Implementation
    # query for the whole declared set instead of two lookups per implementation. Scoped to
    # the agent's app/org, exactly what _create_implementation's per-row lookups filter on.
    # Deterministic order, so two registrations of overlapping sets behave identically.
    declared_implementations = sorted(payload.implementations or [], key=lambda impl: (impl.definition.key, impl.definition.version))
    action_map = None
    implementation_map = None
    if declared_implementations:
        wanted = {(impl.definition.key, impl.definition.version) for impl in declared_implementations}
        action_map = {
            (action.key, action.version): action
            for action in models.Action.objects.filter(
                app=agent.app,
                organization=agent.organization,
                key__in={key for key, _ in wanted},
                version__in={version for _, version in wanted},
            )
        }
        implementation_map = {implementation.interface: implementation for implementation in models.Implementation.objects.filter(agent=agent).select_related("action")}

    for implementation in declared_implementations:
        created_implementation = _create_implementation(implementation, agent, action_map=action_map, implementation_map=implementation_map)
        created_implementations_id.append(created_implementation.id)
        diagnostics.extend(DiagnosticModel(**d) for d in (created_implementation.diagnostics or []))

    for inputstate in payload.states or []:
        state = _register_state(agent, inputstate)
        created_states_id.append(state.id)

    # Reap everything the agent no longer declares. Queryset delete still emits per-instance
    # signals (the subscription fan-out in facade.signals), without the per-row get() loops.
    #
    # Implementations carrying non-terminal tasks are kept: an agent that re-registers without an
    # implementation it is still executing must not have that work deleted out from under it.
    # ``Task.implementation`` is SET_NULL, so reaping an idle implementation preserves its history.
    models.State.objects.filter(agent=agent).exclude(id__in=created_states_id).delete()

    stale_implementations = models.Implementation.objects.filter(agent=agent).exclude(id__in=created_implementations_id)
    live = stale_implementations.filter(tasks__is_done=False).distinct()
    live_ids = list(live.values_list("id", flat=True))
    if live_ids:
        logger.warning(f"Keeping {len(live_ids)} undeclared implementation(s) for agent {agent.id}: still running tasks.")
    stale_implementations.exclude(id__in=live_ids).delete()

    for blok in payload.bloks or []:
        catalog = models.UICatalog.objects.get_or_create(name=blok.catalog or "default", organization=agent.organization)[0]
        blok_diagnostics = validate_manifest_against_catalog(catalog, blok.components)
        diagnostics.extend(blok_diagnostics)

        x, _ = models.Blok.objects.update_or_create(
            name=blok.key,
            organization=agent.organization,
            defaults=dict(
                components=[x.model_dump() for x in blok.components] if blok.components else [],
                description=blok.description,
                creator=user,
                catalog=catalog,
                demo_state=blok.demo_state or {},
                diagnostics=dump_diagnostics(blok_diagnostics),
            ),
        )

        # One auto-materialization per agent-declared blok, every dependency bound to the
        # declaring agent itself.
        mblok, _ = models.MaterializedBlok.objects.update_or_create(
            blok=x,
            declared_by=agent,
            defaults=dict(name=x.name, description=x.description or ""),
        )

        for dep in _sync_blok_dependencies(x, blok.dependencies, replace=True):
            models.BlokAgentMapping.objects.update_or_create(
                materialized_blok=mblok,
                key=dep.key,
                defaults=dict(dependency=dep, agent=agent),
            )

    return agent, diagnostics


def shelve(agent: models.Agent, *, identifier: str, resource_id: str, label: str | None = None, description: str | None = None) -> models.MemoryDrawer:
    """Record that ``agent`` holds ``resource_id`` (an ``identifier``) in memory.

    Upserts the drawer on the agent's shelve, keyed by ``resource_id``; the shelve exists
    since :func:`ensure_agent`.
    """
    memory_shelve, _ = models.MemoryShelve.objects.get_or_create(
        agent=agent,
        defaults=dict(
            name=f"{str(agent)} memory shelve",
            creator=agent.user,
            organization=agent.organization,
        ),
    )

    drawer, _ = models.MemoryDrawer.objects.update_or_create(
        shelve=memory_shelve,
        resource_id=resource_id,
        defaults=dict(
            label=label,
            description=description,
            identifier=identifier,
        ),
    )
    return drawer


def unshelve(agent: models.Agent, drawer_id: str) -> None:
    """Drop the drawer ``drawer_id`` from ``agent``'s shelve.

    Raises:
        ValueError: If there is no such drawer, or it sits on another agent's shelve.
    """
    try:
        drawer = models.MemoryDrawer.objects.select_related("shelve").get(id=drawer_id)
    except (models.MemoryDrawer.DoesNotExist, ValueError):
        raise ValueError(f"Unknown drawer {drawer_id!r}") from None
    if drawer.shelve.agent_id != agent.pk:
        raise ValueError("This drawer does not belong to this agent.")
    drawer.delete()

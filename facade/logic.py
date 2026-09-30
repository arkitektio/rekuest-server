from django.db.models import Exists, OuterRef
from rekuest_core.inputs.models import ActionDemandInputModel

from facade import models, managers
from kante.types import Info
from dataclasses import dataclass
import logging
import uuid
import jsonpatch

logger = logging.getLogger(__name__)


def auto_resolve(info: Info, implementation: models.Implementation, resolution: models.Resolution, visited_implementations: set[str] | None = None) -> None:
    if not visited_implementations:
        visited_implementations = set()

    for dependency in implementation.dependencies.all():
        logger.debug("Resolving dependency %s", dependency)
        agentsqs = models.Agent.objects.filter(organization=info.context.request.organization)

        matched_ids: dict[str, list[int]] = {}

        action_dependencies = dependency.get_action_dependencies()
        # An empty demand still hits the matcher's "No search params provided" guard.
        per_demand_ids = managers.get_action_ids_by_action_demands(
            [action_dependency.demand or ActionDemandInputModel() for action_dependency in action_dependencies],
            organization_id=info.context.request.organization.id,
        )

        for action_dependency, new_ids in zip(action_dependencies, per_demand_ids):
            if len(new_ids) == 0:
                raise ValueError(f"No actions found that match the given action demands {action_dependency}")

            # Further logic to create resolution would go here.
            agentsqs = agentsqs.filter(implementations__action__id__in=new_ids)
            matched_ids[action_dependency.key] = new_ids

        # The agent must ALSO satisfy every state demand of the dependency — each demand may
        # be met by a different State of the agent (same AND pattern as the action demands).
        for state_dependency in dependency.get_state_dependencies():
            if not state_dependency.demand:
                continue

            state_filters = managers.state_demand_state_filters(state_dependency.demand)

            if "definition_id__in" in state_filters and not state_filters["definition_id__in"]:
                raise ValueError(f"No state definitions found that match the given state demand {state_dependency}")

            agentsqs = agentsqs.filter(Exists(models.State.objects.filter(agent=OuterRef("pk"), **state_filters)))

        # todo: select best agent from agentsqs
        selected_agent = agentsqs.first()

        if not selected_agent:
            raise ValueError(f"No agent found that can satisfy dependency {dependency.key}")

        for key, action_id_list in matched_ids.items():
            implementations = models.Implementation.objects.filter(
                action__id__in=action_id_list,
                agent=selected_agent,
            )

            count = 0

            for impl in implementations:
                if impl.id in visited_implementations:
                    continue
                else:
                    if impl.dependencies.exists():
                        visited_implementations.add(impl.id)
                        sresolution = models.Resolution.objects.create(
                            name=f"Auto-resolve for {dependency}{key} on {implementation}",
                            implementation=impl,
                            creator=info.context.request.user,
                            organization=info.context.request.organization,
                        )
                        auto_resolve(info, impl, sresolution, visited_implementations=visited_implementations)

                        models.ResolvedDependency.objects.create(
                            key=key,
                            resolution=resolution,
                            dependency=dependency,
                            resolution_key=str(uuid.uuid4()),
                            implementation=impl,
                            down_stream_resolution=sresolution,
                        )
                    else:
                        models.ResolvedDependency.objects.create(
                            key=key,
                            resolution=resolution,
                            dependency=dependency,
                            resolution_key=str(uuid.uuid4()),
                            implementation=impl,
                        )
                        visited_implementations.add(impl.id)

                    count += 1
                # No preference means no limit: every viable instance is resolved.
                if dependency.prefered_instances is not None and count >= dependency.prefered_instances:
                    break


def get_latest_state(
    agent: models.Agent,
    state_id: int | None = None,
    session_id: str | None = None,
    global_revision: int | None = None,
    forward_patch_count: int = 0,
    backward_patch_count: int = 0,
) -> dict:
    """Materialize an agent's states within ONE session, at ``global_revision`` or the latest.

    ``global_rev`` numbers the patches of a session (it restarts with every agent process), so
    every read here is scoped to the session and ordered by ``global_rev``: the anchor is the
    newest snapshot at or before the target revision, and the patches after it are applied in
    revision order. Ordering by receive timestamp, or mixing sessions, applied a previous
    process's patches (or re-sent ones) on top of the wrong base.
    """
    if not session_id:
        t = models.Session.objects.filter(agent=agent).order_by("-created_at").first()
    else:
        t = models.Session.objects.get(agent=agent, session_id=session_id)

    if t is None:
        return {"states": {}, "global_revision": 0, "forward_patches": [], "backward_patches": [], "session_id": None, "timestamp": None}

    states_data = {}
    max_current_revision = 0
    latest_timestamp = t.created_at

    qs = models.State.objects.filter(agent=agent)
    if state_id:
        qs = qs.filter(id=state_id)

    for state in qs:
        snapshot_qs = models.Snapshot.objects.filter(state=state, agent=agent, session=t)
        if global_revision is not None:
            snapshot_qs = snapshot_qs.filter(global_rev__lte=global_revision)
        snapshot = snapshot_qs.order_by("-global_rev", "-id").first()
        if not snapshot:
            raise ValueError(f"No snapshot found for state {state_id or state.pk}")

        patches_qs = models.Patch.objects.filter(state=state, session=t, global_rev__gt=snapshot.global_rev)
        if global_revision is not None:
            patches_qs = patches_qs.filter(global_rev__lte=global_revision)

        current_value = snapshot.value
        current_global_revision = snapshot.global_rev
        if snapshot.timestamp > latest_timestamp:
            latest_timestamp = snapshot.timestamp

        for patch in patches_qs.order_by("global_rev", "id"):
            # Handle 'remove' operations which shouldn't have a 'value' key
            patch_doc = {"op": patch.op, "path": patch.path}
            if patch.op != "remove":
                patch_doc["value"] = patch.value
            try:
                current_value = jsonpatch.JsonPatch([patch_doc]).apply(current_value)
            except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException, KeyError, IndexError, TypeError) as e:
                # A patch that does not apply means the recorded history is broken (a lost or
                # out-of-order patch). Skip it so the rest of the state still materializes, but
                # say so: silently swallowing this is how corrupted states went unnoticed.
                logger.warning(
                    "State %s (agent %s, session %s): patch %s at revision %s does not apply: %s",
                    state.interface,
                    agent.pk,
                    t.session_id,
                    patch.pk,
                    patch.global_rev,
                    e,
                )
                continue
            current_global_revision = patch.global_rev
            if patch.timestamp > latest_timestamp:
                latest_timestamp = patch.timestamp

        # Track the highest global revision across all states we process
        if current_global_revision > max_current_revision:
            max_current_revision = current_global_revision

        states_data[state.interface] = current_value

    # Use the requested target revision if provided, otherwise the max we just calculated
    reference_rev = global_revision if global_revision is not None else max_current_revision

    # Fetch n-patches forward at the agent level (not scoped to state, but to the session:
    # revisions of different sessions are unrelated)
    forward_patches = []
    if forward_patch_count > 0:
        forward_patches = list(models.Patch.objects.filter(agent=agent, session=t, global_rev__gt=reference_rev).order_by("global_rev")[:forward_patch_count])

    # Fetch n-patches backward at the agent level
    backward_patches = []
    if backward_patch_count > 0:
        backward_patches = list(models.Patch.objects.filter(agent=agent, session=t, global_rev__lte=reference_rev).order_by("-global_rev")[:backward_patch_count][::-1])  # Reverse to maintain chronological order

    # Return a structured payload since patches are now an agent-level property
    return {"states": states_data, "global_revision": max_current_revision, "forward_patches": forward_patches, "backward_patches": backward_patches, "session_id": t.session_id, "timestamp": latest_timestamp}

"""Dependency state_demands: persistence, rehydration and agent selection.

State demands on a Dependency were previously persisted (update path only) but never read —
``get_state_dependencies()`` did not exist and no matcher consumed them. These tests pin the full
loop: both write paths persist the demands, ``get_state_dependencies()`` rehydrates them, and
``auto_resolve`` only selects agents whose States match every state demand (same AND
semantics as the action demands).
"""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from authentikate.models import Organization, User

from facade import models
from facade.logic import auto_resolve
from rekuest_core.enums import PortKind
from rekuest_core.inputs.models import (
    ImplementationInputModel,
    PortMatchInputModel,
    StateDemandInputModel,
    StateDependencyInputModel,
)
from tests.factories import create_agent_for_registry, create_registry_bundle
from tests.registered import create_implementation as _create_implementation

SNAP_DEFINITION = {
    "key": "snap",
    "version": "1",
    "name": "Snap",
    "kind": "FUNCTION",
    "args": [{"key": "exposure", "kind": "INT", "nullable": False}],
    "returns": [],
}


def _orchestrator_input(state_port_kind: str = "INT") -> ImplementationInputModel:
    return ImplementationInputModel.model_validate(
        {
            "interface": "orchestrate",
            "definition": {
                "key": "orchestrate",
                "version": "1",
                "name": "Orchestrate",
                "kind": "FUNCTION",
                "args": [],
                "returns": [],
            },
            "dependencies": [
                {
                    "key": "snap",
                    "action_dependencies": [
                        {"key": "snap", "demand": {"name": "Snap"}}
                    ],
                    "state_dependencies": [
                        {
                            "key": "counter",
                            "demand": {"matches": [{"kind": state_port_kind}]},
                        }
                    ],
                    "prefered_instances": 1,
                }
            ],
        }
    )


def _agent_in(org: Organization, prefix: str) -> models.Agent:
    user, _, _, caller = create_registry_bundle(prefix)
    return create_agent_for_registry(caller, user, org, prefix)


@dataclass(frozen=True)
class Setup:
    """The orchestrator's implementation, the two candidate agents and who asks."""

    org: Organization
    user: User
    #: A stand-in for the resolver's ``Info``: ``auto_resolve`` reads only
    #: ``info.context.request.organization`` and ``.user``.
    info: SimpleNamespace
    main_impl: models.Implementation
    agent_with_state: models.Agent
    agent_without_state: models.Agent
    counter_definition: models.StateDefinition


@pytest.fixture
def setup(db: None) -> Setup:
    """One org: an orchestrator (declares the dependency) and two snap-capable agents,
    only one of which carries a matching State."""
    user, _, org, caller = create_registry_bundle("statedep")
    orchestrator_agent = create_agent_for_registry(caller, user, org, "statedep-main")

    agent_with_state = _agent_in(org, "statedep-stateful")
    agent_without_state = _agent_in(org, "statedep-stateless")
    for agent in (agent_with_state, agent_without_state):
        _create_implementation(
            ImplementationInputModel.model_validate(
                {"interface": "snap", "definition": SNAP_DEFINITION}
            ),
            agent,
        )

    counter_definition = models.StateDefinition.objects.create(
        organization=org,
        name="Counter",
        hash="statedep-counter-hash",
        description="counter",
        ports=[
            {
                "key": "count",
                "kind": "INT",
                "identifier": None,
                "nullable": False,
                "children": [],
            }
        ],
    )
    models.State.objects.create(
        definition=counter_definition,
        interface="counter",
        key="counter",
        app_identifier="statedep-stateful-app",
        agent=agent_with_state,
    )

    main_impl = _create_implementation(_orchestrator_input(), orchestrator_agent)
    info = SimpleNamespace(
        context=SimpleNamespace(request=SimpleNamespace(organization=org, user=user))
    )
    return Setup(
        org=org,
        user=user,
        info=info,
        main_impl=main_impl,
        agent_with_state=agent_with_state,
        agent_without_state=agent_without_state,
        counter_definition=counter_definition,
    )


def test_state_demands_persist_on_both_write_paths(setup: Setup) -> None:
    # Create path (regression: it used to drop state_demands entirely).
    dependency = setup.main_impl.dependencies.get(key="snap")
    assert dependency.state_demands, "create path dropped state_demands"
    assert dependency.prefered_instances == 1

    # Update path (same hash → skip-unchanged fast path, but dependencies still sync).
    agent = setup.main_impl.agent
    again = _create_implementation(_orchestrator_input(), agent)
    dependency = again.dependencies.get(key="snap")
    assert dependency.state_demands
    assert dependency.prefered_instances == 1

    demands = dependency.get_state_dependencies()
    assert len(demands) == 1
    assert isinstance(demands[0], StateDependencyInputModel)
    demand = demands[0].demand
    assert demand is not None and demand.matches is not None
    matches = demand.matches
    assert matches[0].kind is not None
    assert matches[0].kind.value == "INT"


def test_auto_resolve_selects_only_agents_with_matching_state(setup: Setup) -> None:
    resolution = models.Resolution.objects.create(
        name="statedep-resolution",
        implementation=setup.main_impl,
        creator=setup.user,
        organization=setup.org,
    )

    auto_resolve(setup.info, setup.main_impl, resolution)

    resolved = models.ResolvedDependency.objects.filter(resolution=resolution)
    assert resolved.count() == 1
    assert resolved.get().implementation.agent == setup.agent_with_state


def test_auto_resolve_raises_when_no_state_definition_matches(setup: Setup) -> None:
    # Re-register the orchestrator with a state demand no StateDefinition satisfies.
    main_impl = _create_implementation(
        _orchestrator_input(state_port_kind="DATE"), setup.main_impl.agent
    )
    resolution = models.Resolution.objects.create(
        name="statedep-nomatch",
        implementation=main_impl,
        creator=setup.user,
        organization=setup.org,
    )

    with pytest.raises(ValueError, match="No state definitions found"):
        auto_resolve(setup.info, main_impl, resolution)


def test_state_demand_by_key_only_selects_matching_agent(setup: Setup) -> None:
    """A demand can pin a state purely by its identity key (no port matches) — the old
    'state_key-only demands' TODO."""
    from facade import managers

    demand = StateDemandInputModel(key="counter")
    filters = managers.state_demand_state_filters(demand)
    assert filters == {"key": "counter"}

    from django.db.models import Exists, OuterRef

    queryset = models.Agent.objects.filter(
        Exists(models.State.objects.filter(agent=OuterRef("pk"), **filters))
    )
    assert set(queryset.values_list("pk", flat=True)) == {setup.agent_with_state.pk}

    # Pinning by app identifier works the same way.
    app_filters = managers.state_demand_state_filters(
        StateDemandInputModel(app="statedep-stateful-app")
    )
    queryset = models.Agent.objects.filter(
        Exists(models.State.objects.filter(agent=OuterRef("pk"), **app_filters))
    )
    assert set(queryset.values_list("pk", flat=True)) == {setup.agent_with_state.pk}


def test_agent_state_demand_exists_semantics(setup: Setup) -> None:
    """Mirrors AgentFilter.state_demands: one Exists() per demand over facade_statedefinition
    (the old code targeted a nonexistent facade_stateschema table and always errored)."""
    from django.db.models import Exists, OuterRef

    from facade import managers

    matches = [PortMatchInputModel(kind=PortKind.INT)]
    fitting_ids = managers.get_state_ids_by_demands(matches)
    assert setup.counter_definition.pk in fitting_ids

    queryset = models.Agent.objects.filter(
        Exists(
            models.State.objects.filter(
                agent=OuterRef("pk"), definition_id__in=fitting_ids
            )
        )
    )
    assert set(queryset.values_list("pk", flat=True)) == {setup.agent_with_state.pk}

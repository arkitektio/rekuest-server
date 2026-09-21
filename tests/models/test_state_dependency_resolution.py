"""Dependency state_demands: persistence, rehydration and agent selection.

State demands on a Dependency were previously persisted (update path only) but never read —
``get_state_dependencies()`` did not exist and no matcher consumed them. These tests pin the full
loop: both write paths persist the demands, ``get_state_dependencies()`` rehydrates them, and
``auto_resolve`` only selects agents whose States match every state demand (same AND
semantics as the action demands).
"""

from types import SimpleNamespace

import pytest

from rekuest_core.inputs.models import ImplementationInputModel, StateDependencyInputModel

from facade import models
from facade.logic import auto_resolve
from facade.mutations.implementation import _create_implementation

from tests.factories import create_agent_for_registry, create_registry_bundle

SNAP_DEFINITION = {
    "key": "snap",
    "version": "1",
    "name": "Snap",
    "kind": "FUNCTION",
    "args": [{"key": "exposure", "kind": "INT", "nullable": False}],
    "returns": [],
}


def _orchestrator_input(state_port_kind="INT"):
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
                    "action_dependencies": [{"key": "snap", "demand": {"name": "Snap"}}],
                    "state_dependencies": [{"key": "counter", "demand": {"matches": [{"kind": state_port_kind}]}}],
                    "prefered_instances": 1,
                }
            ],
        }
    )


def _agent_in(org, prefix):
    user, _, _, caller = create_registry_bundle(prefix)
    return create_agent_for_registry(caller, user, org, prefix)


@pytest.fixture
def setup(db):
    """One org: an orchestrator (declares the dependency) and two snap-capable agents,
    only one of which carries a matching State."""
    user, _, org, caller = create_registry_bundle("statedep")
    orchestrator_agent = create_agent_for_registry(caller, user, org, "statedep-main")

    agent_with_state = _agent_in(org, "statedep-stateful")
    agent_without_state = _agent_in(org, "statedep-stateless")
    for agent in (agent_with_state, agent_without_state):
        _create_implementation(ImplementationInputModel.model_validate({"interface": "snap", "definition": SNAP_DEFINITION}), agent)

    counter_definition = models.StateDefinition.objects.create(
        organization=org,
        name="Counter",
        hash="statedep-counter-hash",
        description="counter",
        ports=[{"key": "count", "kind": "INT", "identifier": None, "nullable": False, "children": []}],
    )
    models.State.objects.create(definition=counter_definition, interface="counter", key="counter", app_identifier="statedep-stateful-app", agent=agent_with_state, value={"count": 0})

    main_impl = _create_implementation(_orchestrator_input(), orchestrator_agent)
    info = SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(organization=org, user=user)))
    return SimpleNamespace(org=org, user=user, info=info, main_impl=main_impl, agent_with_state=agent_with_state, agent_without_state=agent_without_state, counter_definition=counter_definition)


def test_state_demands_persist_on_both_write_paths(setup):
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
    assert demands[0].demand.matches[0].kind.value == "INT"


def test_auto_resolve_selects_only_agents_with_matching_state(setup):
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


def test_auto_resolve_raises_when_no_state_definition_matches(setup):
    # Re-register the orchestrator with a state demand no StateDefinition satisfies.
    main_impl = _create_implementation(_orchestrator_input(state_port_kind="DATE"), setup.main_impl.agent)
    resolution = models.Resolution.objects.create(
        name="statedep-nomatch",
        implementation=main_impl,
        creator=setup.user,
        organization=setup.org,
    )

    with pytest.raises(ValueError, match="No state definitions found"):
        auto_resolve(setup.info, main_impl, resolution)


def test_state_registration_defaults_identity_fields(db):
    """States registered without explicit key/app get the defaults: key = interface,
    app_identifier = the agent's app identifier; explicit values win."""
    from facade.registration import _register_state
    from rekuest_core.inputs.models import StateImplementationInputModel

    user, _, org, caller = create_registry_bundle("statereg")
    agent = create_agent_for_registry(caller, user, org, "statereg")

    definition = {"name": "Counter", "ports": [{"key": "count", "kind": "INT", "nullable": False}]}

    defaulted = _register_state(agent, StateImplementationInputModel.model_validate({"interface": "counter", "definition": definition}))
    assert defaulted.key == "counter"
    assert defaulted.app_identifier == "statereg-app"

    explicit = _register_state(agent, StateImplementationInputModel.model_validate({"interface": "counter_impl", "key": "counter", "app": "imagej", "definition": definition}))
    assert explicit.interface == "counter_impl"
    assert explicit.key == "counter"
    assert explicit.app_identifier == "imagej"


def test_state_demand_by_key_only_selects_matching_agent(setup):
    """A demand can pin a state purely by its identity key (no port matches) — the old
    'state_key-only demands' TODO."""
    from facade import managers

    demand = SimpleNamespace(hash=None, key="counter", app=None, matches=None, protocols=None)
    filters = managers.state_demand_state_filters(demand)
    assert filters == {"key": "counter"}

    from django.db.models import Exists, OuterRef

    queryset = models.Agent.objects.filter(Exists(models.State.objects.filter(agent=OuterRef("pk"), **filters)))
    assert set(queryset.values_list("pk", flat=True)) == {setup.agent_with_state.pk}

    # Pinning by app identifier works the same way.
    app_filters = managers.state_demand_state_filters(SimpleNamespace(hash=None, key=None, app="statedep-stateful-app", matches=None, protocols=None))
    queryset = models.Agent.objects.filter(Exists(models.State.objects.filter(agent=OuterRef("pk"), **app_filters)))
    assert set(queryset.values_list("pk", flat=True)) == {setup.agent_with_state.pk}


def test_agent_state_demand_exists_semantics(setup):
    """Mirrors AgentFilter.state_demands: one Exists() per demand over facade_statedefinition
    (the old code targeted a nonexistent facade_stateschema table and always errored)."""
    from django.db.models import Exists, OuterRef

    from facade import managers

    matches = [SimpleNamespace(at=None, key=None, kind=SimpleNamespace(value="INT"), identifier=None, nullable=None, children=None)]
    fitting_ids = managers.get_state_ids_by_demands(matches)
    assert setup.counter_definition.id in fitting_ids

    queryset = models.Agent.objects.filter(Exists(models.State.objects.filter(agent=OuterRef("pk"), definition_id__in=fitting_ids)))
    assert set(queryset.values_list("pk", flat=True)) == {setup.agent_with_state.pk}

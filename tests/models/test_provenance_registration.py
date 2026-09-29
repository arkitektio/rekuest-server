"""Audience is resolved at implementation registration, from the action's ports.

Drives the real ``_create_implementation`` path: an implementation that declares
no audience gets one derived from its arg/return structure identifiers, while an
explicit declaration is persisted verbatim. Both are stored on the model so
dispatch never recomputes them.
"""

import pytest

from rekuest_core.enums import Effects, Execution
from rekuest_core.inputs.models import DefinitionInputModel, ImplementationInputModel

from facade import enums
from facade.mutations.implementation import _create_implementation

from tests.factories import create_agent_for_registry, create_registry_bundle


def _definition():
    return DefinitionInputModel.model_validate(
        {
            "key": "thresholder",
            "version": "1",
            "name": "Thresholder",
            "kind": "FUNCTION",
            "args": [{"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": False}],
            "returns": [{"key": "mask", "kind": "STRUCTURE", "identifier": "@fluss/flow", "nullable": False}],
        }
    )


def _agent(prefix):
    user, _client, org, caller = create_registry_bundle(prefix)
    return create_agent_for_registry(caller, user, org, prefix)


@pytest.mark.django_db
def test_audience_derived_from_action_ports_at_registration():
    agent = _agent("prov-reg-derive")
    impl = _create_implementation(
        ImplementationInputModel(definition=_definition(), interface="thresholder"),
        agent,
    )
    # Derived from the arg (@mikro) + return (@fluss) structure ports.
    assert impl.provenance_audience == ["mikro", "fluss"]


@pytest.mark.django_db
def test_declared_audience_is_persisted_verbatim():
    agent = _agent("prov-reg-declared")
    impl = _create_implementation(
        ImplementationInputModel(
            definition=_definition(),
            interface="thresholder",
            provenance_audience=["explicit-service"],
        ),
        agent,
    )
    assert impl.provenance_audience == ["explicit-service"]


@pytest.mark.django_db
def test_an_implementation_is_plain_with_unknown_effects_by_default():
    agent = _agent("effect-default")
    impl = _create_implementation(
        ImplementationInputModel(definition=_definition(), interface="thresholder"),
        agent,
    )
    assert (impl.effects, impl.execution, impl.code_hash) == (
        enums.EffectsChoices.UNKNOWN,
        enums.ExecutionChoices.PLAIN,
        None,
    )


@pytest.mark.django_db
def test_effects_execution_and_code_hash_are_persisted():
    agent = _agent("effect-irreversible")
    impl = _create_implementation(
        ImplementationInputModel(definition=_definition(), interface="thresholder", effects=Effects.IRREVERSIBLE, execution=Execution.WORKFLOW, code_hash="abc"),
        agent,
    )
    # Re-read from the DB to confirm it actually persisted, not just set in memory.
    impl.refresh_from_db()
    assert (impl.effects, impl.execution, impl.code_hash) == ("IRREVERSIBLE", "WORKFLOW", "abc")


@pytest.mark.django_db
def test_re_registering_updates_effects_execution_and_code_hash():
    """Not create-only: a changed claim or new code takes effect on the next registration."""
    agent = _agent("effect-update")
    _create_implementation(ImplementationInputModel(definition=_definition(), interface="thresholder"), agent)
    impl = _create_implementation(
        ImplementationInputModel(definition=_definition(), interface="thresholder", effects=Effects.NONE, execution=Execution.WORKFLOW, code_hash="new"),
        agent,
    )
    impl.refresh_from_db()
    assert (impl.effects, impl.execution, impl.code_hash) == ("NONE", "WORKFLOW", "new")

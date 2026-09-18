"""``implement_agent`` must be all-or-nothing.

A validation error on the Nth declared implementation (here: a requires descriptor key the
JSONPath compiler refuses) used to leave implementations 1..N-1 registered and skip the
stale-implementation reap — a half-registered agent. The mutation is now wrapped in one
transaction: either the whole declared set lands, or none of it.

What counts as a refused key changed with ``8f55d58``: keys are now rendered as *quoted* jsonpath
member literals, which is the injection guard, so a key carrying quotes and operators is no longer
rejected — it is safely escaped (``tests/test_descriptors.py`` covers that directly). The key used
below is refused for a different and still-live reason: it normalizes to an empty member name.
"""

from types import SimpleNamespace

import pytest
from authentikate.models import App, Release

from facade import models
from facade.mutations.agent import ImplementAgentInputModel, implement_agent

from tests.factories import create_registry_bundle


def _implementation(interface, requires_key):
    return {
        "interface": interface,
        "definition": {
            "key": interface,
            "version": "1",
            "name": interface.title(),
            "kind": "FUNCTION",
            "args": [
                {
                    "key": "image",
                    "kind": "STRUCTURE",
                    "identifier": "@mikro/image",
                    "nullable": False,
                    "requires": [{"key": requires_key, "operator": "EQUALS", "value": "c"}],
                }
            ],
            "returns": [],
        },
    }


@pytest.mark.django_db
def test_implement_agent_rolls_back_on_malformed_descriptor():
    user, client, org, _ = create_registry_bundle("impl-atomic")
    client.release = Release.objects.create(app=App.objects.create(identifier="impl-atomic-app"), version="1.0.0")
    client.save()

    payload = ImplementAgentInputModel.model_validate(
        {
            "implementations": [
                _implementation("good", "axes"),
                # ``$.`` is stripped as the optional root prefix, leaving no member name at all —
                # a ValueError from the compiler, raised mid-loop. (Not ``""``: the input model
                # pins ``min_length=1``, so an empty key fails pydantic before the mutation runs,
                # which would not exercise the rollback at all.)
                _implementation("bad", "$."),
            ]
        }
    )
    fake_input = SimpleNamespace(to_pydantic=lambda: payload)
    info = SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(client=client, user=user, organization=org)))

    with pytest.raises(ValueError):
        implement_agent(info, fake_input)

    # Nothing from the batch survives — not the valid first implementation, not its action,
    # and not the agent row the mutation upserts.
    assert models.Implementation.objects.count() == 0
    assert models.Action.objects.count() == 0
    assert models.Agent.objects.filter(organization=org).count() == 0

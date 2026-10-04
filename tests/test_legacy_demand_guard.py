"""The legacy JSONB scanner must refuse demands it cannot express.

Models without relational port rows (shortcuts, state definitions) are matched by the
legacy JSONB scanner, which only understands key/kind/identifier. Descriptor
(requires/provides) and nullable matching used to be silently dropped there, so a
descriptor-bearing demand degraded to a purely structural one with no warning. These are
pure tests (no database): the guard raises while building the SQL, before any execution.
"""

from types import SimpleNamespace

from facade.managers import PortDemand

from rekuest_core.inputs.models import ActionDemandInputModel, DescriptorInputModel, PortMatchInputModel, StateDemandInputModel

import pytest

from facade.managers import build_state_params, get_action_ids_by_port_demands


def match(**overrides):
    base = dict(at=None, key=None, kind=None, identifier="@mikro/image", nullable=None, dimension=None, descriptors=None, children=None)
    base.update(overrides)
    return PortMatchInputModel(**base)


def demand(*matches):
    return PortDemand(kind="args", matches=list(matches))


def test_descriptor_demand_is_rejected_for_legacy_models():
    descriptors = [DescriptorInputModel(key="axes", value="c")]
    with pytest.raises(ValueError, match="Descriptor matching"):
        get_action_ids_by_port_demands([demand(match(descriptors=descriptors))], model="facade_shortcut")


def test_nested_descriptor_demand_is_rejected_for_legacy_models():
    descriptors = [DescriptorInputModel(key="axes", value="c")]
    nested = match(children=[match(descriptors=descriptors)])
    with pytest.raises(ValueError, match="Descriptor matching"):
        get_action_ids_by_port_demands([demand(nested)], model="facade_shortcut")


def test_nullable_demand_is_rejected_for_legacy_models():
    with pytest.raises(ValueError, match="nullable"):
        get_action_ids_by_port_demands([demand(match(nullable=False))], model="facade_shortcut")


def test_state_definition_matching_rejects_descriptors():
    descriptors = [DescriptorInputModel(key="axes", value="c")]
    with pytest.raises(ValueError, match="Descriptor matching"):
        build_state_params([match(descriptors=descriptors)])

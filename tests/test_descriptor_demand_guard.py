"""Descriptor-only root matches are rejected before reaching the database.

A root port match carrying only ``descriptors`` (no structural narrowing) would evaluate
``jsonb_path_match`` against every root port in the organization — the compiled predicate
is unindexable in that direction. The matcher requires a structural field alongside root
descriptors; nested children are exempt because their parent already narrows. Pure tests:
the guard raises while the SQL is being built.
"""

from types import SimpleNamespace

from facade.managers import PortDemand

from rekuest_core.inputs.models import ActionDemandInputModel, DescriptorInputModel, PortMatchInputModel, StateDemandInputModel
from rekuest_core.enums import PortKind

import pytest

from facade.managers import get_action_ids_by_port_demands, get_action_port_demand_subquery


def match(**overrides):
    base = dict(at=None, key=None, kind=None, identifier=None, nullable=None, dimension=None, descriptors=None, children=None)
    base.update(overrides)
    return PortMatchInputModel(**base)


def demand(*matches):
    return PortDemand(kind="args", matches=list(matches))


DESCRIPTORS = [DescriptorInputModel(key="axes", value="c")]


def test_descriptor_only_root_match_is_rejected():
    with pytest.raises(ValueError, match="narrow structurally"):
        get_action_ids_by_port_demands([demand(match(descriptors=DESCRIPTORS))])


def test_descriptor_only_root_match_is_rejected_in_subquery_form():
    with pytest.raises(ValueError, match="narrow structurally"):
        get_action_port_demand_subquery([demand(match(descriptors=DESCRIPTORS))])


def test_root_descriptors_with_structural_narrowing_are_allowed():
    for narrowing in (dict(identifier="@mikro/image"), dict(kind=PortKind.STRUCTURE), dict(key="image"), dict(at=0), dict(dimension="length")):
        get_action_port_demand_subquery([demand(match(descriptors=DESCRIPTORS, **narrowing))])


def test_descriptor_only_nested_child_is_allowed():
    parent = match(identifier="@mikro/image", children=[match(descriptors=DESCRIPTORS)])
    get_action_port_demand_subquery([demand(parent)])

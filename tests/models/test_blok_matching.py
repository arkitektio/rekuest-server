"""Which bloks an item opens in: a blok's args are matched like an action's.

A blok that declares args is opened for an item (``@args.<key>``). Its args are written as
``BlokArgPort`` rows, so the question an item's menu asks ("which bloks take this
``@mikro/image``, given what it is") runs on the relational engine actions use, ``requires``
included.
"""

import pytest

from facade import managers, models
from facade.managers import PortDemand
from rekuest_core.inputs.models import DescriptorInputModel, PortMatchInputModel
from tests.factories import create_action_for_organization, create_registry_bundle
from tests.registered import create_blok

IMAGE = {"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": False}
STACK = {**IMAGE, "requires": [{"key": "@mikro/z_size", "operator": "GTE", "value": 2}]}


def _image(**descriptors: object) -> PortDemand:
    match = PortMatchInputModel(at=0, identifier="@mikro/image", descriptors=[DescriptorInputModel(key=key, value=value) for key, value in descriptors.items()] or None)
    return PortDemand(kind="args", matches=[match])


def _bloks(demands: list[PortDemand], organization: object) -> set[str]:
    subquery = managers.get_blok_port_demand_subquery(demands, organization_id=organization.pk)
    return set(models.Blok.objects.filter(id__in=subquery).values_list("name", flat=True))


@pytest.mark.django_db
def test_a_blok_is_found_by_the_structure_its_args_take() -> None:
    """Only bloks whose first arg takes the image; bloks without args never match."""
    user, _, org, _ = create_registry_bundle("blokmatch")
    create_blok(org, user, "inspector", [IMAGE])
    create_blok(org, user, "tables", [{**IMAGE, "key": "table", "identifier": "@mikro/table"}])
    create_blok(org, user, "panel")

    assert _bloks([_image()], org) == {"inspector"}


@pytest.mark.django_db
def test_requires_decides_which_items_a_blok_opens_for() -> None:
    """A stack-only blok is offered for a stack, not for a plane; one without requires for both."""
    user, _, org, _ = create_registry_bundle("blokreq")
    create_blok(org, user, "stack-viewer", [STACK])
    create_blok(org, user, "any-viewer", [IMAGE])

    assert _bloks([_image(**{"@mikro/z_size": 10})], org) == {"stack-viewer", "any-viewer"}
    assert _bloks([_image(**{"@mikro/z_size": 1})], org) == {"any-viewer"}


@pytest.mark.django_db
def test_a_list_arg_is_matched_through_its_child() -> None:
    """Several selected images match a blok taking a list of them."""
    user, _, org, _ = create_registry_bundle("bloklist")
    create_blok(org, user, "gallery", [{"key": "images", "kind": "LIST", "nullable": False, "children": [IMAGE]}])
    create_blok(org, user, "inspector", [IMAGE])

    many = PortMatchInputModel(at=0, kind="LIST", children=[PortMatchInputModel(identifier="@mikro/image")])
    assert _bloks([PortDemand(kind="args", matches=[many])], org) == {"gallery"}


@pytest.mark.django_db
def test_force_length_counts_a_bloks_root_args() -> None:
    """Without an arg count column, the root rows are counted."""
    user, _, org, _ = create_registry_bundle("bloklen")
    create_blok(org, user, "single", [IMAGE])
    create_blok(org, user, "pair", [IMAGE, {**IMAGE, "key": "mask", "nullable": True}])

    assert _bloks([PortDemand(kind="args", matches=_image().matches, force_length=1)], org) == {"single"}
    assert _bloks([PortDemand(kind="args", force_non_nullable_length=1)], org) == {"single", "pair"}


@pytest.mark.django_db
def test_bloks_are_matched_within_the_organization_and_actions_are_untouched() -> None:
    """Another organization's blok is not offered; the action engine still answers for actions."""
    user, _, org, _ = create_registry_bundle("blokorg")
    other_user, _, other, _ = create_registry_bundle("blokother")
    create_blok(other, other_user, "elsewhere", [IMAGE])
    action = create_action_for_organization(org, "blokorg-act", args=[], returns=[])
    models.ArgPort.objects.create(action=action, index=0, key="image", key_path="image", kind="STRUCTURE", identifier="@mikro/image")

    assert _bloks([_image()], org) == set()
    assert managers.get_action_ids_by_port_demands([_image()], organization_id=org.id) == [action.id]


def test_a_blok_has_no_returns_to_demand() -> None:
    """A returns demand on bloks is refused, not answered with nothing."""
    with pytest.raises(ValueError, match="no returns ports"):
        managers._port_demand_sql([PortDemand(kind="returns", force_length=0)], owner=managers.BLOK_PORTS)

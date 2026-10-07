"""Shortcut port matching, descriptors included.

A shortcut keeps its own ``args``/``returns`` JSONB (its action's ports minus the saved ones)
and is matched by the JSONB scanner. The compiled requires/provides constraints live on the
action's relational port rows, so a descriptor-bearing demand is checked there: the row with
the same key as the shortcut's port must accept the candidate object.
"""

from types import SimpleNamespace

import pytest

from facade import managers, models
from facade.managers import PortDemand
from rekuest_core.enums import PortKind
from tests.factories import create_registry_bundle
from tests.models.test_action_matching import make_action, pm

IMAGE = {"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image", "nullable": False}
REQUIRES_C = [{"key": "axes", "operator": "EQUALS", "value": "c"}]


def shortcut_ids(matches, type="args"):
    return set(managers.get_action_ids_by_port_demands([PortDemand(kind=type, matches=matches)], model="facade_shortcut"))


@pytest.fixture
def shortcuts(db):
    user, client, org, _ = create_registry_bundle("shortcutorg")
    toolbox = models.Toolbox.objects.create(name="box", description="", creator=user, client=client, organization=org)

    def shortcut(prefix, args, saved=()):
        action = make_action(org, prefix, args=args)
        remaining = [arg for arg in args if arg["key"] not in saved]
        return models.Shortcut.objects.create(name=prefix, toolbox=toolbox, creator=user, action=action, args=remaining, saved_args={key: 1 for key in saved})

    # The structure REQUIRES axes == "c".
    strict = shortcut("sc-strict", [{**IMAGE, "requires": REQUIRES_C}])
    # No requires: accepts any object.
    loose = shortcut("sc-loose", [{**IMAGE, "requires": []}])
    # A saved first arg: the strict structure is the shortcut's arg 0, the action's arg 1.
    shifted = shortcut("sc-shifted", [{"key": "sigma", "kind": "INT"}, {**IMAGE, "requires": REQUIRES_C}], saved=("sigma",))
    # A list of strict structures.
    listed = shortcut("sc-list", [{"key": "images", "kind": "LIST", "children": [{**IMAGE, "key": "0", "requires": REQUIRES_C}]}])
    # No action behind it: nothing can accept a described object.
    orphan = models.Shortcut.objects.create(name="sc-orphan", toolbox=toolbox, creator=user, args=[IMAGE])
    return SimpleNamespace(strict=strict, loose=loose, shifted=shifted, listed=listed, orphan=orphan)


def image(at=0, **descriptors):
    return pm(at=at, kind=PortKind.STRUCTURE, identifier="@mikro/image", descriptors=descriptors or None)


def test_structural_match_ignores_constraints(shortcuts):
    assert shortcut_ids([image()]) == {shortcuts.strict.id, shortcuts.loose.id, shortcuts.shifted.id, shortcuts.orphan.id}


def test_descriptors_are_checked_on_the_actions_port(shortcuts):
    assert shortcut_ids([image(axes="c")]) == {shortcuts.strict.id, shortcuts.loose.id, shortcuts.shifted.id}
    assert shortcut_ids([image(axes="z")]) == {shortcuts.loose.id}


def test_list_children_carry_descriptors(shortcuts):
    def images(**descriptors):
        return pm(at=0, kind=PortKind.LIST, children=[image(**descriptors)])

    assert shortcut_ids([images()]) == {shortcuts.listed.id}
    assert shortcut_ids([images(axes="c")]) == {shortcuts.listed.id}
    assert shortcut_ids([images(axes="z")]) == set()

"""A hub with one service and one hook agent configured — two entries that share nothing."""

import pytest
from authentikate.models import Organization
from django.conf import settings as django_settings
from django.test import override_settings

from rekuest.configuration import HookAgentEntry, ServiceEntry
from arkitekt_service.service import Descriptor
from tests.hook_urls import housekeeping, janitor


class Room:
    """What the stand-in service hosts. Never saved: the catalog only needs its declaration."""


@pytest.fixture
def declared():
    """What the service hosts and emits, and what the agent offers, for this test only."""

    def tidy_up() -> dict:
        return {"acted": 3}

    janitor.action(interface="tidy_up", description="Tidy things up.")(tidy_up)
    housekeeping.structure(Room, "@housekeeping/room", label="Room", description="A room to tidy.", descriptors=[Descriptor("@housekeeping/area", "FLOAT", "Square metres")])
    housekeeping.signal("@housekeeping/room", kinds=["CREATED", "DELETED"], descriptors=["@housekeeping/area"], description="A room appeared or went.")
    yield tidy_up
    janitor._actions.clear()
    housekeeping._signals.clear()
    housekeeping._structures.clear()


@pytest.fixture
def hub(live_server, settings, declared):
    prefix = f"/{django_settings.MY_SCRIPT_NAME.strip('/')}" if django_settings.MY_SCRIPT_NAME else ""
    settings.ROOT_URLCONF = "tests.hook_urls"
    settings.SERVICES = [ServiceEntry(name="housekeeping", url=f"{live_server.url}{prefix}/_rekuest/service")]
    settings.HOOK_AGENTS = [HookAgentEntry(name="janitor", hook_url=f"{live_server.url}{prefix}/_rekuest/hook")]
    settings.REKUEST_SERVICE = {"REKUEST_URL": f"{live_server.url}{prefix}"}
    settings.REKUEST_HOOK = {"REKUEST_URL": f"{live_server.url}{prefix}"}
    return settings


def an_organization(slug: str) -> Organization:
    """An organization that is simply there — as one takt was first to see is. (One created
    while hook agents are configured gets its agents at once, in the background; the test for
    that is the only one that wants it.)"""
    with override_settings(HOOK_AGENTS=[]):
        return Organization.objects.create(slug=slug)


@pytest.fixture
def lab(db):
    """An organization of users: provisioning gives it the hook agent."""
    return an_organization("lab")

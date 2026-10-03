"""Services and their HookAgents: provisioning from a manifest — the service's catalog rows (the
structures it hosts, the signals it emits) and, in every organization, its agent with its actions
and schedules — and the vendored ``rekuest_service`` side (its hook endpoint and declaration API).

The agent row and its implementations are takt's (``fake_takt`` stands in for it); a run
travelling the whole way — Assign to the service, reports back — is takt's path too, judged by
rekuest-takt's conformance suite.
"""

from urllib.parse import urlparse

import pytest
from django.conf import settings as django_settings
from django.test import Client as HttpClient
from django.test import override_settings

from facade import enums, models, service_agents
from authentikate.models import Organization
from rekuest_service import Descriptor, HookAgent, Service, trust
from tests.hook_urls import housekeeper, housekeeping

pytestmark = pytest.mark.usefixtures("fake_takt")


class Room:
    """What the stand-in service hosts. Never saved: the catalog only needs its declaration."""



@pytest.fixture
def hook_action():
    """Declare the service and its agent's action for this test only."""

    def tidy_up() -> dict:
        return {"acted": 3}

    housekeeper.action(interface="tidy_up", description="Tidy the service up.", default_interval=60)(tidy_up)
    housekeeping.signal("@housekeeping/room", kinds=["CREATED", "DELETED"], descriptors=["@housekeeping/area"], description="A room appeared or went.")
    housekeeping.structure(Room, "@housekeeping/room", kinds=(), label="Room", description="A room to tidy.", descriptors=[Descriptor("@housekeeping/area", "FLOAT", "Square metres")])
    yield tidy_up
    housekeeper._actions.clear()
    housekeeping._signals.clear()
    housekeeping._structures.clear()


@pytest.fixture
def hub(live_server, settings, hook_action):
    prefix = f"/{django_settings.MY_SCRIPT_NAME.strip('/')}" if django_settings.MY_SCRIPT_NAME else ""
    settings.ROOT_URLCONF = "tests.hook_urls"
    settings.SERVICE_AGENTS = [{"service": "housekeeping", "hook_url": f"{live_server.url}{prefix}/_rekuest/hook"}]
    settings.REKUEST_HOOK = {"REKUEST_URL": f"{live_server.url}{prefix}"}
    return settings


def an_organization(slug: str) -> Organization:
    """An organization that is simply there — as one takt was first to see is. (One created
    while services are configured gets its agents at once, in the background; the test for
    that is the only one that wants it.)"""
    with override_settings(SERVICE_AGENTS=[]):
        return Organization.objects.create(slug=slug)


@pytest.fixture
def lab(db):
    """An organization of users: provisioning gives it the service's agent."""
    return an_organization("lab")


@pytest.mark.django_db(transaction=True)
class TestServiceAgents:
    def test_an_organization_gets_the_agent_its_actions_and_their_default_schedule(self, hub, lab):
        assert service_agents.provision_all() == []

        agent = models.Agent.objects.get(name="housekeeping", organization=lab)
        assert agent.kind == enums.AgentKind.WEBHOOK.value
        implementation = models.Implementation.objects.get(agent=agent, interface="tidy_up")
        assert implementation.needs_token is False

        schedule = models.Schedule.objects.get(agent=agent, interface="tidy_up")
        assert schedule.interval_seconds == 60 and schedule.ephemeral_runs is True
        assert schedule.caller.organization == lab
        # The scheduler is its own client: no service may receive echoes of its own runs.
        assert schedule.caller.client_id != agent.client_id

        # Idempotent, and in place: re-provisioning neither duplicates nor recreates.
        assert service_agents.provision_all() == []
        assert models.Agent.objects.filter(name="housekeeping", organization=lab).count() == 1
        assert models.Schedule.objects.get(pk=schedule.pk).agent_id == agent.pk

    def test_every_organization_has_its_own_agent_and_schedule(self, hub, lab):
        clinic = an_organization("clinic")
        service_agents.provision_all()

        agents = {agent.organization.slug: agent for agent in models.Agent.objects.filter(name="housekeeping")}
        assert set(agents) >= {"lab", "clinic"}
        # One organization switching its schedule off is that organization's business alone.
        models.Schedule.objects.filter(agent=agents["lab"]).update(enabled=False)
        service_agents.provision_all()
        assert models.Schedule.objects.get(agent=agents["lab"]).enabled is False
        assert models.Schedule.objects.get(agent=agents["clinic"]).enabled is True

    def test_there_is_no_internal_organization(self, hub, lab):
        service_agents.provision_all()
        assert not Organization.objects.filter(slug=service_agents.RETIRED_ORGANIZATION).exists()

    def test_the_agents_of_the_former_internal_organization_are_retired(self, hub, lab):
        internal = an_organization(service_agents.RETIRED_ORGANIZATION)
        manifest = service_agents.fetch_manifest(hub.SERVICE_AGENTS[0])
        old = service_agents.provision_agent(hub.SERVICE_AGENTS[0], manifest, internal)
        assert models.Schedule.objects.filter(agent=old).exists()

        service_agents.provision_all()
        assert not models.Agent.objects.filter(organization=internal).exists()
        assert not models.Schedule.objects.filter(caller__organization=internal).exists()
        assert models.Agent.objects.filter(name="housekeeping", organization=lab).exists()

    def test_a_service_is_catalogued_without_any_organization_or_agent(self, hub):
        Organization.objects.all().delete()
        assert service_agents.provision_all() == []

        service = models.Service.objects.get(name="housekeeping")
        assert (service.identifier, service.description) == ("live.arkitekt.housekeeping", "A test service.")
        declared = {(d.identifier, d.kind, tuple(d.descriptor_keys)) for d in service.signals.all()}
        assert declared == {("@housekeeping/room", "CREATED", ("@housekeeping/area",)), ("@housekeeping/room", "DELETED", ("@housekeeping/area",))}
        assert not models.Agent.objects.filter(name="housekeeping").exists()

    def test_a_service_without_actions_has_no_agent(self, hub, lab):
        housekeeper._actions.clear()
        service_agents.provision_all()
        assert models.Service.objects.filter(name="housekeeping").exists()
        assert not models.Agent.objects.filter(name="housekeeping").exists()

    def test_a_dropped_signal_declaration_is_removed(self, hub):
        service_agents.provision_all()
        housekeeping._signals.clear()
        housekeeping.signal("@housekeeping/room", kinds=["CREATED"])

        service_agents.provision_all()
        assert list(models.SignalDeclaration.objects.values_list("kind", flat=True)) == ["CREATED"]

    def test_a_manifest_without_signals_declares_none(self, hub):
        housekeeping._signals.clear()
        assert service_agents.provision_all() == []
        assert models.SignalDeclaration.objects.count() == 0

    def test_provisioning_catalogues_what_the_service_hosts(self, hub):
        service_agents.provision_all()

        (hosted,) = models.StructureDeclaration.objects.all()
        assert (hosted.service.name, hosted.identifier, hosted.label, hosted.description) == ("housekeeping", "@housekeeping/room", "Room", "A room to tidy.")
        assert hosted.descriptors == [{"key": "@housekeeping/area", "type": "FLOAT", "description": "Square metres"}]

        # In place, and exactly the manifest: a structure no longer declared is no longer hosted.
        housekeeping._structures.clear()
        service_agents.provision_all()
        assert models.StructureDeclaration.objects.count() == 0

    def test_a_service_too_old_to_declare_structures_keeps_what_it_hosted(self, hub, monkeypatch):
        service_agents.provision_all()
        current = housekeeping.manifest()
        # rekuest-service before structures: the manifest has no such key at all.
        monkeypatch.setattr(housekeeping, "manifest", lambda: {key: value for key, value in current.items() if key != "structures"})

        assert service_agents.provision_all() == []
        assert models.StructureDeclaration.objects.filter(identifier="@housekeeping/room").exists()

    def test_a_structure_stays_with_the_service_that_hosted_it_first(self, hub):
        service_agents.provision_all()
        claimant = models.Service.objects.create(name="claimant")

        service_agents._sync_structures(claimant, [{"identifier": "@housekeeping/room", "label": "Stolen"}, {"identifier": "@claimant/thing"}])

        assert models.StructureDeclaration.objects.get(identifier="@housekeeping/room").service.name == "housekeeping"
        assert models.StructureDeclaration.objects.get(identifier="@claimant/thing").service == claimant

    def test_a_dropped_default_disables_its_schedule(self, hub, lab):
        service_agents.provision_all()
        schedule = models.Schedule.objects.get(interface="tidy_up", agent__organization=lab)
        housekeeper._actions["tidy_up"] = housekeeper._actions["tidy_up"].__class__(
            **{**housekeeper._actions["tidy_up"].__dict__, "default_interval": None}
        )

        service_agents.provision_all()
        assert models.Schedule.objects.get(pk=schedule.pk).enabled is False


@pytest.mark.django_db(transaction=True)
def test_an_unreachable_service_is_reported_and_the_others_still_provisioned(hub, lab):
    hub.SERVICE_AGENTS = [{"service": "offline", "hook_url": "http://127.0.0.1:9/_rekuest/hook"}, *hub.SERVICE_AGENTS]

    assert service_agents.provision_all() == ["offline"]
    assert models.Agent.objects.filter(client__client_id="rekuest:service-housekeeping").exists()


@pytest.mark.django_db(transaction=True)
def test_a_new_organization_gets_its_agents_on_the_fly(hub):
    """No provisioning pass is asked for: creating the organization is enough."""
    import time

    newcomer = Organization.objects.create(slug="newcomer")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not models.Schedule.objects.filter(agent__organization=newcomer).exists():
        time.sleep(0.05)
    assert models.Agent.objects.filter(name="housekeeping", organization=newcomer).exists()
    assert models.Schedule.objects.filter(agent__organization=newcomer, interface="tidy_up").exists()


@pytest.mark.django_db
class TestHookEndpoint:
    def test_an_unsigned_or_forged_request_is_refused(self, hub):
        client = HttpClient()
        url = urlparse(hub.SERVICE_AGENTS[0]["hook_url"]).path
        body = b'{"type": "ASSIGN", "task": "1", "interface": "tidy_up", "args": {}}'

        def post(authorization):
            headers = {"X-Rekuest-Agent": "7"}
            if authorization:
                headers["Authorization"] = authorization
            return client.post(url, data=body, content_type="application/json", headers=headers).status_code

        assert post(None) == 401
        # mikro's genuine key, speaking as rekuest: the bundle says whose key it is.
        impostor = trust.sign("POST", url, body, issuer="live.arkitekt.rekuest", audience="live.arkitekt.housekeeping", key=hub.TEST_SERVICE_KEYS["mikro"])
        assert post(impostor) == 401
        # rekuest's genuine token, but for another service.
        elsewhere = trust.sign("POST", url, body, issuer="live.arkitekt.rekuest", audience="live.arkitekt.mikro")
        assert post(elsewhere) == 401
        genuine = trust.sign("POST", url, body, issuer="live.arkitekt.rekuest", audience="live.arkitekt.housekeeping")
        assert post(genuine) != 401


class TestServiceDeclaration:
    """The declaration API itself, like an arkitekt App: decorators, handles, manifest."""

    def test_actions_take_name_and_description_from_the_docstring(self):
        service = Service("doc")
        agent = HookAgent(service)

        @agent.action(default_interval=30)
        def compact() -> dict:
            """Compact the store

            Merges small files into big ones.
            """
            return {}

        @agent.action
        def bare() -> dict:
            return {}

        (first, second) = service.manifest()["actions"]
        assert (first["interface"], first["name"], first["description"], first["default_interval"]) == ("compact", "Compact the store", "Merges small files into big ones.", 30)
        assert (second["interface"], second["name"]) == ("bare", "bare")

    def test_a_signal_handle_checks_its_kind(self):
        service = Service("kinds")
        created = service.signal("@kinds/thing", kinds=["CREATED"])
        both = service.signal("@kinds/other", kinds=["CREATED", "DELETED"])
        assert service.signal("@kinds/thing", kinds=["CREATED"]) is created  # same declaration, same handle
        with pytest.raises(ValueError):
            created.emit(1, organization="o", kind="DELETED")
        with pytest.raises(ValueError):
            both.emit(1, organization="o")  # two kinds declared: say which
        with pytest.raises(ValueError):
            service.signal("@kinds/thing", kinds=["DELETED"])  # redeclared differently
        with pytest.raises(ValueError):
            service.signal("@kinds/bad", kinds=["EXPLODED"])

    def test_two_services_do_not_share_declarations(self):
        a, b = Service("a"), Service("b")
        HookAgent(a).action(interface="only_a")(lambda: {})
        a.signal("@a/thing")
        assert [x["interface"] for x in a.manifest()["actions"]] == ["only_a"]
        assert b.manifest()["actions"] == [] and b.manifest()["signals"] == []

    def test_the_settings_name_overrides_the_declared_one(self, settings):
        settings.REKUEST_HOOK = {"REKUEST_URL": "http://x", "SERVICE": "mikro-2"}
        assert Service("mikro").service_name() == "mikro-2"
        settings.REKUEST_HOOK = {"REKUEST_URL": "http://x"}
        assert Service("mikro").service_name() == "mikro"

"""Service agents: provisioning from a manifest (its schedules and signal declarations), and the
vendored ``rekuest_service`` side (its hook endpoint and declaration API).

The agent row and its implementations are agentd's (``fake_agentd`` stands in for it); a run
travelling the whole way — Assign to the service, reports back — is agentd's path too, judged by
rekuest-agentd's conformance suite.
"""

import time
from urllib.parse import urlparse

import pytest
from django.conf import settings as django_settings
from django.test import Client as HttpClient

from facade import enums, models, service_agents
from rekuest_service import Service, trust
from tests.hook_urls import housekeeping

pytestmark = pytest.mark.usefixtures("fake_agentd")


@pytest.fixture
def hook_action():
    """Register the service's action for this test only."""

    def tidy_up() -> dict:
        return {"acted": 3}

    housekeeping.action(interface="tidy_up", description="Tidy the service up.", default_interval=60)(tidy_up)
    housekeeping.signal("@housekeeping/room", kinds=["CREATED", "DELETED"], descriptors=["@housekeeping/area"], description="A room appeared or went.")
    yield tidy_up
    housekeeping._actions.clear()
    housekeeping._signals.clear()


@pytest.fixture
def hub(live_server, settings, hook_action):
    prefix = f"/{django_settings.MY_SCRIPT_NAME.strip('/')}" if django_settings.MY_SCRIPT_NAME else ""
    settings.ROOT_URLCONF = "tests.hook_urls"
    settings.SERVICE_AGENTS = [{"service": "housekeeping", "hook_url": f"{live_server.url}{prefix}/_rekuest/hook"}]
    settings.REKUEST_HOOK = {"REKUEST_URL": f"{live_server.url}{prefix}"}
    return settings


@pytest.mark.django_db(transaction=True)
class TestServiceAgents:
    def test_provisioning_registers_the_manifest_and_its_default_schedule(self, hub):
        assert service_agents.provision_all(force=True) == 1

        agent = models.Agent.objects.get(name="housekeeping")
        assert agent.kind == enums.AgentKind.WEBHOOK.value
        assert agent.organization.slug == django_settings.SERVICE_AGENTS_ORGANIZATION
        implementation = models.Implementation.objects.get(agent=agent, interface="tidy_up")
        assert implementation.needs_token is False

        schedule = models.Schedule.objects.get(agent=agent, interface="tidy_up")
        assert schedule.interval_seconds == 60 and schedule.ephemeral_runs is True
        # The scheduler is its own client: no service may receive echoes of its own runs.
        assert schedule.caller.client_id != agent.client_id

        declared = {(d.identifier, d.kind, tuple(d.descriptor_keys)) for d in models.SignalDeclaration.objects.filter(agent=agent)}
        assert declared == {("@housekeeping/room", "CREATED", ("@housekeeping/area",)), ("@housekeeping/room", "DELETED", ("@housekeeping/area",))}

        # Idempotent, and in place: re-provisioning neither duplicates nor recreates.
        assert service_agents.provision_all(force=True) == 1
        assert models.Agent.objects.filter(name="housekeeping").count() == 1
        assert models.Schedule.objects.get(pk=schedule.pk).agent_id == agent.pk

    def test_a_dropped_signal_declaration_is_removed(self, hub):
        service_agents.provision_all(force=True)
        housekeeping._signals.clear()
        housekeeping.signal("@housekeeping/room", kinds=["CREATED"])

        service_agents.provision_all(force=True)
        assert list(models.SignalDeclaration.objects.values_list("kind", flat=True)) == ["CREATED"]

    def test_a_manifest_without_signals_declares_none(self, hub):
        housekeeping._signals.clear()
        assert service_agents.provision_all(force=True) == 1
        assert models.SignalDeclaration.objects.count() == 0

    def test_a_dropped_default_disables_its_schedule(self, hub):
        service_agents.provision_all(force=True)
        schedule = models.Schedule.objects.get(interface="tidy_up")
        housekeeping._actions["tidy_up"] = housekeeping._actions["tidy_up"].__class__(
            **{**housekeeping._actions["tidy_up"].__dict__, "default_interval": None}
        )

        service_agents.provision_all(force=True)
        assert models.Schedule.objects.get(pk=schedule.pk).enabled is False


@pytest.mark.django_db(transaction=True)
def test_an_unreachable_service_is_retried_sooner(settings, monkeypatch):
    settings.SERVICE_AGENTS = [{"service": "offline", "hook_url": "http://127.0.0.1:9/_rekuest/hook"}]
    monkeypatch.setattr(service_agents, "_next_provision_at", 0.0)

    assert service_agents.provision_all() == 0
    wait = service_agents._next_provision_at - time.monotonic()
    assert 0 < wait <= service_agents.PROVISION_RETRY_SECONDS


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

        @service.action(default_interval=30)
        def compact() -> dict:
            """Compact the store

            Merges small files into big ones.
            """
            return {}

        @service.action
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
        a.action(interface="only_a")(lambda: {})
        a.signal("@a/thing")
        assert [x["interface"] for x in a.manifest()["actions"]] == ["only_a"]
        assert b.manifest()["actions"] == [] and b.manifest()["signals"] == []

    def test_the_settings_name_overrides_the_declared_one(self, settings):
        settings.REKUEST_HOOK = {"REKUEST_URL": "http://x", "SERVICE": "mikro-2"}
        assert Service("mikro").service_name() == "mikro-2"
        settings.REKUEST_HOOK = {"REKUEST_URL": "http://x"}
        assert Service("mikro").service_name() == "mikro"

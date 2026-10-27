"""Hook agents: every organization gets each configured one, with the actions its manifest lists.

A hook agent is not a service's: the one under test (``janitor``) runs as its own instance, and
the hub in :class:`TestWithoutAnyService` has no service configured at all. Provisioning wires
nothing — no schedule, no trigger.

The agent row and its implementations are takt's (the real one registers them here); a run
travelling the whole way — Assign to the agent, reports back — is takt's path too, judged by
rekuest-takt's conformance suite.
"""

import time
from urllib.parse import urlparse

import pytest
from authentikate.models import Organization
from django.test import Client as HttpClient

from facade import enums, hook_agents, models, provisioning, schedules, service_catalog, triggers
from facade.inputs import ImplementAgentInputModel
from rekuest.configuration import HookAgentEntry
from arkitekt_service import trust
from tests import registered
from tests.hook_urls import janitor
from tests.provisioning_fixtures import an_organization, declared, hub, lab  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("takt")


@pytest.mark.django_db(transaction=True)
class TestHookAgents:
    def test_an_organization_gets_the_agent_and_its_actions(self, hub, lab):
        assert hook_agents.provision_all() == []

        agent = models.Agent.objects.get(name="janitor", organization=lab)
        assert agent.kind == enums.AgentKind.WEBHOOK.value
        assert agent.client.client_id == "rekuest:hook-janitor"
        implementation = models.Implementation.objects.get(agent=agent, interface="tidy_up")
        assert implementation.needs_token is False

        # Idempotent, and in place: re-provisioning neither duplicates nor recreates.
        assert hook_agents.provision_all() == []
        assert models.Agent.objects.get(name="janitor", organization=lab).pk == agent.pk

    def test_nothing_is_wired(self, hub, lab):
        """An agent offers actions; when one runs is the organization's own automation."""
        hook_agents.provision_all()
        assert models.Implementation.objects.filter(interface="tidy_up").exists()
        assert not models.Schedule.objects.exists()
        assert not models.Trigger.objects.exists()

    def test_every_organization_has_its_own_agent(self, hub, lab):
        an_organization("clinic")
        hook_agents.provision_all()
        assert {agent.organization.identifier for agent in models.Agent.objects.filter(name="janitor")} >= {"lab", "clinic"}

    def test_an_agent_that_offers_nothing_is_not_created(self, hub, lab):
        janitor._actions.clear()
        assert hook_agents.provision_all() == []
        assert not models.Agent.objects.filter(name="janitor").exists()

    def test_provisioning_an_agent_touches_no_catalog_row(self, hub, lab):
        hook_agents.provision_all()
        assert not models.Service.objects.exists()
        assert not models.StructureDeclaration.objects.exists() and not models.SignalDeclaration.objects.exists()

    def test_the_agents_once_minted_for_services_are_retired(self, hub, lab):
        user, client = hook_agents._identity("service-housekeeping", lab)
        former, _ = registered.implement_agent(client, user, lab, ImplementAgentInputModel(name="housekeeping", implementations=[]))
        assert former.client.client_id.startswith(hook_agents.FORMER_CLIENT_PREFIX)

        hook_agents.provision_all()
        assert not models.Agent.objects.filter(pk=former.pk).exists()
        assert models.Agent.objects.filter(name="janitor", organization=lab).exists()

    def test_an_unreachable_agent_is_reported_and_the_others_still_provisioned(self, hub, lab):
        hub.HOOK_AGENTS = [HookAgentEntry(name="offline", hook_url="http://127.0.0.1:9/_rekuest/hook"), *hub.HOOK_AGENTS]

        assert hook_agents.provision_all() == ["offline"]
        assert models.Agent.objects.filter(client__client_id="rekuest:hook-janitor").exists()

    def test_a_new_organization_gets_its_agents_on_the_fly(self, hub):
        """No provisioning pass is asked for: creating the organization is enough."""
        newcomer = Organization.objects.create(identifier="newcomer")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not models.Implementation.objects.filter(agent__organization=newcomer).exists():
            time.sleep(0.05)
        assert models.Implementation.objects.filter(agent__name="janitor", agent__organization=newcomer, interface="tidy_up").exists()


@pytest.mark.django_db(transaction=True)
class TestWithoutAnyService:
    def test_a_hub_with_no_service_still_has_its_hook_agents(self, hub, lab):
        hub.SERVICES = []
        assert provisioning.provision_all() == provisioning.Failed(services=[], hook_agents=[])
        assert models.Agent.objects.filter(name="janitor", organization=lab).exists()
        # Only rekuest's own structures are catalogued.
        assert list(models.Service.objects.values_list("name", flat=True)) == [service_catalog.SELF]


@pytest.mark.django_db
class TestHookEndpoint:
    def test_an_unsigned_or_forged_request_is_refused(self, hub):
        client = HttpClient()
        url = urlparse(hub.HOOK_AGENTS[0].hook_url).path
        body = b'{"type": "ASSIGN", "task": "1", "interface": "tidy_up", "args": {}}'

        def post(authorization):
            headers = {"X-Rekuest-Agent": "7"}
            if authorization:
                headers["Authorization"] = authorization
            return client.post(url, data=body, content_type="application/json", headers=headers).status_code

        assert post(None) == 401
        # mikro's genuine key, speaking as rekuest: the bundle says whose key it is.
        impostor = trust.sign("POST", url, body, issuer="rekuest", audience="janitor", key=hub.TEST_SERVICE_KEYS["mikro"])
        assert post(impostor) == 401
        # rekuest's genuine token, but for another instance — the service next door included.
        for elsewhere in ("mikro", "housekeeping"):
            assert post(trust.sign("POST", url, body, issuer="rekuest", audience=elsewhere)) == 401
        genuine = trust.sign("POST", url, body, issuer="rekuest", audience="janitor")
        assert post(genuine) != 401


#: An agent offering an action that takes a structure: what arkitekt-service's manifest version 2
#: says for ``@agent.action(args=[StructurePort("metric", "@mikro/metric", nullable=True)])``.
INGEST = hook_agents.HookManifest.model_validate(
    {
        "description": "Ingests metrics.",
        "actions": [
            {
                "interface": "ingest_metrics",
                "name": "Ingest metrics",
                "args": [{"key": "metric", "kind": "STRUCTURE", "identifier": "@mikro/metric", "nullable": True, "description": "The metric written."}],
            }
        ],
    }
)


@pytest.mark.django_db(transaction=True)
class TestHookActionPorts:
    def test_a_manifest_without_ports_is_an_action_without_arguments(self):
        (action,) = hook_agents.HookManifest.model_validate({"actions": [{"interface": "tidy_up"}]}).actions
        assert action.args == []

    def test_a_structure_port_becomes_the_actions_argument(self, lab):
        agent = hook_agents.provision_agent(HookAgentEntry(name="kraph", hook_url="http://127.0.0.1:9/_rekuest/hook"), INGEST, lab)
        implementation = models.Implementation.objects.get(agent=agent, interface="ingest_metrics")
        (port,) = models.ArgPort.objects.filter(action=implementation.action, parent__isnull=True)
        assert (port.key, port.kind, port.identifier, port.nullable) == ("metric", "STRUCTURE", "@mikro/metric", True)

    def test_a_trigger_on_the_structure_can_feed_it(self, lab):
        agent = hook_agents.provision_agent(HookAgentEntry(name="kraph", hook_url="http://127.0.0.1:9/_rekuest/hook"), INGEST, lab)
        action = models.Implementation.objects.get(agent=agent, interface="ingest_metrics").action
        assert triggers.target_port(action, "metric", "@mikro/metric").key == "metric"
        triggers.check_args(action, "metric", "@mikro/metric", {})
        with pytest.raises(ValueError):
            triggers.target_port(action, "metric", "@mikro/annotation")

    def test_a_schedule_runs_it_without_an_object(self, lab):
        """The same action on a schedule: the nullable port is simply left out."""
        agent = hook_agents.provision_agent(HookAgentEntry(name="kraph", hook_url="http://127.0.0.1:9/_rekuest/hook"), INGEST, lab)
        action = models.Implementation.objects.get(agent=agent, interface="ingest_metrics").action
        schedules.validate(action=action, agent=None, interface=None, args={}, interval_seconds=60, cron=None, tz="UTC")

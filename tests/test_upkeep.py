"""Upkeep: the two jobs takt asks this server for (``facade/upkeep.py``), and the health check
that answers for takt.

There is no reaper process: takt keeps the time and calls ``_rekuest/upkeep/<job>``, signed with
the instance key the pair shares. That takt really calls them (and when) is judged in takt.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from django.conf import settings as django_settings
from django.db import connection, connections
from django.test import Client as HttpClient
from django.urls import reverse

from embeddings import engine
from facade import models, service_agents
from facade.service_trust import rekuest_identifier
from rekuest_service import trust
from tests.models.test_action_embedding import _action
from tests.test_service_agents import hook_action, hub, lab  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("fake_takt")


def _post(job: str, authorization: str | None = "sign", body: bytes = b"{}"):
    path = reverse("upkeep", kwargs={"job": job})
    if authorization == "sign":
        identity = rekuest_identifier()
        authorization = trust.sign("POST", path, body, issuer=identity, audience=identity)
    headers = {"Authorization": authorization} if authorization else {}
    return HttpClient().post(path, data=body, content_type="application/json", headers=headers)


@pytest.mark.django_db
class TestOnlyTaktMayAsk:
    def test_an_unsigned_request_is_refused(self):
        assert _post("provision", authorization=None).status_code == 401

    def test_another_instances_key_is_refused_even_if_the_hub_vouches_for_it(self, settings):
        """mikro's key is in the trust bundle, but only this instance's own key is takt's."""
        path = reverse("upkeep", kwargs={"job": "provision"})
        identity = rekuest_identifier()
        forged = trust.sign("POST", path, b"{}", issuer=identity, audience=identity, key=settings.TEST_SERVICE_KEYS["mikro"])
        assert _post("provision", authorization=forged).status_code == 401

    def test_a_token_for_another_request_is_refused(self):
        identity = rekuest_identifier()
        elsewhere = trust.sign("POST", reverse("upkeep", kwargs={"job": "reembed"}), b"{}", issuer=identity, audience=identity)
        assert _post("provision", authorization=elsewhere).status_code == 401
        other_body = trust.sign("POST", reverse("upkeep", kwargs={"job": "provision"}), b'{"x": 1}', issuer=identity, audience=identity)
        assert _post("provision", authorization=other_body).status_code == 401

    def test_a_token_is_good_once(self):
        path = reverse("upkeep", kwargs={"job": "reembed"})
        identity = rekuest_identifier()
        token = trust.sign("POST", path, b"{}", issuer=identity, audience=identity)
        assert _post("reembed", authorization=token).status_code == 200
        assert _post("reembed", authorization=token).status_code == 409

    def test_an_unknown_job_or_method_is_refused(self):
        assert _post("vacuum").status_code == 404
        assert HttpClient().get(reverse("upkeep", kwargs={"job": "provision"})).status_code == 405


@pytest.mark.django_db(transaction=True)
class TestProvision:
    def test_it_provisions_the_configured_services(self, hub, lab):
        response = _post("provision")

        assert response.status_code == 200
        assert response.json() == {"ok": True, "skipped": False, "failed": []}
        assert models.Agent.objects.filter(name="housekeeping").exists()
        assert models.Schedule.objects.filter(interface="tidy_up").count() == 1

    def test_it_says_which_service_failed(self, hub):
        hub.SERVICE_AGENTS = [{"service": "offline", "hook_url": "http://127.0.0.1:9/_rekuest/hook"}, *hub.SERVICE_AGENTS]

        assert _post("provision").json() == {"ok": False, "skipped": False, "failed": ["offline"]}

    def test_a_pass_already_running_elsewhere_is_left_to_it(self, hub, lab):
        """Another replica holds the lock: this one does nothing, and says so."""
        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [service_agents.PROVISION_LOCK_KEY])
            assert _post("provision").json() == {"ok": True, "skipped": True, "failed": []}
            assert not models.Agent.objects.filter(name="housekeeping").exists()
        finally:
            other.close()

        assert _post("provision").json()["skipped"] is False

    def test_passes_racing_from_real_threads_leave_one_schedule(self, hub, lab):
        start = threading.Barrier(4)
        answers: list = []

        def run() -> None:
            try:
                start.wait(timeout=10)
                answers.append(service_agents.provision_all())
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert len(answers) == 4 and [] in answers
        assert all(answer in ([], None) for answer in answers)
        assert models.Schedule.objects.filter(interface="tidy_up").count() == 1
        assert models.Agent.objects.filter(name="housekeeping").count() == 1


@pytest.mark.django_db(transaction=True)
class TestReembed:
    def test_it_embeds_what_takt_registered_without_a_vector(self):
        action = _action("upkeep-heal", name="Blur", description="Gaussian blur of an image")
        models.Action.objects.filter(pk=action.pk).update(embedding=None, embedding_model="")

        assert _post("reembed").json() == {"embedded": 1, "more": False}

        action.refresh_from_db()
        assert action.embedding is not None and action.embedding_model == engine.model_id()
        assert _post("reembed").json() == {"embedded": 0, "more": False}

    def test_it_says_when_a_call_did_not_finish_the_work(self, settings, monkeypatch):
        from facade import upkeep

        settings.EMBEDDINGS = {**engine._settings(), "SWEEP_BATCH_SIZE": 1}
        monkeypatch.setattr(upkeep, "REEMBED_MAX_BATCHES", 1)
        for index in range(2):
            action = _action(f"upkeep-more-{index}", name=f"Blur {index}", description="Gaussian blur")
            models.Action.objects.filter(pk=action.pk).update(embedding=None, embedding_model="")

        assert _post("reembed").json() == {"embedded": 1, "more": True}
        assert _post("reembed").json() == {"embedded": 1, "more": False}


class _TaktHealth(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(self.status if self.path == "/rekuest/ht" else 404)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def takt_health(settings):
    """Something answering takt's ``ht`` on a real socket, as healthy as ``status`` says."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TaktHealth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.TAKT_URL = f"http://127.0.0.1:{server.server_address[1]}/rekuest"
    yield _TaktHealth
    _TaktHealth.status = 200
    server.shutdown()
    server.server_close()


@pytest.mark.django_db
class TestHealthAnswersForTakt:
    def test_healthy_with_takt_healthy(self, takt_health):
        assert HttpClient().get(reverse("health_check")).status_code == 200

    def test_unhealthy_when_takt_says_so(self, takt_health):
        takt_health.status = 503
        assert HttpClient().get(reverse("health_check")).status_code == 500

    def test_unhealthy_when_takt_is_gone(self, settings):
        settings.TAKT_URL = "http://127.0.0.1:9/rekuest"
        assert HttpClient().get(reverse("health_check")).status_code == 500

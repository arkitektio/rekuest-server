"""The service catalog: what each configured service hosts and emits, read from its manifest.

Nothing here is about agents: a service has none, and cataloguing one creates none.
"""

import pytest

from facade import models, provisioning, service_catalog
from rekuest.configuration import ServiceEntry
from tests.hook_urls import housekeeping
from tests.provisioning_fixtures import declared, hub  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures("takt")


@pytest.mark.django_db(transaction=True)
class TestServiceCatalog:
    def test_a_service_is_catalogued_with_what_it_hosts_and_emits(self, hub):
        assert service_catalog.catalogue_all() == []

        service = models.Service.objects.get(name="housekeeping")
        assert (service.identifier, service.description) == ("live.arkitekt.housekeeping", "A test service.")
        (hosted,) = service.structures.all()
        assert (hosted.identifier, hosted.label, hosted.description) == ("@housekeeping/room", "Room", "A room to tidy.")
        assert list(hosted.descriptors.values_list("key", "type", "description")) == [("@housekeeping/area", "FLOAT", "Square metres")]
        signals = {(d.identifier, d.kind, tuple(d.descriptor_keys)) for d in service.signals.all()}
        assert signals == {("@housekeeping/room", "CREATED", ("@housekeeping/area",)), ("@housekeeping/room", "DELETED", ("@housekeeping/area",))}

    def test_cataloguing_a_service_creates_no_agent(self, hub):
        hub.HOOK_AGENTS = []
        assert provisioning.provision_all() == provisioning.Failed(services=[], hook_agents=[])
        assert models.Service.objects.filter(name="housekeeping").exists()
        assert not models.Agent.objects.exists()

    def test_hosting_and_announcing_are_catalogued_separately(self, hub):
        housekeeping._signals.clear()  # hosted, never announced
        service_catalog.catalogue_all()
        assert models.StructureDeclaration.objects.filter(identifier="@housekeeping/room").exists()
        assert models.SignalDeclaration.objects.count() == 0

    def test_the_catalog_is_exactly_the_manifest(self, hub):
        service_catalog.catalogue_all()
        housekeeping._signals.clear()
        housekeeping.signal("@housekeeping/room", kinds=["CREATED"])
        housekeeping._structures.clear()

        service_catalog.catalogue_all()
        assert list(models.SignalDeclaration.objects.values_list("kind", flat=True)) == ["CREATED"]
        assert models.StructureDeclaration.objects.count() == 0

    def test_a_service_that_cannot_say_what_it_hosts_keeps_what_it_hosted(self, hub, monkeypatch):
        service_catalog.catalogue_all()
        current = housekeeping.manifest()
        # A manifest without the key at all (a rolled back service): unknown, not nothing.
        monkeypatch.setattr(housekeeping, "manifest", lambda: {key: value for key, value in current.items() if key != "structures"})

        assert service_catalog.catalogue_all() == []
        assert models.StructureDeclaration.objects.filter(identifier="@housekeeping/room").exists()

    def test_a_structure_stays_with_the_service_that_hosted_it_first(self, hub):
        service_catalog.catalogue_all()
        claimant = models.Service.objects.create(name="claimant")

        service_catalog._sync_structures(claimant, [service_catalog.StructureManifest(identifier="@housekeeping/room", label="Stolen"), service_catalog.StructureManifest(identifier="@claimant/thing")])

        assert models.StructureDeclaration.objects.get(identifier="@housekeeping/room").service.name == "housekeeping"
        assert models.StructureDeclaration.objects.get(identifier="@claimant/thing").service == claimant

    def test_an_unreachable_service_is_reported_and_the_others_still_catalogued(self, hub):
        hub.SERVICES = [ServiceEntry(name="offline", url="http://127.0.0.1:9/_rekuest/service"), *hub.SERVICES]

        assert service_catalog.catalogue_all() == ["offline"]
        assert models.Service.objects.filter(name="housekeeping").exists()

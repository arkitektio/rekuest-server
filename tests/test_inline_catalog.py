"""The service catalog, written from what the config says a service hosts (``rekuest.services[].hosts``).

Nothing here asks a service or takt: an inline declaration needs the database and nothing else.
"""

import pytest

from facade import models, service_catalog
from rekuest.configuration import ServiceEntry
from tests.hook_urls import housekeeping
from tests.provisioning_fixtures import declared, hub  # noqa: F401  (fixtures)


def _rows() -> dict:
    """Every catalog row a pass writes, in the order it is read back."""
    return {
        "services": list(models.Service.objects.order_by("name").values_list("name", "identifier")),
        "structures": list(models.StructureDeclaration.objects.order_by("identifier").values_list("service__name", "identifier", "label", "description")),
        "descriptors": list(models.Descriptor.objects.order_by("structure__identifier", "position").values_list("structure__identifier", "key", "type", "description", "position")),
        "signals": list(models.SignalDeclaration.objects.order_by("identifier", "kind").values_list("service__name", "identifier", "kind", "descriptor_keys", "description")),
    }


@pytest.mark.django_db(transaction=True)
class TestInlineDeclaration:
    """An entry that carries what its service hosts (``hosts``) is catalogued from that, unasked."""

    def test_the_declaration_in_the_config_gives_the_rows_the_manifest_gives(self, hub):
        """One declaration, both ways in: fetched from the service, and handed over inline."""
        said = housekeeping.manifest()
        (fetched,) = hub.SERVICES
        assert service_catalog.catalogue_all() == []
        from_manifest = _rows()
        assert from_manifest["structures"] and from_manifest["descriptors"] and len(from_manifest["signals"]) == 2

        for model in (models.SignalDeclaration, models.StructureDeclaration, models.Service):
            model.objects.all().delete()
        # Nowhere to ask: the rows can only come from the entry.
        hub.SERVICES = [ServiceEntry(name=fetched.name, url="http://127.0.0.1:9/_rekuest/service", hosts={"structures": said["structures"], "signals": said["signals"]})]
        assert service_catalog.catalogue_all() == []

        assert _rows() == from_manifest

    def test_a_description_is_only_a_manifests_to_state(self, hub):
        """An image's ``hosts`` describes no service: the inline way leaves the row's description alone."""
        said = housekeeping.manifest()
        (fetched,) = hub.SERVICES
        service_catalog.catalogue_all()
        assert models.Service.objects.get(name="housekeeping").description == "A test service."

        hub.SERVICES = [ServiceEntry(name=fetched.name, url=fetched.url, hosts={"structures": said["structures"], "signals": said["signals"]})]
        service_catalog.catalogue_all()
        assert models.Service.objects.get(name="housekeeping").description == "A test service."

        models.Service.objects.all().delete()
        service_catalog.catalogue_all()
        assert models.Service.objects.get(name="housekeeping").description is None

    def test_an_inline_declaration_is_all_there_is(self, hub):
        """Unlike a manifest that lists no structures (unknown), an inline declaration without any takes them away."""
        (fetched,) = hub.SERVICES
        service_catalog.catalogue_all()
        assert models.StructureDeclaration.objects.filter(identifier="@housekeeping/room").exists()

        hub.SERVICES = [ServiceEntry(name=fetched.name, url=fetched.url, hosts={})]
        assert service_catalog.catalogue_all() == []
        assert not models.StructureDeclaration.objects.exists() and not models.SignalDeclaration.objects.exists()

    def test_the_catalogue_job_writes_what_the_config_says_and_does_not_fail_on_a_service_that_is_not_up(self, hub, capsys):
        from django.core.management import call_command

        said = housekeeping.manifest()
        hub.SERVICES = [
            ServiceEntry(name="housekeeping", url="http://127.0.0.1:9/_rekuest/service", hosts={"structures": said["structures"], "signals": said["signals"]}),
            ServiceEntry(name="offline", url="http://127.0.0.1:9/_rekuest/service"),
        ]

        call_command("catalogue")

        out = capsys.readouterr().out
        assert "Catalogued housekeeping (from the config)." in out and "Could not reach offline" in out
        assert models.StructureDeclaration.objects.filter(identifier="@housekeeping/room", service__name="housekeeping").exists()
        assert not models.Service.objects.filter(name="offline").exists()

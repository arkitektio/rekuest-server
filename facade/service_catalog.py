"""This hub's services: what each says it hosts and emits, catalogued from its manifest.

A service (``rekuest.services``: a name and where its manifest is) says what exists on the hub:
the structures it hosts, with their descriptors, and the signals it emits. Those become catalog
rows (:class:`facade.models.Service` and its declarations), hub-wide and the same for every
organization. Users' triggers are checked against them.

That is all a service is here. It offers no work and no agent comes out of this pass: agents are
something else (:mod:`facade.hook_agents`), configured and provisioned on their own.

The manifest is fetched signed (``GET <url>/manifest``, served by the service's
``rekuest_service`` package). The pass is idempotent and updates rows in place.
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings
from pydantic import BaseModel

from facade import models
from rekuest.configuration import ServiceEntry

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0


class DescriptorManifest(BaseModel):
    key: str
    type: str | None = None
    description: str | None = None


class StructureManifest(BaseModel):
    identifier: str
    label: str | None = None
    description: str | None = None
    descriptors: list[DescriptorManifest] | None = None


class SignalManifest(BaseModel):
    identifier: str
    kinds: list[str] | None = None
    descriptors: list[str] | None = None
    description: str | None = None


class ServiceManifest(BaseModel):
    """What a service says about itself (``rekuest_service``'s ``/manifest``)."""

    identifier: str | None = None
    description: str | None = None
    signals: list[SignalManifest] | None = None
    # A manifest that does not list structures at all comes from a service that cannot say what
    # it hosts: that reads as "unknown" (None), not as "nothing" — rolling a service back must
    # not empty the catalog. Signals it could always say, so a missing list is "none".
    structures: list[StructureManifest] | None = None


def fetch_manifest(entry: ServiceEntry) -> ServiceManifest:
    """A service's manifest: the structures it hosts and the signals it emits. Signed, so the
    service can refuse strangers."""
    from facade import service_trust

    url = entry.url.rstrip("/") + "/manifest"
    response = httpx.get(url, headers={"Authorization": service_trust.sign_to(entry, "GET", url, b"")}, timeout=_TIMEOUT)
    response.raise_for_status()
    return ServiceManifest.model_validate_json(response.content)


def catalogue(entry: ServiceEntry) -> models.Service:
    """Bring one service's catalog rows — itself, its structures, its signals — in line with its manifest."""
    manifest = fetch_manifest(entry)
    service, _ = models.Service.objects.update_or_create(name=entry.name, defaults={"identifier": manifest.identifier, "description": manifest.description})
    _sync_structures(service, manifest.structures)
    _sync_signals(service, manifest.signals or [])
    return service


def _sync_signals(service: models.Service, signals: list[SignalManifest]) -> None:
    """Make the service's SignalDeclarations exactly what its manifest declares (one row per kind)."""
    declared: set[tuple[str, str]] = set()
    for signal in signals:
        for kind in signal.kinds or ["CREATED"]:
            declared.add((signal.identifier, kind))
            models.SignalDeclaration.objects.update_or_create(
                service=service,
                identifier=signal.identifier,
                kind=kind,
                defaults={"descriptor_keys": list(signal.descriptors or []), "description": signal.description},
            )
    for row in models.SignalDeclaration.objects.filter(service=service):
        if (row.identifier, row.kind) not in declared:
            row.delete()


def _sync_structures(service: models.Service, structures: list[StructureManifest] | None) -> None:
    """Make the structures the service hosts exactly what its manifest declares.

    An identifier another service already hosts stays that service's: the claim is skipped with
    a warning, and the rest of the manifest still applies.
    """
    if structures is None:
        return
    declared: set[str] = set()
    for structure in structures:
        holder = models.StructureDeclaration.objects.filter(identifier=structure.identifier).exclude(service=service).select_related("service").first()
        if holder is not None:
            logger.warning("%s declares the structure %s, which %s already hosts; ignored", service.name, structure.identifier, holder.service.name)
            continue
        declared.add(structure.identifier)
        hosted, _ = models.StructureDeclaration.objects.update_or_create(
            identifier=structure.identifier,
            defaults={"service": service, "label": structure.label, "description": structure.description},
        )
        _sync_descriptors(hosted, structure.descriptors or [])
    models.StructureDeclaration.objects.filter(service=service).exclude(identifier__in=declared).delete()


def _sync_descriptors(structure: models.StructureDeclaration, descriptors: list[DescriptorManifest]) -> None:
    """Make the structure's Descriptor rows exactly what its manifest declares, in that order.

    In place: a descriptor that is still declared keeps its row (and its id), so what a client
    holds on to stays valid across provisioning passes.
    """
    declared: set[str] = set()
    for position, descriptor in enumerate(descriptors):
        declared.add(descriptor.key)
        models.Descriptor.objects.update_or_create(
            structure=structure,
            key=descriptor.key,
            defaults={"type": descriptor.type or "ANY", "description": descriptor.description, "position": position},
        )
    models.Descriptor.objects.filter(structure=structure).exclude(key__in=declared).delete()


def catalogue_all() -> list[str]:
    """Catalogue every configured service once; the names of those that could not be."""
    failed: list[str] = []
    services: list[ServiceEntry] = settings.SERVICES
    for entry in services:
        try:
            catalogue(entry)
        except Exception as error:  # one unreachable service must not keep the others uncatalogued
            logger.warning("Could not catalogue the service %r: %s", entry.name, error)
            failed.append(entry.name)
    return failed

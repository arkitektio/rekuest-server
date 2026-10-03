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
from typing import Any

import httpx
from django.conf import settings

from facade import models

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0


def fetch_manifest(entry: dict[str, Any]) -> dict[str, Any]:
    """A service's manifest: the structures it hosts and the signals it emits. Signed, so the
    service can refuse strangers."""
    from facade import service_trust

    url = entry["url"].rstrip("/") + "/manifest"
    response = httpx.get(url, headers={"Authorization": service_trust.sign_to(entry, "GET", url, b"")}, timeout=_TIMEOUT)
    response.raise_for_status()
    manifest = response.json()
    # A manifest that does not list structures at all comes from a service that cannot say what
    # it hosts: that reads as "unknown" (None), not as "nothing" — rolling a service back must
    # not empty the catalog. Signals it could always say, so a missing list is "none".
    structures = manifest.get("structures")
    return {
        "identifier": manifest.get("identifier"),
        "description": manifest.get("description"),
        "signals": list(manifest.get("signals") or []),
        "structures": None if structures is None else list(structures),
    }


def catalogue(entry: dict[str, Any]) -> models.Service:
    """Bring one service's catalog rows — itself, its structures, its signals — in line with its manifest."""
    manifest = fetch_manifest(entry)
    service, _ = models.Service.objects.update_or_create(name=entry["name"], defaults={"identifier": manifest["identifier"], "description": manifest["description"]})
    _sync_structures(service, manifest["structures"])
    _sync_signals(service, manifest["signals"])
    return service


def _sync_signals(service: models.Service, signals: list[dict[str, Any]]) -> None:
    """Make the service's SignalDeclarations exactly what its manifest declares (one row per kind)."""
    declared = set()
    for signal in signals:
        for kind in signal.get("kinds") or ["CREATED"]:
            declared.add((signal["identifier"], kind))
            models.SignalDeclaration.objects.update_or_create(
                service=service,
                identifier=signal["identifier"],
                kind=kind,
                defaults={"descriptor_keys": list(signal.get("descriptors") or []), "description": signal.get("description")},
            )
    for row in models.SignalDeclaration.objects.filter(service=service):
        if (row.identifier, row.kind) not in declared:
            row.delete()


def _sync_structures(service: models.Service, structures: list[dict[str, Any]] | None) -> None:
    """Make the structures the service hosts exactly what its manifest declares.

    An identifier another service already hosts stays that service's: the claim is skipped with
    a warning, and the rest of the manifest still applies.
    """
    if structures is None:
        return
    declared = set()
    for structure in structures:
        identifier = structure["identifier"]
        holder = models.StructureDeclaration.objects.filter(identifier=identifier).exclude(service=service).select_related("service").first()
        if holder is not None:
            logger.warning("%s declares the structure %s, which %s already hosts; ignored", service.name, identifier, holder.service.name)
            continue
        declared.add(identifier)
        models.StructureDeclaration.objects.update_or_create(
            identifier=identifier,
            defaults={"service": service, "label": structure.get("label"), "description": structure.get("description"), "descriptors": list(structure.get("descriptors") or [])},
        )
    models.StructureDeclaration.objects.filter(service=service).exclude(identifier__in=declared).delete()


def catalogue_all() -> list[str]:
    """Catalogue every configured service once; the names of those that could not be."""
    failed = []
    for entry in getattr(settings, "SERVICES", None) or []:
        try:
            catalogue(entry)
        except Exception as error:  # one unreachable service must not keep the others uncatalogued
            logger.warning("Could not catalogue the service %r: %s", entry.get("name"), error)
            failed.append(str(entry.get("name")))
    return failed

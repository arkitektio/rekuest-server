"""Virtual structure/interface/package types: what ports reference, and what services host.

A "structure" is a distinct ``@package/key`` identifier. It is known here for either of two
reasons: some action's port references it (enumerated from the indexed ``identifier`` column of
the relational ArgPort/ReturnPort rows, scoped to the requesting organization — registration
writes nothing, so that half can never drift from what ports actually reference), or a service
of this hub declares that it hosts it (``StructureDeclaration``, hub-wide, from the service's
manifest). The types here are plain strawberry types (no DB id — the identifier IS the
identity); a hosted structure also says who hosts it and which descriptors its objects carry.

Usage lookups ("which actions consume @mikro/image?") are likewise answered from the port
rows. ``modifiers`` (container nesting like ``["list"]``) are reconstructed from the
materialized ``key_path``: every dot-prefix of a row's path is an ancestor row, so one
extra query fetches all ancestors for all usages.
"""

from __future__ import annotations

from typing import Optional

import strawberry
import strawberry_django
from asgiref.sync import sync_to_async
from strawberry.types import Info

from facade import filters, models


@strawberry.type(description="A usage of a structure or interface by an action's port, derived from the relational port rows.")
class PortUsage:
    action: Action
    port_key: str = strawberry.field(description="The key of the root port this usage sits under.")
    index: int = strawberry.field(description="The index of the root port this usage sits under.")
    key_path: str = strawberry.field(description="The full dot-notation path of the using port, e.g. 'masks.mask'.")
    modifiers: list[str] = strawberry.field(description="Container nesting between the root port and the using port, e.g. ['dict', 'list'].")


def _port_usages(info: Info, identifier: str, kind: str, port_model: type[models.ArgPort] | type[models.ReturnPort]) -> list[PortUsage]:
    """All usages of ``identifier`` (case-insensitive) among ports of ``kind`` in one table, scoped to the requesting org."""
    rows = list(port_model.objects.filter(identifier__iexact=identifier, kind=kind, action__organization=info.context.request.organization).select_related("action"))
    if not rows:
        return []

    action_ids = {row.action_id for row in rows}
    ancestor_paths = {".".join(row.key_path.split(".")[:depth]) for row in rows for depth in range(1, len(row.key_path.split(".")))}
    ancestors = {(port.action_id, port.key_path): port for port in port_model.objects.filter(action_id__in=action_ids, key_path__in=ancestor_paths)} if ancestor_paths else {}

    usages = []
    for row in rows:
        parts = row.key_path.split(".")
        chain = [ancestors.get((row.action_id, ".".join(parts[:depth]))) for depth in range(1, len(parts))]
        root = chain[0] if chain and chain[0] is not None else row
        usages.append(
            PortUsage(
                action=row.action,
                port_key=root.key,
                index=root.index,
                key_path=row.key_path,
                modifiers=[ancestor.kind.lower() for ancestor in chain if ancestor is not None and ancestor.kind in ("DICT", "LIST")],
            )
        )
    return usages


def _distinct_identifiers(info: Info, kind: str, search: str | None = None, package_key: str | None = None) -> list[str]:
    """Distinct (lowercased) '@package/key' identifiers of ``kind`` referenced by the org's ports."""
    identifiers: set[str] = set()
    for port_model in (models.ArgPort, models.ReturnPort):
        queryset = port_model.objects.filter(kind=kind, identifier__isnull=False, action__organization=info.context.request.organization)
        if search:
            queryset = queryset.filter(identifier__icontains=search)
        if package_key:
            queryset = queryset.filter(identifier__istartswith=f"@{package_key}/")
        identifiers.update(queryset.values_list("identifier", flat=True).distinct())
    if kind == "STRUCTURE":
        # What a service hosts is a structure of this hub, whether or not a port uses it yet.
        declared = models.StructureDeclaration.objects.all()
        if search:
            declared = declared.filter(identifier__icontains=search)
        if package_key:
            declared = declared.filter(identifier__istartswith=f"@{package_key}/")
        identifiers.update(declared.values_list("identifier", flat=True))
    # Identifiers without a package part ('@pkg/key') were never catalogued; keep that rule.
    return sorted({identifier.lower() for identifier in identifiers if "/" in identifier})


def _structures(identifiers: list[str]) -> list["Structure"]:
    """Structures for ``identifiers``, each with its declaration when a service hosts it (one query)."""
    rows = models.StructureDeclaration.objects.filter(identifier__in=identifiers).select_related("service").prefetch_related("descriptors")
    declared = {row.identifier.lower(): row for row in rows}
    return [Structure(identifier=identifier, declaration=declared.get(identifier.lower())) for identifier in identifiers]


def _find_structures(info: Info, search: str | None = None, package_key: str | None = None) -> list["Structure"]:
    return _structures(_distinct_identifiers(info, "STRUCTURE", search=search, package_key=package_key))


def _package_of(identifier: str) -> str:
    return identifier.split("/")[0].removeprefix("@")


def _key_of(identifier: str) -> str:
    return identifier.split("/")[-1]


@strawberry.type(description="A package of structures/interfaces, derived from the '@package/' prefix of port identifiers.")
class StructurePackage:
    key: strawberry.ID = strawberry.field(description="The package key (the part between '@' and '/').")

    @strawberry.field(description="Structures of this package: those the org's ports reference and those a service hosts.")
    async def structures(self, info: Info) -> list["Structure"]:
        return await sync_to_async(_find_structures)(info, package_key=self.key)

    @strawberry.field(description="The service of this hub that hosts this package's structures, if one declares any.")
    async def service(self) -> Optional["Service"]:
        hosted = models.StructureDeclaration.objects.filter(identifier__istartswith=f"@{self.key}/").select_related("service").order_by("identifier")
        row = await hosted.afirst()
        return row.service if row is not None else None

    @strawberry.field(description="Interfaces of this package referenced by the org's ports.")
    async def interfaces(self, info: Info) -> list["Interface"]:
        identifiers = await sync_to_async(_distinct_identifiers)(info, "INTERFACE", package_key=self.key)
        return [Interface(identifier=identifier) for identifier in identifiers]


@strawberry.type(description="An interface referenced by an action's port, derived from the relational port rows.")
class Interface:
    identifier: strawberry.ID = strawberry.field(description="The full identifier, e.g. '@rekuest/taskevent'.")

    @strawberry.field(description="The local key (the part after '/').")
    def key(self) -> str:
        return _key_of(self.identifier)

    @strawberry.field(description="The package this interface belongs to.")
    def package(self) -> StructurePackage:
        return StructurePackage(key=_package_of(self.identifier))

    @strawberry.field(description="Usages of this interface as an input in actions (derived from the relational arg ports).")
    async def input_usages(self, info: Info) -> list[PortUsage]:
        return await sync_to_async(_port_usages)(info, self.identifier, "INTERFACE", models.ArgPort)

    @strawberry.field(description="Usages of this interface as an output in actions (derived from the relational return ports).")
    async def output_usages(self, info: Info) -> list[PortUsage]:
        return await sync_to_async(_port_usages)(info, self.identifier, "INTERFACE", models.ReturnPort)


@strawberry_django.type(models.Service, description="A service of this hub (mikro, kabinet, …): the structures it hosts and the signals it emits, the same for every organization. Not an agent, and it has none: a service says what exists, agents say what can be done.")
class Service:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the service.")
    name: str = strawberry_django.field(description="The name the service is known by on this hub.")
    identifier: str | None = strawberry_django.field(description="The identity the service signs as, e.g. live.arkitekt.mikro.")
    description: str | None = strawberry_django.field(description="What the service says it is.")
    signals: list["SignalDeclaration"] = strawberry_django.field(description="The signals it declares it emits.")

    @strawberry_django.field(description="The structures it hosts.")
    def structures(self) -> list["Structure"]:
        return [Structure(identifier=row.identifier, declaration=row) for row in self.structures.select_related("service").prefetch_related("descriptors").order_by("identifier")]


@strawberry_django.type(
    models.Descriptor,
    filters=filters.StructureDescriptorFilter,
    ordering=filters.StructureDescriptorOrder,
    pagination=True,
    description="A descriptor of a hosted structure's objects: a key action ports can require or provide, and triggers can test. Hub-wide.",
)
class StructureDescriptor:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the descriptor.")
    key: str = strawberry_django.field(description="The descriptor key, e.g. '@mikro/n_channels'.")
    type: str = strawberry_django.field(description="What its value is: INT, FLOAT, STRING, BOOL, LIST, or ANY when the service does not say.")
    description: str | None = strawberry_django.field(description="What the service says the descriptor means.")
    hosted_structure: "HostedStructure" = strawberry_django.field(field_name="structure", description="The hosted structure whose objects carry it.")

    @strawberry_django.field(description="The structure whose objects carry it.", select_related=["structure__service"])
    def structure(self) -> "Structure":
        return Structure(identifier=self.structure.identifier.lower(), declaration=self.structure)

    @strawberry_django.field(description="The service that declares it.", select_related=["structure__service"])
    def service(self) -> "Service":
        return self.structure.service

    @strawberry_django.field(description="Other structures whose objects carry a descriptor of the same key.")
    def shared_with(self) -> list["HostedStructure"]:
        return list(models.StructureDeclaration.objects.filter(descriptors__key=self.key).exclude(pk=self.structure_id).order_by("identifier"))


@strawberry_django.type(
    models.StructureDeclaration,
    filters=filters.HostedStructureFilter,
    ordering=filters.HostedStructureOrder,
    pagination=True,
    description="A structure a service of this hub hosts, as that service declares it: a row, with its descriptors. Hub-wide. (`Structure` is the wider notion: it also covers identifiers only action ports reference.)",
)
class HostedStructure:
    id: strawberry.ID = strawberry_django.field(description="Unique ID of the hosted structure.")
    identifier: str = strawberry_django.field(description="The full identifier, e.g. '@mikro/arraydataset'.")
    label: str | None = strawberry_django.field(description="What the hosting service calls one such object.")
    description: str | None = strawberry_django.field(description="What the hosting service says about the structure.")
    service: "Service" = strawberry_django.field(description="The service that hosts it.")
    descriptors: list[StructureDescriptor] = strawberry_django.field(description="The descriptors of its objects.")

    @strawberry_django.field(description="The local key (the part after '/').")
    def key(self) -> str:
        return _key_of(self.identifier)

    @strawberry_django.field(description="The package it belongs to.")
    def package(self) -> StructurePackage:
        return StructurePackage(key=_package_of(self.identifier))

    @strawberry_django.field(description="The same structure with what your organization's action ports say about it (usages).")
    def structure(self) -> "Structure":
        return Structure(identifier=self.identifier.lower(), declaration=self)

    @strawberry_django.field(description="The signals services declare they emit about it.")
    def signals(self) -> list["SignalDeclaration"]:
        return list(models.SignalDeclaration.objects.filter(identifier__iexact=self.identifier).select_related("service").order_by("kind"))


@strawberry.type(description="A structure (data type): referenced by an action's port, hosted by a service of this hub, or both.")
class Structure:
    identifier: strawberry.ID = strawberry.field(description="The full identifier, e.g. '@mikro/arraydataset'.")
    declaration: strawberry.Private[models.StructureDeclaration | None] = None

    @strawberry.field(description="The service of this hub that hosts the structure; null when none declares it.")
    def service(self) -> Optional["Service"]:
        return self.declaration.service if self.declaration is not None else None

    @strawberry.field(description="What the hosting service calls one such object.")
    def label(self) -> str | None:
        return self.declaration.label if self.declaration is not None else None

    @strawberry.field(description="What the hosting service says about the structure.")
    def description(self) -> str | None:
        return self.declaration.description if self.declaration is not None else None

    @strawberry.field(description="The hosted structure itself, as its service declares it; null when no service of this hub hosts it.")
    def hosted(self) -> Optional["HostedStructure"]:
        return self.declaration

    @strawberry_django.field(description="The descriptors of its objects, as the hosting service declares them. Empty when nobody hosts it.")
    def descriptors(self) -> list[StructureDescriptor]:
        return list(self.declaration.descriptors.all()) if self.declaration is not None else []

    @strawberry.field(description="The signals services declare they emit about this structure.")
    async def signals(self) -> list["SignalDeclaration"]:
        declarations = models.SignalDeclaration.objects.filter(identifier__iexact=self.identifier).select_related("service").order_by("kind")
        return [declaration async for declaration in declarations]

    @strawberry.field(description="The local key (the part after '/').")
    def key(self) -> str:
        return _key_of(self.identifier)

    @strawberry.field(description="The package this structure belongs to.")
    def package(self) -> StructurePackage:
        return StructurePackage(key=_package_of(self.identifier))

    @strawberry.field(description="Usages of this structure as an input in actions (derived from the relational arg ports).")
    async def input_usages(self, info: Info) -> list[PortUsage]:
        return await sync_to_async(_port_usages)(info, self.identifier, "STRUCTURE", models.ArgPort)

    @strawberry.field(description="Usages of this structure as an output in actions (derived from the relational return ports).")
    async def output_usages(self, info: Info) -> list[PortUsage]:
        return await sync_to_async(_port_usages)(info, self.identifier, "STRUCTURE", models.ReturnPort)


# --------------------------------------------------------------------------- #
# Query resolvers (wired in facade/schema.py)
# --------------------------------------------------------------------------- #
async def list_structures(info: Info, search: str | None = None) -> list[Structure]:
    return await sync_to_async(_find_structures)(info, search=search)


async def list_interfaces(info: Info, search: str | None = None) -> list[Interface]:
    identifiers = await sync_to_async(_distinct_identifiers)(info, "INTERFACE", search=search)
    return [Interface(identifier=identifier) for identifier in identifiers]


def _known_packages(info: Info) -> set[str]:
    return {_package_of(identifier) for kind in ("STRUCTURE", "INTERFACE") for identifier in _distinct_identifiers(info, kind)}


async def list_structure_packages(info: Info, search: str | None = None) -> list[StructurePackage]:
    packages = await sync_to_async(_known_packages)(info)
    if search:
        packages = {package for package in packages if search.lower() in package.lower()}
    return [StructurePackage(key=key) for key in sorted(packages)]


async def get_structure(info: Info, identifier: strawberry.ID) -> Structure:
    if str(identifier).lower() not in await sync_to_async(_distinct_identifiers)(info, "STRUCTURE"):
        raise ValueError(f"No action port references the structure {identifier!r}, and no service hosts it")
    (structure,) = await sync_to_async(_structures)([str(identifier).lower()])
    return structure


async def get_interface(info: Info, identifier: strawberry.ID) -> Interface:
    if str(identifier).lower() not in await sync_to_async(_distinct_identifiers)(info, "INTERFACE"):
        raise ValueError(f"No action port references the interface {identifier!r}")
    return Interface(identifier=str(identifier).lower())


async def get_structure_package(info: Info, key: strawberry.ID) -> StructurePackage:
    known = await sync_to_async(_known_packages)(info)
    if str(key) not in known:
        raise ValueError(f"No action port references the package {key!r}")
    return StructurePackage(key=str(key))

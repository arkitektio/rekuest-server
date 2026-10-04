"""The hub's services, and what they say they host: read from their manifests by provisioning.

A service is not an agent and has none. A :class:`Service` says what exists on this hub — the
structures it hosts, the signals it emits — and that is true for every organization. What can be
*done* is an agent's to say, and no row here points at one.
"""

from django.db import models


class Service(models.Model):
    """A service of this hub (mikro, kabinet, …), as its manifest describes it. Hub-wide."""

    name = models.CharField(max_length=1000, unique=True, help_text="The name the service is configured under (rekuest.services[].name)")
    identifier = models.CharField(max_length=1000, null=True, blank=True, help_text="The identity the service signs as, e.g. live.arkitekt.mikro")
    description = models.TextField(null=True, blank=True, help_text="What the service says it is")

    def __str__(self) -> str:
        return self.name


class StructureDeclaration(models.Model):
    """A structure a service says it hosts. The descriptors of its objects are :class:`Descriptor` rows.

    Hub-wide, like :class:`SignalDeclaration`: an identifier names one kind of object on this hub,
    whichever organization an object belongs to. So an identifier has one host — the service
    that declared it first.
    """

    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    descriptors: "models.Manager[Descriptor]"

    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="structures", help_text="The service that hosts it")
    identifier = models.CharField(max_length=1000, unique=True, help_text="The structure identifier, e.g. @mikro/arraydataset")
    label = models.CharField(max_length=1000, null=True, blank=True, help_text="What the service calls one such object")
    description = models.TextField(null=True, blank=True, help_text="What the service says about the structure")

    def __str__(self) -> str:
        return f"{self.service} hosts {self.identifier}"


class Descriptor(models.Model):
    """One descriptor of a hosted structure's objects, as the hosting service declares it.

    A row of its own, so descriptors can be listed and searched across the hub: which keys
    exist, what they mean, which structures carry them. Action ports ``require`` and ``provide``
    these keys, and triggers test them.
    """

    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    structure_id: int

    structure = models.ForeignKey(StructureDeclaration, on_delete=models.CASCADE, related_name="descriptors", help_text="The hosted structure whose objects carry it")
    key = models.CharField(max_length=1000, help_text="The descriptor key, e.g. @mikro/n_channels")
    type = models.CharField(max_length=20, default="ANY", db_default="ANY", help_text="What its value is: INT, FLOAT, STRING, BOOL, LIST, or ANY when the service does not say")
    description = models.TextField(null=True, blank=True, help_text="What the service says the descriptor means")
    position = models.PositiveIntegerField(default=0, db_default=0, help_text="Its place in the service's declaration")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["structure", "key"], name="descriptor_unique_key_per_structure")]
        indexes = [models.Index(fields=["key"], name="descriptor_key_idx")]
        ordering = ["structure_id", "position", "id"]

    def __str__(self) -> str:
        return f"{self.key} of {self.structure.identifier}"

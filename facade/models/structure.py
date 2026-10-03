"""The hub's services, and what they say they host: read from their manifests by provisioning.

A service is not an agent. A :class:`Service` says what exists on this hub — the structures it
hosts, the signals it emits — and that is true for every organization. What can be *done* is an
agent's to say: a service that offers actions has a HookAgent (an ``Agent`` of kind WEBHOOK,
running inside the service), provisioned beside it and found through its client.
"""

from django.db import models


class Service(models.Model):
    """A service of this hub (mikro, kabinet, …), as its manifest describes it. Hub-wide."""

    name = models.CharField(max_length=1000, unique=True, help_text="The name the service is configured under (rekuest.service_agents[].service)")
    identifier = models.CharField(max_length=1000, null=True, blank=True, help_text="The identity the service signs as, e.g. live.arkitekt.mikro")
    description = models.TextField(null=True, blank=True, help_text="What the service says it is")

    def __str__(self) -> str:
        return self.name


class StructureDeclaration(models.Model):
    """A structure a service says it hosts, with the descriptors of its objects.

    Hub-wide, like :class:`SignalDeclaration`: an identifier names one kind of object on this hub,
    whichever organization an object belongs to. So an identifier has one host — the service
    that declared it first.
    """

    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="structures", help_text="The service that hosts it")
    identifier = models.CharField(max_length=1000, unique=True, help_text="The structure identifier, e.g. @mikro/arraydataset")
    label = models.CharField(max_length=1000, null=True, blank=True, help_text="What the service calls one such object")
    description = models.TextField(null=True, blank=True, help_text="What the service says about the structure")
    descriptors = models.JSONField(default=list, blank=True, help_text="The descriptors its objects carry: [{key, type, description}]", db_default=[])

    def __str__(self) -> str:
        return f"{self.service} hosts {self.identifier}"

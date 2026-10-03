"""Wiregrams: an organization's automation as one document it imported.

Nothing on a hub is wired by itself: an agent offers actions, a service announces signals, and
which action runs when is for the organization's users to say. A **wiregram** is how they say a
lot of it at once — a document listing schedules and triggers, each naming its target as an
agent's name and one of its interfaces, so the same document fits any organization that has
those agents.

Importing one creates a ``Wiregram`` row that *owns* the rules it made (``Schedule.wiregram``,
``Trigger.wiregram``, each with its ``wire_key`` from the document). Importing the same key again
brings them in line with the new document in place; deleting the wiregram removes them.
"""

from django.db import models
from django.db.models.functions import Now


class Wiregram(models.Model):
    """One imported automation document, and the owner of the rules it created."""

    organization = models.ForeignKey("authentikate.Organization", on_delete=models.CASCADE, related_name="wiregrams", help_text="The organization that imported it")
    caller = models.ForeignKey("Caller", on_delete=models.CASCADE, related_name="wiregrams", help_text="Who imported it last: the runs of its rules are assigned as this identity")
    key = models.CharField(max_length=200, help_text="What the document calls itself; importing the same key again updates this wiregram")
    name = models.CharField(max_length=200, help_text="A human-readable name")
    description = models.TextField(null=True, blank=True, help_text="What the document says it is for")
    document = models.JSONField(default=dict, blank=True, help_text="The document as it was last imported", db_default={})
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "key"], name="wiregram_unique_key_per_organization")]

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"

from authentikate.models import Organization
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now


class Space(models.Model):
    """A room: where the organization keeps its devices, for every view that places them.

    Which devices a 3D view shows is the action's (its ``<Space room=...>`` blok places the
    run's dependencies); where each one stands is kept here, shared by everyone, so a device
    moved by one viewer stands there for all. A space is found again by its ``reference`` (the
    blok's ``room``, a URI for one room of the organization or an app-scoped key) and is made
    the first time someone keeps a pose in it.
    """

    name = models.CharField(max_length=2000)
    reference = models.CharField(
        max_length=2000,
        null=True,
        blank=True,
        help_text="What finds the room again: a blok's `room` (a URI such as `room:helsinki-lab`, or `app/<identifier>/<key>` for an app's own). One per organization.",
    )
    description = models.TextField(null=True, blank=True)
    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name="spaces",
        help_text="The user that first kept a pose in this space",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="spaces",
        help_text="The organization this Space belongs to",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "reference"],
                condition=Q(reference__isnull=False),
                name="space_reference_unique_per_organization",
            ),
        ]


class Placement(models.Model):
    """Where someone left an agent in a space: its pose, the same for every viewer."""

    space = models.ForeignKey(Space, on_delete=models.CASCADE, related_name="placements")
    agent = models.ForeignKey("Agent", on_delete=models.CASCADE, related_name="placements")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())
    role = models.CharField(max_length=1000, help_text="The dependency key the agent was placed under when its pose was kept (`robot`)")
    affine_matrix = models.JSONField(help_text="Where the agent stands: a 4x4 affine matrix, row-major, in meters with Y up, from the agent's model into the space", null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["space", "agent"], name="placement_one_per_agent_and_space"),
        ]

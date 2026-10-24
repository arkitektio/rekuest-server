from authentikate.models import Organization
from django.db.models.functions import Now
from django.contrib.auth import get_user_model
from django.db import models


class Space(models.Model):
    name = models.CharField(max_length=2000)
    description = models.TextField(null=True, blank=True)
    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name="spaces",
        help_text="The user that created this Space",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="spaces",
        help_text="The organization this Space belongs to",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())


class Placement(models.Model):
    space = models.ForeignKey(Space, on_delete=models.CASCADE, related_name="placements")
    agent = models.ForeignKey("Agent", on_delete=models.CASCADE, related_name="placements")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())
    role = models.CharField(max_length=1000, help_text="The role of this placement ")
    affine_matrix = models.JSONField(help_text="The affine matrix of this placement (e.g. the position and orientation of the agent in the space)", null=True, blank=True)
    blok = models.ForeignKey("MaterializedBlok", on_delete=models.CASCADE, related_name="placements", null=True, blank=True, help_text="An optional Blok to represent this placement in a 3D space and to define its interactions")


class SpacePlacement(models.Model):
    """A 3D blok placed in a space at an affine transform, with no agent of its own.

    The space's twin of ``DashboardPlacement``: what a scene shows besides its agents -- a
    ``<Model>`` or ``<Robot>`` blok over a kulisse asset (a stage, a fixture, a room), placed by
    its 4x4 transform. An agent's own presence in a space is a ``Placement``.
    """

    space = models.ForeignKey(Space, on_delete=models.CASCADE, related_name="blok_placements")
    blok = models.ForeignKey("MaterializedBlok", on_delete=models.CASCADE, related_name="space_placements", help_text="The 3D blok drawn at this placement")
    name = models.CharField(max_length=1000, null=True, blank=True, help_text="What this placement is called in its space")
    affine_matrix = models.JSONField(null=True, blank=True, help_text="Where the blok sits in the space: a 4x4 affine matrix, row-major, from the blok's frame into the space's. Null is the identity")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())

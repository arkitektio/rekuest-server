from authentikate.models import Organization
from django.db.models.functions import Now
from django.contrib.auth import get_user_model
from django.db import models
from datalayer.models import MediaStore


class ThreeDModel(models.Model):
    organization = models.ForeignKey(
        "authentikate.Organization",
        on_delete=models.CASCADE,
        related_name="threed_models",
        help_text="The organization this 3D model belongs to. Access is scoped to it.",
    )
    name = models.CharField(max_length=1000)
    description = models.TextField(null=True, blank=True)
    file = models.ForeignKey(
        MediaStore,
        on_delete=models.CASCADE,
        related_name="threedmodels",
        help_text="The media file containing this 3D model",
    )
    transfer_function = models.CharField(max_length=10000, help_text="The function used to transfer the state of the model to properties of the model")
    dependency = models.JSONField(help_text="The protocol of the agent that we need to show this model", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())


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
    model = models.ForeignKey(ThreeDModel, on_delete=models.CASCADE, related_name="placements", null=True, blank=True, help_text="An optional 3D model to represent this placement in a 3D space")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())
    role = models.CharField(max_length=1000, help_text="The role of this placement ")
    affine_matrix = models.JSONField(help_text="The affine matrix of this placement (e.g. the position and orientation of the agent in the space)", null=True, blank=True)
    blok = models.ForeignKey("MaterializedBlok", on_delete=models.CASCADE, related_name="placements", null=True, blank=True, help_text="An optional Blok to represent this placement in a 3D space and to define its interactions")

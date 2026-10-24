"""Assets: the files apps bring with them, stored once by content and named per agent."""

from django.contrib.auth import get_user_model
from django.db import models
from django.db.models.functions import Now

from arkitekt_datalayer.models import MediaStore


class Asset(models.Model):
    """One verified file. Identical bytes within an organization are one asset, whoever brought them."""

    organization = models.ForeignKey("authentikate.Organization", on_delete=models.CASCADE, related_name="assets", help_text="The organization this asset belongs to. Access is scoped to it.")
    ref = models.CharField(max_length=71, help_text="'sha256:' and the hex SHA-256 of the file's exact bytes.")
    kind = models.CharField(max_length=32, help_text="What the file is (rekuest_core AssetKind).")
    file = models.ForeignKey(MediaStore, on_delete=models.CASCADE, related_name="assets", help_text="The stored file.")
    size = models.BigIntegerField(help_text="The size of the file in bytes, measured by the server.")
    meta = models.JSONField(default=dict, db_default={}, help_text="What validation found: a model's node names and counts, an image's format and size.")
    uploaded_by = models.ForeignKey(get_user_model(), on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_assets", help_text="The user whose app uploaded the file.")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "ref"], name="unique_asset_ref_per_organization")]


class AgentAsset(models.Model):
    """A name an agent gives one of its files: what `@<dependency>.assets.<name>` resolves to.

    Written at registration from the agent's declaration. The file itself is the
    organization's ``Asset`` of the same ``ref``, once it was uploaded: by the agent, or,
    for a file the agent only names a ``source`` for, by an admin.
    """

    agent = models.ForeignKey("Agent", on_delete=models.CASCADE, related_name="assets", help_text="The agent that declares the asset.")
    name = models.CharField(max_length=200, help_text="The name bloks refer to the asset by.")
    kind = models.CharField(max_length=32, help_text="What the agent says the file is (rekuest_core AssetKind).")
    ref = models.CharField(max_length=71, help_text="'sha256:' and the hex SHA-256 of the file's exact bytes, as the agent declares it.")
    size = models.BigIntegerField(null=True, blank=True, help_text="The size of the file in bytes, as the agent declares it. NULL for a file the agent does not hold (it names a source).")
    source = models.CharField(max_length=2000, null=True, blank=True, help_text="The https URL the file can be fetched from. Set when the agent does not hold the file: an admin's client fetches and uploads it.")
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())

    class Meta:
        constraints = [models.UniqueConstraint(fields=["agent", "name"], name="unique_asset_name_per_agent")]

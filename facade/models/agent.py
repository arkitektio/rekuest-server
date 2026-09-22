import uuid

from authentikate.models import App, Client, Organization, Release, User
from django.contrib.auth import get_user_model
from django.db import models
from django_choices_field import TextChoicesField

from facade import enums, liveness


class Lock(models.Model):
    agent = models.ForeignKey(
        "Agent",
        on_delete=models.CASCADE,
        related_name="locks",
        help_text="The agent this lock belongs to",
    )
    key = models.CharField(max_length=2000, help_text="A unique identifier for this lock within the agent")
    description = models.TextField(null=True, blank=True, help_text="A description for the Lock")
    created_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    hold_by = models.ForeignKey(
        "Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="held_locks",
        help_text="The assigniation that currently holds this lock",
    )

    class Meta:
        constraints = [
            # One row per lock key. It is upserted from two directions — the agent's socket
            # (``on_agent_lock``) and its registration, which run on different backends — and
            # ``update_or_create`` cannot dedupe what the database does not constrain. A second
            # row makes every later upsert raise ``MultipleObjectsReturned``, permanently.
            models.UniqueConstraint(fields=["agent", "key"], name="lock_unique_key_per_agent"),
        ]


class Agent(models.Model):
    app = models.ForeignKey(
        App,
        on_delete=models.CASCADE,
        related_name="agents",
        help_text="The app this agent belongs to (agents are part of an app and are NOT associated only with a release)",
    )
    hash = models.CharField(max_length=1000, help_text="The hash of the Agent (comparing the hash can be used to check if the agent has changed in a definition way)")
    release = models.ForeignKey(Release, on_delete=models.CASCADE, related_name="agents", help_text="The release this agent belongs to (agents are part of a release and are NOT associated only with an app)")
    name = models.CharField(max_length=2000, help_text="This providers Name", default="Nana")
    description = models.TextField(null=True, blank=True, help_text="A description for the Agent")
    health_check_interval = models.IntegerField(
        default=60 * 5,
        help_text="How often should this agent be checked for its health. Defaults to 5 mins",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        help_text="The user this Agent belongs to",
    )
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    unique = models.CharField(max_length=1000, default=uuid.uuid4, help_text="The Channel we are listening to")
    active_connection_id = models.CharField(
        max_length=1000,
        null=True,
        blank=True,
        help_text="Identifies the websocket connection currently owning this Agent. Used to reject/displace duplicate connections and to guard disconnect handling against a displaced connection.",
    )
    active_session_id = models.CharField(
        max_length=1000,
        null=True,
        blank=True,
        help_text="The executor process's volatile session id from the last connect. On reconnect, a matching session means the same process survived (reclaim in-flight work); a different session means a fresh process (fail-and-cascade the orphaned work).",
    )
    lease_epoch = models.BigIntegerField(
        default=0,
        help_text=(
            "Monotonic fencing token for the executor write-lease. Bumped on every claim (connect) "
            "and on every revoke (stale sweep). A connection carries the epoch it claimed and renews "
            "its lease with a compare-and-set on it, so a displaced or revoked connection's heartbeat "
            "matches no row and the connection terminates itself. Distinct from active_connection_id "
            "(a socket *name*, unique but not revocable) and active_session_id (the client-supplied "
            "*process* identity, which must stay equal across a reclaiming reconnect)."
        ),
    )
    on_instance = models.CharField(
        max_length=1000,
        help_text="The Instance this Agent is running on",
        default="all",
    )
    kind = models.CharField(
        max_length=1000,
        choices=[(tag, tag.value) for tag in enums.AgentKind],
        default=enums.AgentKind.WEBSOCKET,
        help_text="The kind of this Agent",
    )
    hook_url = models.CharField(max_length=1000, help_text="The webhook URL for this Agent (only if webhook)", null=True, blank=True)
    hook_url_secret = models.CharField(max_length=1000, help_text="The webhook URL secret for this Agent (only if webhook)", null=True, blank=True)
    latest_event = TextChoicesField(
        max_length=1000,
        choices_enum=enums.AgentEventChoices,
        default=enums.AgentEventChoices.DISCONNECT,
        help_text="The Status of this Agent",
    )
    connected = models.BooleanField(default=False, help_text="Is this Agent connected to the backend")
    last_seen = models.DateTimeField(help_text="The last time this Agent was seen", null=True)
    pinned_by = models.ManyToManyField(
        get_user_model(),
        related_name="pinned_agents",
        blank=True,
        help_text="The users that pinned this Agent",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="agents",
        help_text="The client (app instance) this Agent runs as",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        help_text="The organization this Agent belongs to",
    )
    blocked = models.BooleanField(
        default=False,
        help_text="If this Agent is blocked, it will not be used for provision, nor will it be able to provide",
    )

    class Meta:
        permissions = [("can_provide_on", "Can provide on this Agent")]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "user", "organization"],
                name="one_agent_per_client_user_organization",
            )
        ]

    # The executor lease. Owned exclusively by the claim / renew / release / revoke paths in
    # ``facade.persist_backend``, each of which writes them with explicit ``update_fields``.
    LEASE_FIELDS = frozenset({"connected", "last_seen", "lease_epoch", "active_connection_id", "active_session_id"})

    def save(self, *args, **kwargs):
        """A save that names no fields never touches the lease.

        ``agent.save()`` writes EVERY column from whatever snapshot the instance was loaded with.
        With several backends that snapshot is routinely stale: an operator renames or unblocks an
        agent on one process while another claims or revokes its lease, and the late full-row
        save silently restores the old ``lease_epoch`` — un-fencing a connection whose in-flight
        work was already failed — or flips ``connected`` back. So a bare save of an existing row
        is narrowed to everything *except* :attr:`LEASE_FIELDS`. Lease writers are unaffected:
        they always pass ``update_fields``.
        """
        if not self._state.adding and kwargs.get("update_fields") is None and not kwargs.get("force_insert"):
            kwargs["update_fields"] = [f.name for f in self._meta.concrete_fields if not f.primary_key and f.name not in self.LEASE_FIELDS]
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name}"

    @property
    def is_active(self):
        return liveness.agent_is_live(self.connected, self.last_seen)


class MemoryShelve(models.Model):
    """A shelve is a collection of shelved items that are
    related to each other. Shelves are used to group shelved
    items together and provide a way to access them.

    Shelves are not directly accessible by the user, but are
    used by the agent to store and manage shelved items.

    """

    organization = models.ForeignKey(
        "authentikate.Organization",
        on_delete=models.CASCADE,
        related_name="memory_shelves",
        help_text="The organization this MemoryShelve belongs to. Access is scoped to it.",
    )
    agent = models.OneToOneField(
        Agent,
        on_delete=models.CASCADE,
        help_text="The associated agent for this memory shelve",
        related_name="memory_shelve",
    )

    name = models.CharField(max_length=1000)
    description = models.TextField()
    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name="shelves",
        help_text="The user that created this Shelf",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class MemoryDrawer(models.Model):
    """A shelve is a collection of shelved items that are
    related to each other. Shelves are used to group shelved
    items together and provide a way to access them.

    Shelves are not directly accessible by the user, but are
    used by the agent to store and manage shelved items.

    """

    shelve = models.ForeignKey(
        MemoryShelve,
        on_delete=models.CASCADE,
        help_text="The associated shelve for this drawer",
        related_name="drawers",
    )
    resource_id = models.CharField(
        max_length=1000,
        help_text="The resource id of this drawer",
        null=True,
        blank=True,
    )
    identifier = models.CharField(
        max_length=1000,
        help_text="The identifier of this drawer",
    )
    label = models.CharField(max_length=1000, null=True)
    description = models.TextField(null=True)

    class Meta:
        constraints = [
            # ``shelve_in_memory_drawer`` upserts on (shelve, resource_id); without this two
            # concurrent shelvings of one resource create two drawers and every later upsert
            # raises. Partial: ``resource_id`` is nullable and NULLs are not a duplicate.
            models.UniqueConstraint(
                fields=["shelve", "resource_id"],
                condition=models.Q(resource_id__isnull=False),
                name="drawer_unique_resource_per_shelve",
            ),
        ]


class HardwareRecord(models.Model):
    agent = models.ForeignKey(
        Agent,
        on_delete=models.CASCADE,
        help_text="The associated agent for this HardwareRecord",
        related_name="hardware_records",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    cpu_count = models.IntegerField(default=0)
    cpu_vendor_name = models.CharField(max_length=1000, default="Unknown")
    cpu_frequency = models.FloatField(default=0)

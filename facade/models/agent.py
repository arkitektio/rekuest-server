from typing import TYPE_CHECKING

from authentikate.models import App, Client, Organization, Release, User
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models.functions import Now

from facade import enums

if TYPE_CHECKING:
    from facade.models.implementation import Implementation


class Lock(models.Model):
    agent = models.ForeignKey(
        "Agent",
        on_delete=models.CASCADE,
        related_name="locks",
        help_text="The agent this lock belongs to",
    )
    key = models.CharField(max_length=2000, help_text="A unique identifier for this lock within the agent")
    description = models.TextField(null=True, blank=True, help_text="A description for the Lock")
    created_at = models.DateTimeField(auto_created=True, auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())
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
    # Declared for the type checker: Django adds these (a foreign key's id column, a
    # reverse relation's manager) without saying so in a way it can read.
    app_id: int
    client_id: int
    organization_id: int
    release_id: int
    user_id: int
    implementations: "models.Manager[Implementation]"
    app = models.ForeignKey(
        App,
        on_delete=models.CASCADE,
        related_name="agents",
        help_text="The app this agent belongs to (agents are part of an app and are NOT associated only with a release)",
    )
    hash = models.CharField(max_length=1000, help_text="The hash of the Agent (comparing the hash can be used to check if the agent has changed in a definition way)")
    release = models.ForeignKey(Release, on_delete=models.CASCADE, related_name="agents", help_text="The release this agent belongs to (agents are part of a release and are NOT associated only with an app)")
    name = models.CharField(max_length=2000, help_text="The name the agent declares for itself, written at every registration.", default="Nana", db_default="Nana")
    display_name = models.CharField(max_length=2000, null=True, blank=True, help_text="The name a user gave this agent. It wins over the declared name, and registration never touches it.")
    description = models.TextField(null=True, blank=True, help_text="A description for the Agent")
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        help_text="The user this Agent belongs to",
    )
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True, db_default=Now())
    active_connection_id = models.CharField(
        max_length=1000,
        null=True,
        blank=True,
        help_text="Identifies the websocket connection currently owning this Agent, and is the fencing token of the executor write-lease: written on every claim (connect), cleared on every revoke (stale sweep). A connection renews its lease with a compare-and-set on it, so a displaced or revoked connection's heartbeat matches no row and the connection terminates itself.",
    )
    active_session_id = models.CharField(
        max_length=1000,
        null=True,
        blank=True,
        help_text="The executor process's volatile session id from the last connect. On reconnect, a matching session means the same process survived (reclaim in-flight work); a different session means a fresh process (fail-and-cascade the orphaned work).",
    )
    kind = models.CharField(
        max_length=1000,
        choices=[(tag, tag.value) for tag in enums.AgentKind],
        default=enums.AgentKind.WEBSOCKET,
        help_text="The kind of this Agent",
        db_default="WEBSOCKET",
    )
    hook_url = models.CharField(max_length=1000, help_text="The webhook URL for this Agent (only if webhook)", null=True, blank=True)
    hook_url_secret = models.CharField(max_length=1000, help_text="The webhook URL secret for this Agent (only if webhook)", null=True, blank=True)
    connected = models.BooleanField(default=False, help_text="Is this Agent connected to the backend", db_default=False)
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
        db_default=False,
    )

    class Meta:
        permissions = [("can_provide_on", "Can provide on this Agent")]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "user", "organization"],
                name="one_agent_per_client_user_organization",
            )
        ]

    # The executor lease. takt's (``takt/crates/facade/src/persist/leases.rs``): its claim,
    # renew, release and revoke are the only writers.
    LEASE_FIELDS = frozenset({"connected", "last_seen", "active_connection_id", "active_session_id"})

    def save(self, *args, **kwargs) -> None:
        """A save that names no fields never touches the lease.

        ``agent.save()`` writes EVERY column from whatever snapshot the instance was loaded with.
        With several backends that snapshot is routinely stale: an operator renames or unblocks an
        agent on one process while another claims or revokes its lease, and the late full-row
        save silently restores the old ``active_connection_id`` — un-fencing a connection whose in-flight
        work was already failed — or flips ``connected`` back. So a bare save of an existing row
        is narrowed to everything *except* :attr:`LEASE_FIELDS`. Lease writers are unaffected:
        they always pass ``update_fields``.
        """
        if not self._state.adding and kwargs.get("update_fields") is None and not kwargs.get("force_insert"):
            kwargs["update_fields"] = [f.name for f in self._meta.concrete_fields if not f.primary_key and f.name not in self.LEASE_FIELDS]
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name}"


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
    created_at = models.DateTimeField(auto_now_add=True, db_default=Now())
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())


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
    agent_minted = models.BooleanField(
        default=False,
        help_text="The agent minted this drawer's reference (a numbered SHELVE): the agent addresses it by resource_id, and COLLECT names it by resource_id. False for drawers of older agents, which reference the pk.",
        db_default=False,
    )

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

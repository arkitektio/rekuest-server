from turtle import forward
from typing import List, Tuple
from hare.carrots import (
    HareMessage,
    ProvideHareMessage,
    ReserveHareMessage,
    UnassignHareMessage,
    KickHareMessage,
    UnprovideHareMessage,
    BounceHareMessage,
    UnreserveHareMessage,
    ReservationChangedMessage,
)
from hare.messages import ReserveParams, BindParams
from hare.connection import pikaconnection
from lok.models import LokApp, LokClient
from facade.managers import NodeManager, ReservationManager
from facade.fields import (
    ArgsField,
    KwargsField,
    ParamsField,
    ReturnField,
)
from facade.enums import (
    AccessStrategy,
    AgentStatus,
    LogLevel,
    AssignationStatus,
    LogLevel,
    ProvisionMode,
    NodeKind,
    ProvisionStatus,
    ReservationStatus,
    RepositoryType,
    WaiterStatus,
)
from django.db import models
from django.contrib.auth import get_user_model
import uuid
import logging
from guardian.shortcuts import get_objects_for_user
from django.db.models import Q

logger = logging.getLogger(__name__)


class Repository(models.Model):
    """A Repository is the housing conatinaer for a Node, Multiple Nodes belong to one repository."""

    type = models.CharField(
        choices=RepositoryType.choices, default=RepositoryType.APP, max_length=4000
    )
    name = models.CharField(max_length=1000, help_text="The name of this Repository")
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    unique = models.CharField(
        max_length=1000, default=uuid.uuid4, help_text="A world-unique identifier"
    )

    def __str__(self):
        return f"{self.name}"


class Registry(models.Model):
    client = models.ForeignKey(LokClient, on_delete=models.CASCADE)
    app = models.ForeignKey(
        LokApp, on_delete=models.CASCADE, null=True, help_text="The Associated App"
    )
    user = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        null=True,
        help_text="The Associatsed App",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "user"],
                name="No multiple Clients for same App and User allowed",
            )
        ]

    def __str__(self) -> str:
        return f"{self.app} used by {self.user}"


class Protocol(models.Model):
    name = models.CharField(
        max_length=1000, unique=True, help_text="The name of this Protocol"
    )
    description = models.TextField(help_text="A description for the Protocol")

    def __str__(self) -> str:
        return self.name


class Structure(models.Model):
    """A Structure is a uniquely identifiable model for a Repository"""

    extenders = models.JSONField(
        help_text="Registered Extenders on this Model", null=True
    )
    identifier = models.CharField(
        max_length=1000,
        help_text="A unique identifier for this Model accross the Platform",
        unique=True,
    )

    def __str__(self):
        return f"{self.identifier}"


class MirrorRepository(Repository):
    url = models.URLField(null=True, blank=True, unique=True, default="None")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} at {self.url}"


class AppRepository(Repository):
    app = models.ForeignKey(
        LokApp, on_delete=models.CASCADE, null=True, help_text="The Associated App"
    )


class Agent(models.Model):
    name = models.CharField(
        max_length=2000, help_text="This providers Name", default="Nana"
    )
    instance_id = models.CharField(default="main", max_length=1000)
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    unique = models.CharField(
        max_length=1000, default=uuid.uuid4, help_text="The Channel we are listening to"
    )
    on_instance = models.CharField(
        max_length=1000,
        help_text="The Instance this Agent is running on",
        default="all",
    )
    status = models.CharField(
        max_length=1000,
        choices=AgentStatus.choices,
        default=AgentStatus.VANILLA,
        help_text="The Status of this Agent",
    )
    registry = models.ForeignKey(
        Registry,
        on_delete=models.CASCADE,
        help_text="The provide might be limited to a instance like ImageJ belonging to a specific person. Is nullable for backend users",
        null=True,
        related_name="agents",
    )
    blocked = models.BooleanField(
        default=False,
        help_text="If this Agent is blocked, it will not be used for provision, nor will it be able to provide",
    )

    class Meta:
        permissions = [("can_provide_on", "Can provide on this Agent")]
        constraints = [
            models.UniqueConstraint(
                fields=["registry", "instance_id"],
                name="No multiple Agents for same App and User allowed on same identifier",
            )
        ]

    def __str__(self):
        return f"{self.status} {self.registry} on {self.instance_id} managed by {self.on_instance}"

    def kick(self):
        """Kick the Agent to reinitialize the connection"""
        forwards = []

        print("Kicking agent", self)
        forwards.append(KickHareMessage(queue=self.queue))

        for forward_res in forwards:
            pikaconnection.publish(forward_res.queue, forward_res.to_message())

        self.status = AgentStatus.KICKED
        self.save()

    def bounce(self):
        """Kick the Agent to reinitialize the connection"""
        forwards = []

        print("Bouncing agents", self)
        forwards.append(BounceHareMessage(queue=self.queue))

        for forward_res in forwards:
            pikaconnection.publish(forward_res.queue, forward_res.to_message())

    @property
    def queue(self):
        return f"agent_{self.unique}"


class Collection(models.Model):
    name = models.CharField(
        max_length=1000, unique=True, help_text="The name of this Collection"
    )
    description = models.TextField(help_text="A description for the Collection")
    defined_at = models.DateTimeField(auto_created=True, auto_now_add=True)


class Waiter(models.Model):
    name = models.CharField(
        max_length=2000, help_text="This waiters Name", default="Nana"
    )
    identifier = models.CharField(default="main", max_length=1000)
    installed_at = models.DateTimeField(auto_created=True, auto_now_add=True)
    unique = models.CharField(
        max_length=1000, default=uuid.uuid4, help_text="The Channel we are listening to"
    )
    status = models.CharField(
        max_length=1000,
        choices=WaiterStatus.choices,
        default=WaiterStatus.VANILLA,
        help_text="The Status of this Waiter",
    )
    registry = models.ForeignKey(
        Registry,
        on_delete=models.CASCADE,
        help_text="The provide might be limited to a instance like ImageJ belonging to a specific person. Is nullable for backend users",
        null=True,
        related_name="waiters",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["registry", "identifier"],
                name="No multiple Waiters for same App and User allowed on same identifier",
            )
        ]

    def __str__(self):
        return f"Waiter {self.registry} on {self.identifier}"

    @property
    def queue(self):
        return f"waiter_{self.unique}"


class Node(models.Model):
    """Nodes are abstraction of RPC Tasks. They provide a common API to deal with creating tasks.

    See online Documentation"""

    collections = models.ManyToManyField(
        Collection,
        related_name="nodes",
        help_text="The collections this Node belongs to",
    )
    pure = models.BooleanField(
        default=False, help_text="Is this function pure. e.g can we cache the result?"
    )
    idempotent = models.BooleanField(
        default=False, help_text="Is this function pure. e.g can we cache the result?"
    )
    kind = models.CharField(
        max_length=1000,
        choices=NodeKind.choices,
        default=NodeKind.FUNCTION,
        help_text="Function, generator? Check async Programming Textbook",
    )
    interfaces = models.JSONField(
        default=list, help_text="Intercae that we use to interpret the meta data"
    )
    port_groups = models.JSONField(
        default=list, help_text="Intercae that we use to interpret the meta data"
    )
    name = models.CharField(
        max_length=1000, help_text="The cleartext name of this Node"
    )
    meta = models.JSONField(
        null=True, blank=True, help_text="Meta data about this Node"
    )

    protocols = models.ManyToManyField(
        Protocol,
        related_name="nodes",
        blank=True,
        help_text="The protocols this Node implements (e.g. Predicate)",
    )

    description = models.TextField(help_text="A description for the Node")
    image = models.ImageField(
        null=True, blank=True, help_text="Beautiful images for beautiful Nodes"
    )
    scope = models.CharField(
        max_length=1000,
        default="GLOBAL",
        help_text="The scope of this Node. e.g. does the data it needs or produce live only in the scope of this Node or is it global or does it bridge data?",
    )
    is_test_for = models.ManyToManyField(
        "self",
        related_name="tests",
        blank=True,
        symmetrical=False,
        help_text="The users that have pinned the position",
    )

    hash = models.CharField(
        max_length=1000,
        help_text="The hash of the Node (completely unique)",
        unique=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    args = ArgsField(default=list, help_text="Inputs for this Node")
    returns = ReturnField(default=list, help_text="Outputs for this Node")

    objects = NodeManager()

    def __str__(self):
        return f"{self.name}"


class Template(models.Model):
    """A Template is a conceptual implementation of A Node. It represents its implementation as well as its performance"""

    interface = models.CharField(
        max_length=1000, help_text="Interface (think Function)"
    )
    node = models.ForeignKey(
        Node,
        on_delete=models.CASCADE,
        help_text="The node this template is implementatig",
        related_name="templates",
    )
    agent = models.ForeignKey(
        Agent,
        on_delete=models.CASCADE,
        help_text="The associated registry for this Template",
        related_name="templates",
    )
    name = models.CharField(
        max_length=1000,
        default="Unnamed",
        help_text="A name for this Template",
    )
    extensions = models.JSONField(
        max_length=2000,
        default=list,
        help_text="The attached extensions for this Template",
    )

    policy = models.JSONField(
        max_length=2000, default=dict, help_text="The attached policy for this template"
    )

    params = ParamsField(default=dict, help_text="Params for this Template")

    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        null=True,
        help_text="Who created this template on this instance",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [("providable", "Can provide this template")]
        constraints = [
            models.UniqueConstraint(
                fields=["interface", "agent"],
                name="A template has unique versions for every node it trys to implement",
            )
        ]

    def __str__(self):
        return f"{self.node} implemented by {self.agent} on {self.interface}"


class ProvisionLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    provision = models.ForeignKey(
        "Provision",
        help_text="The provision this log item belongs to",
        related_name="log",
        on_delete=models.CASCADE,
    )
    message = models.CharField(max_length=20000, null=True, blank=True)
    level = models.CharField(
        choices=LogLevel.choices, default=LogLevel.INFO.value, max_length=200
    )


class Provision(models.Model):
    """Topic (STATEFUL MODEL)

    Topic represents the current state of active Topics that are caused by provisions, they store the reservations (links)
    and are indexed by provision, a consumer connects through its provision to Arkitekt and sets the Topic to active, every
    reservation that is connected gets signalled that this Topic is now active. On disconnect every reservation can design
    according to its policy if it wants to wait for reconnect (if connection was Lost), raise an error, or choose another Topic.

    """

    unique = models.UUIDField(
        max_length=1000,
        unique=True,
        default=uuid.uuid4,
        help_text="A Unique identifier for this Topic",
    )

    # Deploymode
    mode = models.CharField(
        max_length=100,
        default=ProvisionMode.PRODUCTION,
        choices=ProvisionMode.choices,
        help_text="The Deployment Mode for this Provisions",
    )

    # Identifiers
    reference = models.CharField(
        max_length=1000,
        unique=True,
        default=uuid.uuid4,
        help_text="The Unique identifier of this Provision",
    )

    reservation = models.ForeignKey(
        "Reservation",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Reservation that created this provision (if we were auto created)",
        related_name="created_provisions",
    )

    agent = models.ForeignKey(
        Agent,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Is this Provision bound to a certain Agent?",
        related_name="provisions",
    )

    title = models.CharField(
        max_length=200,
        help_text="A Short Hand Way to identify this reservation for you",
        null=True,
        blank=True,
    )
    # Input
    template = models.ForeignKey(
        Template,
        on_delete=models.CASCADE,
        help_text="The Template for this Provision",
        related_name="provisions",
        null=True,
        blank=True,
    )

    dropped = models.BooleanField(
        default=True, help_text="Is the connection to this Provision lost?"
    )
    # Platform specific Details (non relational Data)
    params = models.JSONField(
        null=True,
        blank=True,
        help_text="Params for the Policy (including Agent etc..)",
    )
    extensions = models.JSONField(
        null=True, blank=True, help_text="The Platform extensions"
    )
    context = models.JSONField(null=True, blank=True, help_text="The Platform context")

    #
    access = models.CharField(
        max_length=100,
        default=AccessStrategy.EVERYONE,
        choices=AccessStrategy.choices,
        help_text="Access Strategy for this Provision",
    )

    # Status Field
    status = models.CharField(
        max_length=300,
        choices=ProvisionStatus.choices,
        default=ProvisionStatus.PENDING.value,
        help_text="Current lifecycle of Provision",
    )

    statusmessage = models.CharField(
        max_length=10000,
        help_text="Clear Text status of the Provision as for now",
        blank=True,
    )
    # Meta fields of the creator of this
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        max_length=1000,
        help_text="This provision creator",
        null=True,
        blank=True,
    )
    app = models.ForeignKey(
        LokApp,
        on_delete=models.CASCADE,
        max_length=1000,
        help_text="This provision creator",
        null=True,
        blank=True,
    )

    class Meta:
        permissions = [("can_link_to", "Can link a reservation to a provision")]

    @property
    def queue(self):
        return f"provision_{self.unique}"

    def __str__(self):
        return f"Provision for Template: {self.template if self.template else ''}: {self.status}"

    def get_reservation_queues(self):
        """
        Get all reservation queues attached to this provision
        """
        return [res.queue for res in self.reservations.all()]

    def link(self, reservation) -> Tuple["Provision", List[HareMessage]]:
        """
        Link this provision to a reservation
        """
        self.reservations.add(reservation)
        forwards = []

        params = ReserveParams(**reservation.params)
        minimalInstances = params.minimalInstances or 1

        active_provisions = reservation.provisions.filter(
            status=ProvisionStatus.ACTIVE.value, dropped=False
        ).all()

        if len(active_provisions) + 1 >= minimalInstances:
            # +1 because we have not propagated our status yet
            res, resforwards = reservation.activate()
            forwards += resforwards

        if len(self.reservations.all()) == 1:
            # this means we had previously no reservations, so we need to signal that we are active
            forwards.append(
                ProvideHareMessage(
                    queue=self.agent.queue,
                    provision=self.id,
                    reservation=reservation.id,
                    template=self.template.id,
                )
            )
        else:
            forwards.append(
                ReserveHareMessage(
                    queue=self.agent.queue,
                    reservation=reservation.id,
                    provision=self.id,
                )
            )
        self.save()
        return self, forwards

    def unlink(self, reservation) -> Tuple["Provision", List[HareMessage]]:
        """
        Link this provision to a reservation
        """
        self.reservations.remove(reservation)
        forwards = []

        params = ReserveParams(**reservation.params)
        minimalInstances = params.minimalInstances or 1
        active_provisions = reservation.provisions.filter(
            status=ProvisionStatus.ACTIVE.value, dropped=False
        ).all()

        if (
            len(active_provisions) - 1 < minimalInstances
        ):  # minus one because we self *will* be critical
            res, resforwards = reservation.critical()
            forwards += resforwards

        if len(self.reservations.all()) == 0:
            self.delete()
        else:
            forwards.append(
                UnreserveHareMessage(
                    queue=self.agent.queue,
                    reservation=reservation.id,
                    provision=self.id,
                )
            )
            self.save()
        return self, forwards

    def activate(self) -> Tuple["Provision", List[HareMessage]]:
        """
        Activate this provision
        """
        self.status = ProvisionStatus.ACTIVE.value
        forwards = []
        for reservation in self.reservations.all():
            if reservation.status == ReservationStatus.ACTIVE.value:
                # omiting reservations that are already active
                continue

            params = ReserveParams(**reservation.params)
            minimalInstances = params.minimalInstances or 1

            active_provisions = reservation.provisions.filter(
                status=ProvisionStatus.ACTIVE.value, dropped=False
            ).all()

            if len(active_provisions) + 1 >= minimalInstances:
                # +1 because we have not propagated our status yet
                res, resforwards = reservation.activate()
                forwards += resforwards

        self.save()
        return self, forwards

    def critical(self) -> Tuple["Provision", List[HareMessage]]:
        """
        Critical this provision
        """
        self.status = ProvisionStatus.CRITICAL.value
        forwards = []
        for reservation in self.reservations.all():
            if reservation.status == ReservationStatus.CRITICAL.value:
                # omiting reservations that are already active
                continue

            params = ReserveParams(**reservation.params)
            minimalInstances = params.minimalInstances or 1

            active_provisions = reservation.provisions.filter(
                status=ProvisionStatus.ACTIVE.value, dropped=False
            ).all()

            if (
                len(active_provisions) - 1 < minimalInstances
            ):  # minus one because we self *will* be critical
                res, resforwards = reservation.critical()
                forwards += resforwards

        self.save()
        return self, forwards

    def drop(self) -> Tuple["Provision", List[HareMessage]]:
        """Drop this Provision (gets called by the agent)"""
        self.dropped = True
        forwards = []
        for reservation in self.reservations.all():
            if reservation.status == ReservationStatus.CRITICAL.value:
                # omiting reservations that are already active
                continue

            params = ReserveParams(**reservation.params)
            minimalInstances = params.minimalInstances or 1

            active_provisions = reservation.provisions.filter(
                status=ProvisionStatus.ACTIVE.value, dropped=False
            ).all()

            if (
                len(active_provisions) - 1 < minimalInstances
            ):  # minus one because we self *will* be critical
                res, resforwards = reservation.critical()
                forwards += resforwards

        self.save()
        return self, forwards


class ReservationLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    reservation = models.ForeignKey(
        "Reservation",
        help_text="The reservation this log item belongs to",
        related_name="log",
        on_delete=models.CASCADE,
    )
    message = models.CharField(max_length=2000, null=True, blank=True)
    level = models.CharField(
        choices=LogLevel.choices, default=LogLevel.INFO.value, max_length=200
    )


class Reservation(models.Model):
    """Reservation (CONTRACT MODEL)

    Reflects RabbitMQ Channel

    Reservations are constant logs of active connections to Arkitekt and are logging the state of the connection to the workers. They are user facing
    and are created by the user, they hold a log of all transactions that have been done since its inception, as well as as of the inputs that it was
    created by (Node and Template as desired inputs) and the currently active Topics it connects to. It also specifies the routing policy (it case a
    connection to a worker/app gets lost). A Reservation creates also a (rabbitmq) Channel that every connected Topic listens to and the specific user assigns to.
    According to its Routing Policy, if a Topic dies another Topic can eithers take over and get the Items stored in this  (rabbitmq) Channel or a specific user  event
    happens with this Assignations.

    """

    # Channel is the RabbitMQ channel that every user assigns to and that every topic listens to
    channel = models.CharField(
        max_length=2000,
        unique=True,
        default=uuid.uuid4,
        help_text="The channel of this Reservation",
    )

    happy = models.BooleanField(
        default=False,
        help_text="Is this reservation happy? (aka: does it have as many linked provisions as desired",
    )
    viable = models.BooleanField(
        default=False,
        help_text="Is this reservation viable? (aka: does it have as many linked provisions as minimal",
    )
    allow_auto_request = models.BooleanField(
        default=False,
        help_text="Allow automatic requests for this reservation",
    )

    # 1 Inputs to the the Reservation (it can be either already a template to provision or just a node)
    node = models.ForeignKey(
        Node,
        on_delete=models.CASCADE,
        help_text="The node this reservation connects",
        related_name="reservations",
    )
    title = models.CharField(
        max_length=200,
        help_text="A Short Hand Way to identify this reservation for you",
        null=True,
        blank=True,
    )
    template = models.ForeignKey(
        Template,
        on_delete=models.CASCADE,
        help_text="The template this reservation connects",
        related_name="reservations",
        null=True,
        blank=True,
    )

    # The connections
    provisions = models.ManyToManyField(
        Provision,
        help_text="The Provisions this reservation connects",
        related_name="reservations",
        null=True,
        blank=True,
    )

    # Platform specific Details (non relational Data)
    params = models.JSONField(
        default=dict, help_text="Params for the Policy (including Agent etc..)"
    )
    binds = models.JSONField(
        help_text="Params for the Policy (including Agent etc..)",
        null=True,
        blank=True,
    )

    hash = models.CharField(
        default=uuid.uuid4,
        max_length=1000,
        help_text="The hash of the Reservation",
        unique=True,
    )

    # Status Field
    status = models.CharField(
        max_length=300,
        choices=ReservationStatus.choices,
        default=ReservationStatus.ROUTING,
        help_text="Current lifecycle of Reservation",
    )
    statusmessage = models.CharField(
        max_length=1000,
        help_text="Clear Text status of the Provision as for now",
        blank=True,
    )

    # Callback
    callback = models.CharField(
        max_length=1000, help_text="Callback", blank=True, null=True
    )
    progress = models.CharField(
        max_length=1000, help_text="Provider", blank=True, null=True
    )

    # Meta fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    waiter = models.ForeignKey(
        Waiter,
        on_delete=models.CASCADE,
        max_length=1000,
        help_text="This Reservations app",
        related_name="reservations",
    )
    app = models.ForeignKey(
        LokApp,
        on_delete=models.CASCADE,
        max_length=1000,
        help_text="This Reservations app",
        null=True,
        blank=True,
    )
    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        max_length=1000,
        help_text="This Reservations creator",
        null=True,
        blank=True,
    )
    reference = models.CharField(
        max_length=1000,
        default="default",
        help_text="The Unique identifier of this Assignation",
    )
    provision = models.ForeignKey(
        "Provision",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Was this Reservation caused by a Provision?",
        related_name="caused_reservations",
    )

    objects = ReservationManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["reference", "node", "waiter"],
                name="Equal Reservation on this App by this Waiter is already in place",
            )
        ]
        permissions = [("can_assign", "Can assign to this reservation")]

    def __str__(self):
        return f"Reservation {self.id} for Node: {self.node}: {self.status}"

    @property
    def queue(self):
        return f"reservation_{self.channel}"

    def reschedule(self) -> Tuple["Reservation", List[HareMessage]]:
        """Unreserve this reservation"""
        forwards = []
        self.status = ReservationStatus.CANCELLED

        for provision in self.provisions.all():
            prov, provforwards = provision.unlink(self)  # Unlink from Provision
            forwards += provforwards

        self.save()
        return self, forwards

    def schedule(self) -> Tuple["Reservation", List[HareMessage]]:
        """Schedule this reservation"""
        forwards = []
        linked_provisions = []
        params = ReserveParams(**self.params)

        desiredInstances = params.desiredInstances or 1
        minimalInstances = params.minimalInstances or 1

        binds = BindParams(**self.binds) if self.binds else None

        if self.node is not None:
            templates = Template.objects
            templates = templates.filter(node=self.node)

            if binds:
                if binds.templates and binds.clients:
                    templates = templates.filter(
                        Q(id__in=binds.templates)
                        | Q(agent__registry__client__client_id__in=binds.clients)
                    )
                elif binds.templates:
                    templates = templates.filter(id__in=binds.templates)
                elif binds.clients:
                    templates = templates.filter(
                        agent__registry__client__client_id__in=binds.clients
                    )

            for template in templates.all():
                if len(linked_provisions) >= desiredInstances and not binds:
                    break

                linkable_provisions = (
                    get_objects_for_user(
                        self.waiter.registry.user, "facade.can_link_to"
                    )
                    .filter(template=template)
                    .all()
                )

                if linkable_provisions.count() == 0:
                    assert self.waiter.registry.user.has_perm(
                        "facade.providable", template
                    ), "User cannot provide this template and no linked provision is found"

                    prov = Provision.objects.create(
                        template=template,
                        agent=template.agent,
                        reservation=self,
                        creator=self.waiter.registry.user,
                    )

                    prov, linkforwards = prov.link(self)
                    linked_provisions.append(prov)
                    forwards += linkforwards
                else:
                    for prov in linkable_provisions:
                        prov, linkforwards = prov.link(self)
                        linked_provisions.append(prov)
                        forwards += linkforwards

        else:
            raise NotImplementedError(
                "No node specified. Template reservation not implemented yet."
            )

        self.provisions.add(*linked_provisions)
        self.status = ReservationStatus.ROUTING

        if len(self.provisions.all()) >= minimalInstances:
            self.viable = True

        if len(self.provisions.all()) >= desiredInstances:
            self.happy = True

        self.save()

        return self, forwards

    def activate(self) -> Tuple["Reservation", List[HareMessage]]:
        """Activate the reservation"""
        self.status = ReservationStatus.ACTIVE
        forwards = [
            ReservationChangedMessage(
                queue=self.waiter.queue,
                reservation=self.id,
                status=ReservationStatus.ACTIVE.value,
            )
        ]
        self.viable = True
        self.save()
        return self, forwards

    def critical(self) -> Tuple["Reservation", List[HareMessage]]:
        """Activate the reservation"""
        self.status = ReservationStatus.CRITICAL
        forwards = [
            ReservationChangedMessage(
                queue=self.waiter.queue,
                reservation=self.id,
                status=ReservationStatus.CRITICAL.value,
            )
        ]
        self.viable = False
        self.save()
        return self, forwards


class Assignation(models.Model):
    """A constant log of a tasks transition through finding a Node, Template and finally Pod , also a store for its results"""

    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        help_text="Which reservation are we assigning to",
        related_name="assignations",
        blank=True,
        null=True,
    )
    context = models.JSONField(default=dict, help_text="The Platform context")
    progress = models.IntegerField(
        null=True, blank=True, help_text="The progress of this assignation"
    )

    # 1. The State of Everything
    args = models.JSONField(blank=True, null=True, help_text="The Args", default=list)
    provision = models.ForeignKey(
        Provision,
        on_delete=models.CASCADE,
        help_text="Which Provision did we end up being assigned to",
        related_name="assignations",
        blank=True,
        null=True,
    )
    waiter = models.ForeignKey(
        Waiter,
        on_delete=models.CASCADE,
        max_length=1000,
        help_text="This Assignation app",
        null=True,
        blank=True,
        related_name="assignations",
    )
    kwargs = models.JSONField(blank=True, null=True, help_text="The Kwargs")
    returns = models.JSONField(blank=True, null=True, help_text="The Returns")
    status = models.CharField(
        max_length=300,
        choices=AssignationStatus.choices,
        default=AssignationStatus.PENDING.value,
        help_text="Current lifecycle of Assignation",
    )
    statusmessage = models.CharField(
        max_length=1000,
        help_text="Clear Text status of the Assignation as for now",
        blank=True,
    )

    # Meta fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reference = models.CharField(
        max_length=1000,
        default=uuid.uuid4,
        help_text="The Unique identifier of this Assignation considering its parent",
    )
    creator = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        max_length=1000,
        help_text="The creator is this assignation",
        null=True,
        blank=True,
    )
    app = models.ForeignKey(
        LokApp,
        on_delete=models.CASCADE,
        max_length=1000,
        help_text="The app is this assignation",
        null=True,
        blank=True,
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="The Assignations parent",
        related_name="children",
    )

    def __str__(self):
        return f"{self.status} for {self.reservation}"

    class Meta:
        pass

    def unassign(self) -> Tuple["Assignation", List[HareMessage]]:
        """Activate the reservation"""
        self.status = AssignationStatus.CANCELING
        if self.provision:
            forwards = [
                UnassignHareMessage(
                    queue=self.provision.queue,
                    assignation=self.id,
                    provision=self.provision.id,
                )
            ]
        else:
            forwards = []
        self.save()
        return self, forwards


class AssignationLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    assignation = models.ForeignKey(
        Assignation,
        help_text="The reservation this log item belongs to",
        related_name="log",
        on_delete=models.CASCADE,
    )
    message = models.CharField(max_length=2000, null=True, blank=True)
    level = models.CharField(
        choices=LogLevel.choices, default=LogLevel.INFO.value, max_length=200
    )


class TestCase(models.Model):
    node = models.ForeignKey(
        Node,
        on_delete=models.CASCADE,
        related_name="testcases",
        help_text="The node this test belongs to",
    )
    key = models.CharField(max_length=2000, null=True, blank=True)
    name = models.CharField(max_length=2000, null=True, blank=True)
    description = models.CharField(max_length=2000, null=True, blank=True)
    is_benchmark = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class TestResult(models.Model):
    case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name="results")
    template = models.ForeignKey(
        Template, on_delete=models.CASCADE, related_name="testresults"
    )
    passed = models.BooleanField(default=False)
    result = models.JSONField(default=dict, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


import facade.signals

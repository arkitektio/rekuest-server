"""Typed, fully-documented configuration schema for the **rekuest** service.

Owned by this service. Values resolve (highest precedence first) from init
kwargs, environment variables (nested via ``__`` — e.g. ``POSTGRES__PASSWORD``),
then the YAML file (``config.yaml`` where the service runs by default; override with
``ARKITEKT_CONFIG_FILE``). Secret fields have **no default**: loading fails fast
with a ``ValidationError`` if they are not supplied via config or environment.
"""

from typing import List, Optional

from arkitekt_service.server import settings as shared
from arkitekt_service.server.settings import DjangoSettings, InstanceSettings, PostgresSettings, ServiceSettings
from authentikate.base_models import AuthentikateSettings
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from facade.json_types import JSON

class RedisSettings(shared.RedisSettings):
    """Redis connection (channel layer / cache)."""

    channel_prefix: str = Field(default="rekuest", description="Key prefix for the channels_redis channel layer. Must differ from every other service on the same redis, or group messages bleed across services.")
    key_prefix: str = Field(default="rekuest", description="Namespace for every redis key this service writes (agent queues, probe state, sweep and upkeep tokens, replay guards). Give each deployment sharing one redis a distinct value.")
    channel_capacity: int = Field(default=5000, description="channels_redis capacity. This bounds the ONE per-process receive queue shared by every socket and subscription in a replica — messages beyond it are dropped silently — so it must be far above the library default of 100.")


class DescriptorManifest(BaseModel):
    """One descriptor of a structure's objects, as a service declares it."""

    # Open, like everything a service says it hosts: how that is spelt is the service's
    # release, and a key a newer one adds is not this config's mistake.
    model_config = ConfigDict(extra="allow")

    key: str = Field(description="The descriptor's key, e.g. @mikro/n_channels.")
    type: Optional[str] = Field(default=None, description="What its value is (ANY, INT, FLOAT, STRING, BOOL, LIST). Default: ANY.")
    description: Optional[str] = Field(default=None, description="What it says about an object.")


class StructureManifest(BaseModel):
    """A structure a service hosts, as it declares it."""

    model_config = ConfigDict(extra="allow")

    identifier: str = Field(description="What the structure is known by across the hub, e.g. @mikro/arraydataset.")
    label: Optional[str] = Field(default=None, description="What a user reads it as.")
    description: Optional[str] = Field(default=None, description="What it is.")
    descriptors: Optional[list[DescriptorManifest]] = Field(default=None, description="What is known of each of its objects, in order.")


class SignalManifest(BaseModel):
    """A signal a service emits, as it declares it."""

    model_config = ConfigDict(extra="allow")

    identifier: str = Field(description="The structure the signal is about.")
    kinds: Optional[list[str]] = Field(default=None, description="What is announced: CREATED, UPDATED, DELETED. Default: CREATED.")
    descriptors: Optional[list[str]] = Field(default=None, description="The descriptor keys an announcement carries: what a trigger can test.")
    description: Optional[str] = Field(default=None, description="What it announces.")


class ServiceHosts(BaseModel):
    """What a service hosts and emits, said inline: the `hosts` of its image's description.

    The same two lists its manifest carries (``<url>/manifest``). Here both are always known:
    an empty list is "nothing", and takes rows away.
    """

    model_config = ConfigDict(extra="allow")

    structures: list[StructureManifest] = Field(default_factory=list, description="The structures the service hosts, with their descriptors.")
    signals: list[SignalManifest] = Field(default_factory=list, description="The signals it emits about them.")


class ServiceEntry(BaseModel):
    """One of this hub's services: what it hosts and emits is catalogued (see ``facade.service_catalog``).

    From ``hosts`` when the entry carries it (an installer read it from the service's image, so
    the catalog is there before the service runs), else from the service's own manifest.

    A service is not an agent and no agent comes of this entry. No secret: requests both ways are
    signed with each side's instance key and checked against the hub's trust bundle (``instance.trust``).
    """

    name: str = Field(description="The service's name (e.g. 'mikro'): what it is catalogued under, and its signal endpoint.")
    url: str = Field(description="The service's `rekuest_service` endpoint, e.g. http://mikro:80/mikro/_rekuest/service; its manifest is read at <url>/manifest.")
    identifier: Optional[str] = Field(default=None, description="The fakts identifier of the service's instance — what its key is listed under in the trust bundle. Default: live.arkitekt.<name>.")
    hosts: Optional[ServiceHosts] = Field(default=None, description="What the service hosts and emits, as its image says. Set: catalogued from here, and the service is not asked. Unset: its manifest is fetched.")


class HookAgentEntry(BaseModel):
    """One of this hub's hook agents: an agent rekuest reaches over HTTP and gives every organization (see ``facade.hook_agents``).

    Not tied to a service: it may run in a service's process or anywhere else. No secret:
    requests both ways are signed with instance keys.
    """

    name: str = Field(description="The agent's name: what every organization sees it as.")
    hook_url: str = Field(description="Where rekuest POSTs the agent's Assigns — its `rekuest_hook` endpoint, e.g. http://mikro:80/mikro/_rekuest/hook; its manifest is read at <hook_url>/manifest.")
    identifier: Optional[str] = Field(default=None, description="The fakts identifier of the instance the agent runs in — what its key is listed under in the trust bundle. Default: live.arkitekt.<name>.")


class InstanceBlock(InstanceSettings):
    """This instance's key — its only secret towards the hub's other services — and whom it trusts."""

    private_key: str = Field(description="Ed25519 private key (PKCS#8 PEM). Signs provenance tokens and requests to the hub's services. Secret — must be set.")


class RekuestBlock(BaseModel):
    """Rekuest assignment grace + capability tuning.

    Closed: a key here that no field claims is reported (see :func:`unread`), since this is
    the block a release renames keys in.
    """

    grace_default: int = Field(default=30, description="Default reclaim grace window (seconds) after a disconnect.")
    sweep_interval: int = Field(default=5, description="How often (seconds) takt sweeps the DB-held deadlines. Bounds how late a deadline can fire.")
    pickup_deadline: int = Field(default=60, description="Seconds a dispatched task may go without any report from its (live) agent before the Assign is redelivered once, then failed; 0 disables.")
    disconnected_expiry: int = Field(default=3600, description="Seconds a DISCONNECTED (fate unknown) task stays recoverable before it is finalized as terminal; 0 = never.")
    control_deadline: int = Field(
        default=60,
        description="Seconds an unconfirmed cancel may wait before it escalates to an interrupt (and an unconfirmed interrupt before it is finalized); 0 disables. On by default: a Cancel/Interrupt frame lost in transit (a displaced connection, a redis restart) is otherwise never noticed — the DB says CANCELLING while the agent never heard of it.",
    )
    hook_signature_mode: str = Field(default="compat", description="HookAgent HTTP signatures. 'compat': accept the timestamped V1 signature or the legacy body-only one, send both. 'strict': V1 only (replay-protected).")
    hook_max_skew: int = Field(default=300, description="Maximum age/clock skew (seconds) accepted for a V1-signed HookAgent request; also bounds the replay-guard window.")
    task_retention: int = Field(default=0, description="Seconds to keep terminal root task trees; 0 disables deletion. Deleting past runs also removes them from replay (reusable_task_for). Suggested production value: 2592000 (30 days).")
    ephemeral_task_retention: int = Field(default=86400, description="Seconds to keep terminal EPHEMERAL root task trees (housekeeping runs of schedules with ephemeralRuns); applies even while task_retention is 0. 0 disables.")
    takt_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("takt_url", "agentd_url"),
        description="takt's internal listener (`TAKT_INTERNAL_BIND`), with its script name: not the address agents connect to. Defaults to http://takt:8081/<django.force_script_name>. Assigns, controls, registrations, deletes, probes and running a schedule now go through its internal API; while it is unreachable each of those raises TaktUnavailable. `agentd_url` is its former name and still read.",
    )
    server_url: Optional[str] = Field(default=None, description="Read by takt, not by this server: where takt reaches this server for the upkeep jobs, with its script name. Defaults to http://rekuest:80/<django.force_script_name>; empty turns upkeep off.")
    takt_socket: Optional[str] = Field(default=None, description="takt's internal listener as a unix socket this server and takt both mount (takt: `TAKT_INTERNAL_BIND=unix:<path>`). When set, the internal API is reached through it and only the path of `takt_url` is used.")
    identifier: str = Field(default="live.arkitekt.rekuest", description="This rekuest's fakts identifier — what its key is listed under in the hub trust bundle, and what services require rekuest's requests to come from.")
    services: list[ServiceEntry] = Field(default_factory=list, description="This hub's services: each one's structures and signals are catalogued from its manifest. Says nothing about agents.")
    hook_agents: list[HookAgentEntry] = Field(default_factory=list, description="This hub's hook agents: each is given to every organization, with the actions its manifest lists. Nothing is scheduled or triggered by itself.")
    trigger_max_depth: int = Field(default=3, description="How many trigger firings may chain (a triggered run's object signalling another trigger …) before a signal stops firing — the loop guard.")
    dependency_max_depth: int = Field(default=8, description="How many levels of dependencies an assign resolves below the assigned implementation before it refuses.")
    signal_retention: int = Field(default=604800, description="Seconds to keep processed signals (the runs they caused keep their link as null afterwards); 0 keeps them forever.")
    probe_ttl: int = Field(default=3600, description="Lifetime (seconds) of a probe's redis state while live.")
    probe_linger: int = Field(default=300, description="How long (seconds) a terminal call's state lingers for late subscribers.")
    probe_max_inflight: int = Field(default=32, description="Maximum concurrent probes per caller.")


class ProvenanceBlock(BaseModel):
    """Rekuest provenance (attestation) policy. Tokens are signed with the instance key (`instance`), `kid` its thumbprint."""

    issuer: str = Field(default="rekuest", description="Provenance token issuer (iss).")
    token_ttl_seconds: int = Field(default=3600, description="Provenance token lifetime (seconds).")
    human_roles: List[str] = Field(default_factory=list, description="Roles marking an accountable human; empty disables the human-root invariant.")
    strict: bool = Field(default=False, description="Require the human-root invariant when minting.")


class DatalayerBucket(BaseModel):
    """A single S3 bucket binding within the datalayer."""

    model_config = ConfigDict(extra="allow")

    bucket: str = Field(description="S3 bucket name.")


class DatalayerSettings(BaseModel):
    """S3 storage connection and buckets (the datalayer module; replaces the old top-level ``s3`` block)."""

    model_config = ConfigDict(extra="allow")

    access_key: str = Field(description="S3 access key. Secret — must be set.")
    secret_key: str = Field(description="S3 secret key. Secret — must be set.")
    host: Optional[str] = Field(default=None, description="S3 endpoint host.")
    port: Optional[int] = Field(default=None, description="S3 endpoint port.")
    protocol: str = Field(default="http", description="S3 endpoint protocol (http or https).")
    region: str = Field(default="us-east-1", description="S3 region name.")
    media: Optional[DatalayerBucket] = Field(default=None, description="Bucket for media / general file storage.")
    zarr: Optional[DatalayerBucket] = Field(default=None, description="Bucket for Zarr arrays.")
    parquet: Optional[DatalayerBucket] = Field(default=None, description="Bucket for Parquet tables.")
    bigfile: Optional[DatalayerBucket] = Field(default=None, description="Bucket for large binary files.")


class EmbeddingsSettings(BaseModel):
    """Semantic search: a model2vec static model embeds name + description into pgvector columns.

    Every value has a default, so the block may be omitted. The model is not configuration:
    it is a constant of the release (``embeddings.engine.MODEL``) and the image carries it.
    """

    enabled: bool = Field(default=True, description="Embed rows on save and give `search` a semantic leg. Off: `search` is substring-only and the embedding column stays NULL.")
    distance_threshold: float = Field(default=0.55, description="Cosine distance (0 identical, 1 unrelated) above which a row no longer counts as a semantic `search` hit.")


class Settings(ServiceSettings):
    """Top-level, validated configuration for the rekuest service."""

    django: DjangoSettings = Field(description="Core Django settings.")
    postgres: PostgresSettings = Field(description="PostgreSQL connection.")
    redis: RedisSettings = Field(description="Redis connection.")
    authentikate: AuthentikateSettings = Field(description="Token-verification config (authentikate).")
    rekuest: RekuestBlock = Field(default_factory=RekuestBlock, description="Grace/capability tuning.")
    provenance: ProvenanceBlock = Field(description="Provenance signing config (requires a static Ed25519 key).")
    instance: InstanceBlock = Field(description="This instance's key and the hub trust bundle (no shared secrets between services).")
    datalayer: Optional[DatalayerSettings] = Field(default=None, description="Optional S3 config forwarded to the datalayer app.")
    embeddings: EmbeddingsSettings = Field(default_factory=EmbeddingsSettings, description="Semantic search: on or off, and its threshold.")

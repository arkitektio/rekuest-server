"""Typed, fully-documented configuration schema for the **rekuest** service.

Owned by this service. Values resolve (highest precedence first) from init
kwargs, environment variables (nested via ``__`` — e.g. ``POSTGRES__PASSWORD``),
then the YAML file (the mount's ``config.yaml`` by default; override with
``ARKITEKT_CONFIG_FILE``). Secret fields have **no default**: loading fails fast
with a ``ValidationError`` if they are not supplied via config or environment.
"""

import dataclasses
import os
import typing
from collections.abc import Mapping
from typing import List, Optional

import yaml
from authentikate.base_models import AuthentikateSettings
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from facade.json_types import JSON

_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")


class AdminSettings(BaseModel):
    """Django superuser created on first boot."""

    username: str = Field(description="Superuser login name.")
    password: str = Field(description="Superuser password. Secret — must be set.")
    email: Optional[str] = Field(default=None, description="Superuser email address.")


class DjangoSettings(BaseModel):
    """Core Django framework settings."""

    secret_key: str = Field(description="Django SECRET_KEY for cryptographic signing. Secret — must be set.")
    debug: bool = Field(default=False, description="Enable Django debug mode (never in production).")
    log_level: str = Field(default="INFO", description="Root logger level (e.g. DEBUG, INFO, WARNING). The LOG_LEVEL env var overrides it.")
    enable_rich_logging: bool = Field(default=False, description="Render console logs with rich (colours, boxed tracebacks). A dev convenience; off by default, as plain one-line records suit container logs.")
    hosts: List[str] = Field(default_factory=lambda: ["*"], description="ALLOWED_HOSTS entries.")
    use_x_forwarded_host: bool = Field(default=True, description="Trust the X-Forwarded-Host header behind a reverse proxy.")
    admin: Optional[AdminSettings] = Field(default=None, description="Superuser provisioned on first boot.")
    csrf_trusted_origins: List[str] = Field(default_factory=lambda: ["http://localhost", "https://localhost"], description="CSRF_TRUSTED_ORIGINS for unsafe (POST) requests.")
    force_script_name: str = Field(default="", description="URL path prefix (FORCE_SCRIPT_NAME) this service is served under.")


class PostgresSettings(BaseModel):
    """PostgreSQL database connection (Django ``DATABASES['default']``)."""

    model_config = ConfigDict(extra="allow")

    engine: str = Field(default="django.db.backends.postgresql", description="Django database backend (PostgreSQL).")
    db_name: str = Field(description="Database name.")
    username: str = Field(description="Database user.")
    password: str = Field(description="Database password. Secret — must be set.")
    host: str = Field(description="Database host.")
    port: int = Field(default=5432, description="Database port.")


class RedisSettings(BaseModel):
    """Redis connection (channel layer / cache)."""

    model_config = ConfigDict(extra="allow")

    host: str = Field(description="Redis host.")
    port: int = Field(default=6379, description="Redis port.")
    key_prefix: str = Field(default="rekuest", description="Namespace for every redis key this service writes (agent queues, probe state, sweep and upkeep tokens, replay guards). Give each deployment sharing one redis a distinct value.")
    channel_prefix: str = Field(default="rekuest", description="Key prefix for the channels_redis channel layer. Must differ from every other service on the same redis, or group messages bleed across services.")
    channel_capacity: int = Field(default=5000, description="channels_redis capacity. This bounds the ONE per-process receive queue shared by every socket and subscription in a replica — messages beyond it are dropped silently — so it must be far above the library default of 100.")


class ServiceEntry(BaseModel):
    """One of this hub's services: what it hosts and emits is catalogued from its manifest (see ``facade.service_catalog``).

    A service is not an agent and no agent comes of this entry. No secret: requests both ways are
    signed with each side's instance key and checked against the hub's trust bundle (``instance.trust``).
    """

    name: str = Field(description="The service's name (e.g. 'mikro'): what it is catalogued under, and its signal endpoint.")
    url: str = Field(description="The service's `rekuest_service` endpoint, e.g. http://mikro:80/mikro/_rekuest/service; its manifest is read at <url>/manifest.")
    identifier: Optional[str] = Field(default=None, description="The fakts identifier of the service's instance — what its key is listed under in the trust bundle. Default: live.arkitekt.<name>.")


class HookAgentEntry(BaseModel):
    """One of this hub's hook agents: an agent rekuest reaches over HTTP and gives every organization (see ``facade.hook_agents``).

    Not tied to a service: it may run in a service's process or anywhere else. No secret:
    requests both ways are signed with instance keys.
    """

    name: str = Field(description="The agent's name: what every organization sees it as.")
    hook_url: str = Field(description="Where rekuest POSTs the agent's Assigns — its `rekuest_hook` endpoint, e.g. http://mikro:80/mikro/_rekuest/hook; its manifest is read at <hook_url>/manifest.")
    identifier: Optional[str] = Field(default=None, description="The fakts identifier of the instance the agent runs in — what its key is listed under in the trust bundle. Default: live.arkitekt.<name>.")


class TrustBlock(BaseModel):
    """Where the hub's instance public keys come from: the coord's bundle, or inline."""

    jwks_uri: Optional[str] = Field(default=None, description="The coord's hub-keys URL (the fakts `self.hub_keys_url`).")
    jwks: Optional[dict[str, JSON]] = Field(default=None, description="The bundle inline (a JWKS whose keys carry `service`), for a hub not enrolled yet.")


class InstanceBlock(BaseModel):
    """This instance's key — its only secret towards the hub's other services — and whom it trusts."""

    private_key: str = Field(description="Ed25519 private key (PKCS#8 PEM). Signs provenance tokens and requests to the hub's services. Secret — must be set.")
    trust: TrustBlock = Field(default_factory=TrustBlock, description="The hub's trust bundle.")


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

    Every value has a default, so the block may be omitted. The vector width is fixed by the
    model *and* by the database column; see CONFIG.md before changing ``model``.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    enabled: bool = Field(default=True, description="Embed rows on save and give `search` a semantic leg. Off: `search` is substring-only and the embedding columns stay NULL.")
    model: str = Field(default="minishlab/potion-base-8M", description="model2vec model id. Recorded on every row; rows embedded by another model are re-embedded in-process and skipped by vector search until then.")
    model_path: Optional[str] = Field(default=None, description="Directory holding the weights of `model` (save_pretrained layout). The Docker image bakes them under /opt/models and sets EMBEDDINGS__MODEL_PATH; unset, model2vec downloads from Hugging Face on first use.")
    dimensions: int = Field(default=256, description="Vector width of `model`. Also the width of the database column, so changing it is a migration. Checked against both at startup.")
    distance_threshold: float = Field(default=0.55, description="Cosine distance (0 identical, 1 unrelated) above which a row no longer counts as a semantic `search` hit.")
    sweep_interval: int = Field(default=30, description="Seconds between in-process passes that re-embed rows whose `embedding_model` is not `model`. Unused by rekuest: takt asks for the re-embed (every 30 s).")
    sweep_batch_size: int = Field(default=200, description="Rows re-embedded per pass.")


class Settings(BaseSettings):
    """Top-level, validated configuration for the rekuest service."""

    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    django: DjangoSettings = Field(description="Core Django settings.")
    postgres: PostgresSettings = Field(description="PostgreSQL connection.")
    redis: RedisSettings = Field(description="Redis connection.")
    authentikate: AuthentikateSettings = Field(description="Token-verification config (authentikate).")
    rekuest: RekuestBlock = Field(default_factory=RekuestBlock, description="Grace/capability tuning.")
    provenance: ProvenanceBlock = Field(description="Provenance signing config (requires a static Ed25519 key).")
    instance: InstanceBlock = Field(description="This instance's key and the hub trust bundle (no shared secrets between services).")
    datalayer: Optional[DatalayerSettings] = Field(default=None, description="Optional S3 config forwarded to the datalayer app.")
    embeddings: EmbeddingsSettings = Field(default_factory=EmbeddingsSettings, description="Semantic search model and thresholds.")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: explicit init kwargs > environment variables > YAML file.
        path = os.environ.get("ARKITEKT_CONFIG_FILE", _DEFAULT_CONFIG)
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=path),
            file_secret_settings,
        )


@dataclasses.dataclass(frozen=True)
class Unread:
    """What a config file says that this release does not read as written."""

    unknown: list[str]
    """Keys no setting claims, as dotted paths: a misspelling, or a key of another release."""
    renamed: list[tuple[str, str]]
    """Keys still read under a former name, with the name they have now."""

    def __bool__(self) -> bool:
        """Whether there is anything to say."""
        return bool(self.unknown or self.renamed)


def config_path() -> str:
    """The YAML file the settings are read from."""
    return os.environ.get("ARKITEKT_CONFIG_FILE", _DEFAULT_CONFIG)


def _models_of(annotation: object) -> list[type[BaseModel]]:
    """This module's settings models an annotation holds: itself, or inside ``Optional[...]`` / ``list[...]``.

    Only this module's: a block another package defines (``authentikate``) is that package's to
    judge, and its aliases are spellings, not former names.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation] if annotation.__module__ == __name__ else []
    return [model for inner in typing.get_args(annotation) for model in _models_of(inner)]


def _names(model: type[BaseModel]) -> dict[str, str]:
    """Every key ``model`` reads, to the field's own name: its fields and their former names."""
    names: dict[str, str] = {}
    for name, field in model.model_fields.items():
        names[name] = name
        alias = field.validation_alias
        for former in alias.choices if isinstance(alias, AliasChoices) else [alias]:
            if isinstance(former, str):
                names[former] = name
    return names


def _unread(model: type[BaseModel], written: Mapping[str, JSON], path: str, into: Unread) -> None:
    # A block that passes its extras on (a connection's driver options) and the top level,
    # which every service of a hub shares the shape of, are open: nothing there is unknown.
    closed = model.model_config.get("extra") != "allow" and not issubclass(model, BaseSettings)
    names = _names(model)
    for key, value in written.items():
        where = f"{path}{key}"
        name = names.get(key)
        if name is None:
            if closed:
                into.unknown.append(where)
            continue
        if name != key:
            into.renamed.append((where, f"{path}{name}"))
        for inner in _models_of(model.model_fields[name].annotation):
            for index, item in enumerate(value) if isinstance(value, list) else [(None, value)]:
                if isinstance(item, dict):
                    _unread(inner, item, f"{where}." if index is None else f"{where}[{index}].", into)


def unread(written: Mapping[str, JSON] | None = None) -> Unread:
    """What the config file (or ``written``) says that this release does not read as written.

    A setting nobody reads is silent by nature: the service starts, with the default. This is
    what makes it loud — a system check at boot, and ``validate_settings --strict``, which an
    installer runs against a release before it moves a hub to it.
    """
    if written is None:
        try:
            with open(config_path(), encoding="utf-8") as file:
                loaded: object = yaml.safe_load(file)
        except OSError:
            loaded = None
        written = loaded if isinstance(loaded, dict) else {}
    found = Unread(unknown=[], renamed=[])
    _unread(Settings, written, "", found)
    return found

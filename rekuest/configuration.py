"""Typed, fully-documented configuration schema for the **rekuest** service.

Owned by this service. Values resolve (highest precedence first) from init
kwargs, environment variables (nested via ``__`` — e.g. ``POSTGRES__PASSWORD``),
then the YAML file (the mount's ``config.yaml`` by default; override with
``ARKITEKT_CONFIG_FILE``). Secret fields have **no default**: loading fails fast
with a ``ValidationError`` if they are not supplied via config or environment.
"""

import os
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from authentikate.base_models import AuthentikateSettings

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
    key_prefix: str = Field(default="rekuest", description="Namespace for every redis key this service writes (agent queues, probe state, reaper token, webhook replay guard). Give each deployment sharing one redis a distinct value.")
    channel_prefix: str = Field(default="rekuest", description="Key prefix for the channels_redis channel layer. Must differ from every other service on the same redis, or group messages bleed across services.")
    channel_capacity: int = Field(default=5000, description="channels_redis capacity. This bounds the ONE per-process receive queue shared by every socket and subscription in a replica — messages beyond it are dropped silently — so it must be far above the library default of 100.")


class RekuestBlock(BaseModel):
    """Rekuest assignment grace + capability tuning."""

    model_config = ConfigDict(extra="allow")

    grace_default: int = Field(default=30, description="Default reclaim grace window (seconds) after a disconnect.")
    grace_physical: int = Field(default=5, description="Grace window (seconds) for effect:physical work.")
    progress_lease: int = Field(default=0, description="Progress lease (seconds); 0 disables the wedged-task lease.")
    sweep_interval: int = Field(default=5, description="How often (seconds) the in-process reaper sweeps the DB-held deadlines. Bounds how late a deadline can fire.")
    pickup_deadline: int = Field(default=60, description="Seconds a dispatched task may go without any report from its (live) agent before the Assign is redelivered once, then failed; 0 disables.")
    disconnected_expiry: int = Field(default=3600, description="Seconds a DISCONNECTED (fate unknown) task stays recoverable before it is finalized as terminal; 0 = never.")
    control_deadline: int = Field(default=60, description="Seconds an unconfirmed cancel may wait before it escalates to an interrupt (and an unconfirmed interrupt before it is finalized); 0 disables. On by default: a Cancel/Interrupt frame lost in transit (a displaced connection, a redis restart) is otherwise never noticed — the DB says CANCELLING while the agent never heard of it.")
    hook_signature_mode: str = Field(default="compat", description="HookAgent HTTP signatures. 'compat': accept the timestamped V1 signature or the legacy body-only one, send both. 'strict': V1 only (replay-protected).")
    hook_max_skew: int = Field(default=300, description="Maximum age/clock skew (seconds) accepted for a V1-signed HookAgent request; also bounds the replay-guard window.")
    task_retention: int = Field(default=0, description="Seconds to keep terminal root task trees; 0 disables deletion. Deleting past runs also removes them from replay (reusable_task_for). Suggested production value: 2592000 (30 days).")
    probe_ttl: int = Field(default=3600, description="Lifetime (seconds) of a probe's redis state while live.")
    probe_linger: int = Field(default=300, description="How long (seconds) a terminal call's state lingers for late subscribers.")
    probe_max_inflight: int = Field(default=32, description="Maximum concurrent probes per caller.")


class ProvenanceBlock(BaseModel):
    """Rekuest provenance (attestation) signing keypair and policy."""

    model_config = ConfigDict(extra="allow")

    issuer: str = Field(default="rekuest", description="Provenance token issuer (iss).")
    kid: str = Field(default="rekuest-prov-1", description="Key id published at the JWKS endpoint.")
    private_key: str = Field(description="Ed25519 signing key (PEM). Secret — must be set; the facade refuses to start without it.")
    public_key: Optional[str] = Field(default=None, description="Ed25519 verifying key (PEM, published via JWKS). Derived from the private key when omitted.")
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
    sweep_interval: int = Field(default=30, description="Seconds between in-process passes that re-embed rows whose `embedding_model` is not `model`. rekuest folds this into its reaper tick instead.")
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

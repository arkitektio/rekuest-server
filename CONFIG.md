# Rekuest — Configuration Reference

This document explains how the **rekuest** service is configured, then lists every
configuration value, its environment-variable name, its default, and what it does.

The single source of truth for the schema is
[`rekuest/configuration.py`](rekuest/configuration.py); this file documents it for
humans. If the two ever disagree, the code wins — and you can always print the live,
resolved configuration with `python manage.py validate_settings` (see below).

---

## How configuration works

Configuration is a typed [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
schema. Values are resolved from several sources, **highest precedence first**:

1. **Init kwargs** — values passed directly in code (rarely used; tests).
2. **Environment variables** — override anything in the YAML file.
3. **The YAML file** — [`config.yaml`](config.yaml) by default.
4. **File secrets** — Docker/systemd secret files, if used.

So an environment variable always beats the YAML file, which makes containerized
overrides easy without editing the mounted config.

### The YAML file

By default the service reads `config.yaml` next to the project. Point it elsewhere with
the `ARKITEKT_CONFIG_FILE` environment variable:

```bash
ARKITEKT_CONFIG_FILE=/etc/rekuest/config.yaml python manage.py runserver
```

The file is a nested mapping, one top-level key per configuration *block*:

```yaml
django:
  secret_key: "change-me"
  debug: false
postgres:
  db_name: rekuest_db
  username: rekuest
  password: "change-me"
  host: db
  port: 5432
redis:
  host: redis
  port: 6379
```

### Environment variables (the `__` rule)

Every value is also settable from the environment. The nesting is expressed with a
**double-underscore** (`__`) delimiter, and names are case-insensitive:

| YAML path | Environment variable |
|---|---|
| `postgres.password` | `POSTGRES__PASSWORD` |
| `postgres.port` | `POSTGRES__PORT` |
| `django.debug` | `DJANGO__DEBUG` |
| `provenance.token_ttl_seconds` | `PROVENANCE__TOKEN_TTL_SECONDS` |

Lists and nested objects (e.g. `authentikate.issuers`) are awkward to express as
environment variables — prefer the YAML file for those and use env vars for the flat
scalars (hosts, ports, passwords, toggles).

### Secrets fail fast

Fields marked **secret / required** below have **no default**. If they are missing from
both the YAML file and the environment, the service refuses to start and raises a
`pydantic.ValidationError` naming the missing field. The same error blocks
`manage.py` entirely, so a broken config cannot be deployed silently.

### Validating a configuration

Run the bundled command to load the config exactly as the app would, validate it, and
print the fully-resolved result as a tree with **secrets redacted**:

```bash
python manage.py validate_settings
```

- Valid config → prints a green `Configuration valid` tree and exits `0`.
- Invalid config → prints each offending field and its error, and exits `1`.

It honors `ARKITEKT_CONFIG_FILE`, so you can validate an alternate file the same way.
(Note: because Django loads settings on startup, a fundamentally invalid config also
surfaces the same validation errors when running *any* `manage.py` command.)

---

## Configuration reference

Secret fields are flagged with 🔒. "Required" means there is no default.

### `django` — core Django framework settings

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `secret_key` 🔒 | `DJANGO__SECRET_KEY` | str | **required** | Django `SECRET_KEY` for cryptographic signing. |
| `debug` | `DJANGO__DEBUG` | bool | `false` | Enable Django debug mode. Never enable in production. |
| `hosts` | `DJANGO__HOSTS` | list[str] | `["*"]` | `ALLOWED_HOSTS` entries. |
| `use_x_forwarded_host` | `DJANGO__USE_X_FORWARDED_HOST` | bool | `true` | Trust the `X-Forwarded-Host` header behind a reverse proxy. |
| `admin` | `DJANGO__ADMIN__*` | object | `null` | Superuser provisioned on first boot (see below). |
| `csrf_trusted_origins` | `DJANGO__CSRF_TRUSTED_ORIGINS` | list[str] | `["http://localhost", "https://localhost"]` | `CSRF_TRUSTED_ORIGINS` for unsafe (POST) requests. |
| `force_script_name` | `DJANGO__FORCE_SCRIPT_NAME` | str | `""` | URL path prefix this service is served under (`FORCE_SCRIPT_NAME`). |

#### `django.admin` — superuser created on first boot

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `username` | `DJANGO__ADMIN__USERNAME` | str | **required** | Superuser login name. |
| `password` 🔒 | `DJANGO__ADMIN__PASSWORD` | str | **required** | Superuser password. |
| `email` | `DJANGO__ADMIN__EMAIL` | str | `null` | Superuser email address. |

### `postgres` — PostgreSQL database (Django `DATABASES['default']`)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `engine` | `POSTGRES__ENGINE` | str | `django.db.backends.postgresql` | Django database backend. |
| `db_name` | `POSTGRES__DB_NAME` | str | **required** | Database name. |
| `username` | `POSTGRES__USERNAME` | str | **required** | Database user. |
| `password` 🔒 | `POSTGRES__PASSWORD` | str | **required** | Database password. |
| `host` | `POSTGRES__HOST` | str | **required** | Database host. |
| `port` | `POSTGRES__PORT` | int | `5432` | Database port. |

### `redis` — Redis connection (channel layer / agent queue)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `host` | `REDIS__HOST` | str | **required** | Redis host. |
| `port` | `REDIS__PORT` | int | `6379` | Redis port. |
| `key_prefix` | `REDIS__KEY_PREFIX` | str | `rekuest` | Namespace for every redis key this service writes (agent queues, probe state, reaper token, webhook replay guard). Two deployments sharing one redis MUST differ here, or agent 42 of one receives the other's Assigns. |
| `channel_prefix` | `REDIS__CHANNEL_PREFIX` | str | `rekuest` | Key prefix for the `channels_redis` channel layer. Must differ from every other service on the same redis, or group messages bleed between services. |
| `channel_capacity` | `REDIS__CHANNEL_CAPACITY` | int | `5000` | `channels_redis` capacity. This bounds the **one** receive queue a whole replica shares — not one per socket — and messages beyond it are dropped silently, so it is set far above the library default of 100. |

### `authentikate` — inbound token verification

Configures how incoming JWT access tokens are verified (the shared `authentikate`
library). At least one issuer is required.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `issuers` | — (use YAML) | list[issuer] | **required** | Trusted token issuers whose keys verify incoming tokens (see issuer kinds below). |
| `audience` | `AUTHENTIKATE__AUDIENCE` | str | **required** | This service's identifier, checked against an incoming token's `aud` claim. Required so the choice is always deliberate. `"*"` accepts a token minted for any service — the token must still carry an `aud`, so the wildcard widens the check rather than removing it. A token whose own `aud` is `"*"` gains nothing by it: a service configured with a literal audience still rejects that token. |
| `authorization_headers` | `AUTHENTIKATE__AUTHORIZATION_HEADERS` | list[str] | `["Authorization", "X-Authorization", "AUTHORIZATION", "authorization"]` | Request headers searched (in order) for a Bearer token. |
| `provenance_header` | `AUTHENTIKATE__PROVENANCE_HEADER` | list[str] | rekuest/provenance task header names | Request headers searched for an inbound provenance token. |
| `static_tokens` | — (use YAML) | map | `{}` | Pre-defined tokens that bypass signature verification. **Tests only.** |
| `provenance` | — (use YAML) | object | `null` | Inbound provenance-token verification (separate issuers/`audience`/`algorithms`; `null` disables it). |

Each entry in `issuers` is discriminated by its `kind`:

- `kind: rsa` — inline PEM RSA public key. Fields: `iss`, `kid` (default `1`), `public_key`.
- `kind: rsa_file` — RSA public key read from a PEM file. Fields: `iss`, `kid`, `public_key_pem_file`.
- `kind: jwks_dict` — inline JWKS document. Fields: `iss`, `jwks` (a dict with a `keys` list).
- `kind: jwks_uri` — JWKS fetched from a remote endpoint. Fields: `iss`, `jwks_uri`.

```yaml
authentikate:
  audience: "*"
  issuers:
    - kind: rsa
      iss: lok
      kid: lok-key-1
      public_key: "ssh-rsa AAAA..."
  static_tokens: {}
```

### `rekuest` — deadlines, retention and probe limits

Every window the server enforces over agent work: how long a lost agent's tasks are held
before being failed, how long a task may go unreported, how long finished work is kept, and
the probe limits. All optional with sensible defaults.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `grace_default` | `REKUEST__GRACE_DEFAULT` | int | `30` | Default reclaim grace window (seconds) after a disconnect. |
| `grace_physical` | `REKUEST__GRACE_PHYSICAL` | int | `5` | Grace window (seconds) for `effect:physical` work. |
| `progress_lease` | `REKUEST__PROGRESS_LEASE` | int | `0` | Progress lease (seconds); `0` disables the wedged-task lease. |
| `hook_signature_mode` | `REKUEST__HOOK_SIGNATURE_MODE` | str | `compat` | HookAgent HTTP signatures. `compat` accepts the timestamped `X-Rekuest-Signature-V1` **or** the legacy body-only `X-Rekuest-Signature`, and sends both. `strict` accepts and sends V1 only — the legacy signature is replayable, so move to `strict` once your HookAgents are updated. |
| `hook_max_skew` | `REKUEST__HOOK_MAX_SKEW` | int | `300` | Maximum age/clock skew (seconds) for a V1-signed HookAgent request. Also the replay guard's memory: a digest is remembered for twice this. |
| `task_retention` | `REKUEST__TASK_RETENTION` | int | `0` | Seconds to keep terminal root task trees before the retention sweep deletes them; `0` disables. Deleting past runs also removes them from replay discovery (`reusableTaskFor`), so it is an explicit opt-in. Suggested production value: `2592000` (30 days). |
| `probe_ttl` | `REKUEST__PROBE_TTL` | int | `3600` | Lifetime (seconds) of a probe's redis state while it is live. |
| `probe_linger` | `REKUEST__PROBE_LINGER` | int | `300` | How long (seconds) a finished probe's state lingers so a late subscriber can still read its outcome. |
| `probe_max_inflight` | `REKUEST__PROBE_MAX_INFLIGHT` | int | `32` | Maximum concurrent probes per caller. Exceeding it refuses the probe rather than queueing it — probes are hover-grade work. |
| `sweep_interval` | `REKUEST__SWEEP_INTERVAL` | int | `5` | How often (seconds) the in-process reaper sweeps the DB-held deadlines below. Bounds how late any of them can fire. |
| `pickup_deadline` | `REKUEST__PICKUP_DEADLINE` | int | `60` | Seconds a dispatched task may go without **any** report from its live agent (or webhook endpoint) before the Assign is redelivered once, then failed `CRITICAL`; `0` disables. Physical-effect work is never redelivered. |
| `disconnected_expiry` | `REKUEST__DISCONNECTED_EXPIRY` | int | `3600` | Seconds a `DISCONNECTED` (fate unknown) task — or an undelivered task of an agent that is gone — stays recoverable before it is finalized `CRITICAL`; `0` = never. |
| `control_deadline` | `REKUEST__CONTROL_DEADLINE` | int | `60` | Seconds an unconfirmed cancel waits before escalating to an interrupt, and an unconfirmed interrupt before it is finalized; `0` disables. On by default: a Cancel/Interrupt frame lost in transit is otherwise never noticed, and nothing redelivers it the way the pickup deadline redelivers an Assign. A socket `CancelRequest.auto_interrupt` takes precedence. |

None of these is a timer. Each deadline starts at a database column and is enforced by the
reaper loop inside every backend process (`facade/reaper.py`) — there is no management command,
cron job or sidecar to run, a backend can be killed at any moment without losing a pending
deadline, and any number of backends can run side by side (every transition is a row-locked
claim with exactly one winner).

## Running more than one replica

Nothing needs to be configured to scale the service: state lives in Postgres and redis, no
request needs to return to the replica that served the last one (sticky sessions are **not**
required), and `manage.py migrate` takes a Postgres advisory lock so every replica can run it at
boot with one winner. What does need attention:

- **`redis.channel_prefix` and `redis.key_prefix`** must be unique per service, and per
  deployment if two deployments share a redis. See above.
- **Clocks.** Liveness compares one replica's clock against another's writes, so hosts must be
  NTP-synced. A replica measures itself against the database clock and, if it is off by more than
  `(AGENT_STALE_AFTER − heartbeat interval − heartbeat timeout) / 2` (7.5 s at the defaults),
  stops sweeping and reports unhealthy on `/ht` rather than deciding other replicas' agents are
  dead. See `facade/clock.py`.
- **`control_deadline`** should stay non-zero. A Cancel/Interrupt frame can be lost when a
  connection is displaced or redis restarts, and nothing redelivers it — the deadline is what
  stops the database from saying `CANCELLING` forever while the agent runs on.
- **Postgres connections.** Each replica opens its own; raise the server's `max_connections`
  before scaling a stack that shares one cluster between services.

### `provenance` — provenance (attestation) signing keypair and policy

Rekuest acts as the provenance authority: it signs an Ed25519 attestation JWT per
non-trivial assignment and publishes the verifying key at its JWKS endpoint. This
keypair is **orthogonal** to the auth keys above (different issuer, different lifetime);
the private key never leaves Rekuest.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `issuer` | `PROVENANCE__ISSUER` | str | `rekuest` | Provenance token issuer (`iss`). |
| `kid` | `PROVENANCE__KID` | str | `rekuest-prov-1` | Key id published at the JWKS endpoint. |
| `private_key` 🔒 | `PROVENANCE__PRIVATE_KEY` | str (PEM) | **required** | Ed25519 signing key. The facade refuses to start without it. |
| `public_key` | `PROVENANCE__PUBLIC_KEY` | str (PEM) | derived | Ed25519 verifying key (published via JWKS); derived from the private key when omitted. |
| `token_ttl_seconds` | `PROVENANCE__TOKEN_TTL_SECONDS` | int | `3600` | Provenance token lifetime (seconds). |
| `human_roles` | `PROVENANCE__HUMAN_ROLES` | list[str] | `[]` | Roles marking an accountable human; empty disables the human-root invariant. |
| `strict` | `PROVENANCE__STRICT` | bool | `false` | Require the human-root invariant when minting. |

### `datalayer` — S3 storage (optional)

Optional S3 configuration forwarded to the datalayer app. Omit the whole block to
disable it. When present, `access_key` and `secret_key` are required.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `access_key` 🔒 | `DATALAYER__ACCESS_KEY` | str | **required** | S3 access key. |
| `secret_key` 🔒 | `DATALAYER__SECRET_KEY` | str | **required** | S3 secret key. |
| `host` | `DATALAYER__HOST` | str | `null` | S3 endpoint host. |
| `port` | `DATALAYER__PORT` | int | `null` | S3 endpoint port. |
| `protocol` | `DATALAYER__PROTOCOL` | str | `http` | S3 endpoint protocol (`http` or `https`). |
| `region` | `DATALAYER__REGION` | str | `us-east-1` | S3 region name. |
| `media` / `zarr` / `parquet` / `bigfile` | — (use YAML) | object | `null` | Per-purpose bucket bindings, each `{ bucket: <name> }`. |

### `embeddings` — semantic search

Actions embed their name + description into a pgvector column when they are saved, using a
[model2vec](https://github.com/MinishLab/model2vec) static model that runs inside the service
process (CPU, ~1 ms per row, no extra service). The `actions(filters: { search })` argument
then matches a substring of the name **or** a description whose meaning is close to the
query, ranking substring matches first and the rest by similarity. Every key has a default;
the block may be omitted.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `enabled` | `EMBEDDINGS__ENABLED` | bool | `true` | Embed rows on save and give `search` a semantic leg. Off: `search` is substring-only and the columns stay `NULL`. |
| `model` | `EMBEDDINGS__MODEL` | str | `minishlab/potion-base-8M` | model2vec model id. Recorded on every row (`embedding_model`); rows embedded by another model are re-embedded in-process and skipped by vector search until then. |
| `model_path` | `EMBEDDINGS__MODEL_PATH` | str | `null` | Directory holding the weights of `model`. The Docker image bakes them under `/opt/models/embeddings` and sets this itself (with `HF_HUB_OFFLINE=1`); unset, model2vec downloads from Hugging Face on first use. |
| `dimensions` | `EMBEDDINGS__DIMENSIONS` | int | `256` | Vector width of `model` — and of the database column. Checked against both at startup (`embeddings.E001` / `E002`). |
| `distance_threshold` | `EMBEDDINGS__DISTANCE_THRESHOLD` | float | `0.55` | Cosine distance (0 identical, 1 unrelated) above which a row no longer counts as a semantic hit. Lower is stricter. |
| `sweep_interval` | `EMBEDDINGS__SWEEP_INTERVAL` | int | `30` | Unused by rekuest: stale rows are re-embedded on the reaper tick (`rekuest.sweep_interval`). Kept for parity with the other services' config. |
| `sweep_batch_size` | `EMBEDDINGS__SWEEP_BATCH_SIZE` | int | `200` | Rows re-embedded per batch. |

Rows that were written before embeddings were enabled, while the model could not be loaded,
or by a previous `model` are healed by the reaper loop (`facade/reaper.py`) in row-locked
batches — no command, no cron, any number of replicas. Until healed, such rows are found by
the substring leg only.

**Changing the model.** Same `dimensions`: change `model`, restart, and the reaper re-embeds
every row within a few ticks. Different `dimensions`: the column type changes, so write a
migration that first nulls the column (`UPDATE facade_action SET embedding = NULL,
embedding_model = ''` — Postgres refuses to retype non-empty vectors), then `AlterField`s it
to the new width, then change the config; the reaper refills it after boot. `migrate` refuses
to run while the column, the setting and the model disagree.

The Docker image bakes the default model; a different `model` needs a rebuild with
`--build-arg EMBEDDINGS_MODEL=<id>` (or a `model_path` of your own), because the running
image is offline.

---

## Minimal example

```yaml
django:
  secret_key: "REPLACE_ME"
  debug: false
  admin:
    username: admin
    password: "REPLACE_ME"
    email: admin@example.com
postgres:
  db_name: rekuest_db
  username: rekuest
  password: "REPLACE_ME"
  host: db
  port: 5432
redis:
  host: redis
  port: 6379
authentikate:
  # No default — omitting `audience` fails validation at startup.
  audience: "*"
  issuers:
    - kind: rsa
      iss: lok
      kid: lok-key-1
      public_key: "ssh-rsa AAAA..."
provenance:
  private_key: |
    -----BEGIN PRIVATE KEY-----
    ...
    -----END PRIVATE KEY-----
# Optional — everything defaults; shown for the one knob worth tuning.
embeddings:
  distance_threshold: 0.55
```

Validate it with `python manage.py validate_settings`.

# Rekuest — Configuration Reference

This document explains how the **rekuest** service is configured, then lists every
configuration value, its environment-variable name, its default, and what it does.

One `config.yaml` configures both programs of a deployment: the rekuest server (Python) and
takt (Rust, the agent protocol). Each reads the blocks it needs and ignores the rest.

The single source of truth for the schema is
[`rekuest/configuration.py`](rekuest/configuration.py); this file documents it for
humans. takt's reading of the same file is
[`takt/crates/rekuest-server/src/configuration.rs`](takt/crates/rekuest-server/src/configuration.rs).
If the documents and the code ever disagree, the code wins — and you can always print the
server's live, resolved configuration with `python manage.py validate_settings` (see below).

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

### One file, two programs

takt reads the same file. It looks for it at `TAKT_CONFIG`, then at
`ARKITEKT_CONFIG_FILE`, then at `config.yaml` in its working directory; the takt image sets
`TAKT_CONFIG=/workspace/config.yaml`, so mount the server's file there.

takt reads these blocks: `django` (`debug`, `force_script_name`), `postgres`, `redis`,
`authentikate`, `rekuest`, `provenance` and `instance`. The `instance` block is required: takt
refuses to start without it, because takt signs what it asks of the server (the upkeep jobs) and
of HookAgents with the instance key.

The environment overrides below apply to takt too, for keys in those seven blocks
(`POSTGRES__PASSWORD`, `REKUEST__PICKUP_DEADLINE`, …). A secret given only as an environment
variable therefore has to be set on both containers.

Three variables are takt's alone: `TAKT_INTERNAL_BIND` (where the internal API is served, apart from what agents reach: `unix:<path>` for a socket the server mounts too, or an address; default `127.0.0.1:8081`. Nothing on it is authenticated, so it must be reachable by the server alone), `TAKT_BIND` (the listen address, default `0.0.0.0:8080`)
and `RUST_LOG` (the log filter, default `info`).

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

A valid config can still say things this release does not read: a misspelt key, or a key of
another release, is not an error to the loader — the service starts, with the default. Those are
listed under the tree, and warned about at every boot (system checks `rekuest.W001`, a key no
setting claims, and `rekuest.W002`, a key still read under a former name). To ask for a verdict:

```bash
python manage.py validate_settings --strict
```

It exits `78` when the config sets a key this release does not read. A key read under a former
name is said, not failed, since a release may rename a key within its major; an invalid config
exits `1`, as it does for every command. This is what an installer asks of a release before it moves a hub to it. It looks at the
service's own blocks (`rekuest`, `provenance`, `instance`, `django`, `embeddings`); a block that
passes options on (`postgres`, `redis`, `datalayer`), `authentikate`, and top-level blocks other
services read are left alone.

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
| `debug` | `DJANGO__DEBUG` | bool | `false` | Enable Django debug mode. Never enable in production. Static tokens (`authentikate.static_tokens`) are accepted only while it is on, by the server and by takt. |
| `log_level` | `DJANGO__LOG_LEVEL` | str | `INFO` | Root logger level of the server. The `LOG_LEVEL` environment variable overrides it. |
| `enable_rich_logging` | `DJANGO__ENABLE_RICH_LOGGING` | bool | `false` | Render the server's console logs with rich. A development convenience. |
| `hosts` | `DJANGO__HOSTS` | list[str] | `["*"]` | `ALLOWED_HOSTS` entries. |
| `use_x_forwarded_host` | `DJANGO__USE_X_FORWARDED_HOST` | bool | `true` | Trust the `X-Forwarded-Host` header behind a reverse proxy. |
| `admin` | `DJANGO__ADMIN__*` | object | `null` | Superuser provisioned on first boot (see below). |
| `csrf_trusted_origins` | `DJANGO__CSRF_TRUSTED_ORIGINS` | list[str] | `["http://localhost", "https://localhost"]` | `CSRF_TRUSTED_ORIGINS` for unsafe (POST) requests. |
| `force_script_name` | `DJANGO__FORCE_SCRIPT_NAME` | str | `""` | URL path prefix this service is served under. takt serves its routes under the same prefix (`/<prefix>/agent` and its former name `/<prefix>/agi`, `/<prefix>/internal/…`, `/<prefix>/ht`). |

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
| `key_prefix` | `REDIS__KEY_PREFIX` | str | `rekuest` | Namespace for every redis key the server and takt write (agent queues, probe state, tick tokens, webhook replay guard). Two deployments sharing one redis MUST differ here, or agent 42 of one receives the other's Assigns. |
| `channel_prefix` | `REDIS__CHANNEL_PREFIX` | str | `rekuest` | Key prefix for the `channels_redis` channel layer, which the server and takt both speak. Must differ from every other service on the same redis, or group messages bleed between services. |
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

### `rekuest` — takt, deadlines, retention and probe limits

Where the server finds takt, and every window enforced over agent work: how long a lost
agent's tasks are held before they end, how long a task may go unreported, how long finished
work is kept, and the probe limits. Everything is optional.

The "Read by" column says which program acts on the key. Deadlines, retention, the trigger loop
guard and the hook signatures are takt's: changing them means restarting takt, not the
server.

| Key | Env var | Type | Default | Read by | Description |
|---|---|---|---|---|---|
| `takt_url` | `REKUEST__TAKT_URL` | str | `http://takt:8081/<script name>` | server | takt's internal listener (`TAKT_INTERNAL_BIND`) with the script name: not the address agents connect to. The server POSTs to `<takt_url>/internal/<op>` (`facade/takt.py`) and asks `<takt_url>/ht` for its own health check. While takt is unreachable every assign, control, registration, delete, probe and "run now" is refused; a created or changed schedule is still written, and takt's reaper plans it once it is back. `agentd_url`, its former name, is still read. |
| `takt_socket` | `REKUEST__TAKT_SOCKET` | str | unset | server | takt's internal listener as a unix socket the server and takt both mount (takt: `TAKT_INTERNAL_BIND=unix:<path>`). When set, the internal API is reached through it and only the path of `takt_url` is used. |
| `server_url` | `REKUEST__SERVER_URL` | str | `http://rekuest:80/<script name>` | takt | The server's base URL with the script name. takt POSTs the upkeep jobs to `<server_url>/_rekuest/upkeep/<job>` (`takt/crates/facade/src/upkeep.rs`). Empty turns upkeep off: no service is catalogued, no hook agent is provisioned and new actions get no embedding. |
| `identifier` | `REKUEST__IDENTIFIER` | str | `live.arkitekt.rekuest` | both | This rekuest's fakts identifier: what its key is listed under in the hub trust bundle, and the issuer and audience of the service tokens the server signs its internal requests with. |
| `services` | — (use YAML) | list | `[]` | both | This hub's services, each `{name, url, identifier?}`. The server catalogues what each hosts and emits from its manifest at `<url>/manifest` (when takt asks, `facade/service_catalog.py`); takt accepts their signed signals. A service is not an agent: nothing here creates one. |
| `hook_agents` | — (use YAML) | list | `[]` | both | This hub's hook agents, each `{name, hook_url, identifier?}`. The server gives every organization the agent with the actions of its manifest at `<hook_url>/manifest` (`facade/hook_agents.py`); takt signs deliveries to them and accepts their signed reports. Independent of `services`: a hook agent may run in a service's process or anywhere else. Nothing is scheduled or triggered by itself. |
| `sweep_interval` | `REKUEST__SWEEP_INTERVAL` | int | `5` | both | How often (seconds) takt's sweeps tick (deadlines, schedules, triggers, delayed tasks). Bounds how late any of them can fire. |
| `grace_default` | `REKUEST__GRACE_DEFAULT` | int | `30` | takt | Reclaim grace window (seconds) after a disconnect: how long a gone agent's running tasks wait for it before they end `LOST` (a workflow is resumed). |
| `pickup_deadline` | `REKUEST__PICKUP_DEADLINE` | int | `60` | takt | Seconds a dispatched task may go without **any** report from its live agent (or webhook endpoint) before the Assign is redelivered once, then ended `LOST` (never started); `0` disables. |
| `disconnected_expiry` | `REKUEST__DISCONNECTED_EXPIRY` | int | `3600` | takt | Seconds an undelivered task of an agent that is gone waits for it before it ends `LOST` (never started); `0` = never. |
| `control_deadline` | `REKUEST__CONTROL_DEADLINE` | int | `60` | takt | Seconds an unconfirmed cancel waits before escalating to an interrupt, and an unconfirmed interrupt before it is finalized; `0` disables. On by default: a Cancel/Interrupt frame lost in transit is otherwise never noticed, and nothing redelivers it the way the pickup deadline redelivers an Assign. A socket `CancelRequest.auto_interrupt` takes precedence. |
| `hook_signature_mode` | `REKUEST__HOOK_SIGNATURE_MODE` | str | `compat` | takt | HookAgent HTTP signatures. `compat` accepts the timestamped `X-Rekuest-Signature-V1` **or** the legacy body-only `X-Rekuest-Signature`, and sends both. `strict` accepts and sends V1 only — the legacy signature is replayable, so move to `strict` once your HookAgents are updated. |
| `hook_max_skew` | `REKUEST__HOOK_MAX_SKEW` | int | `300` | takt | Maximum age/clock skew (seconds) for a V1-signed HookAgent request. Also the replay guard's memory: a digest is remembered for twice this. |
| `trigger_max_depth` | `REKUEST__TRIGGER_MAX_DEPTH` | int | `3` | takt | How many trigger firings may chain (a triggered run creates an object whose signal fires another trigger …) before a signal stops firing. The loop guard. |
| `dependency_max_depth` | `REKUEST__DEPENDENCY_MAX_DEPTH` | int | `8` | takt | How many levels of dependencies an assign resolves below the assigned implementation. An implementation's dependencies may have dependencies of their own; the whole tree is resolved at the root assign, and a tree deeper than this is refused. |
| `signal_retention` | `REKUEST__SIGNAL_RETENTION` | int | `604800` | takt | Seconds to keep processed signals; `0` keeps them forever. Runs keep their tasks; their `signal` link turns null. |
| `ephemeral_task_retention` | `REKUEST__EPHEMERAL_TASK_RETENTION` | int | `86400` | takt | Seconds to keep terminal *ephemeral* root task trees (the runs of schedules with `ephemeralRuns`, e.g. services' housekeeping sweeps). Applies even while `task_retention` is `0`; `0` disables. |
| `task_retention` | `REKUEST__TASK_RETENTION` | int | `0` | takt | Seconds to keep terminal root task trees before the retention sweep deletes them; `0` disables. Deleting past runs also removes them from replay discovery (`reusableTaskFor`), so it is an explicit opt-in. Suggested production value: `2592000` (30 days). |
| `probe_ttl` | `REKUEST__PROBE_TTL` | int | `3600` | takt | Lifetime (seconds) of a probe's redis state while it is live. |
| `probe_linger` | `REKUEST__PROBE_LINGER` | int | `300` | takt | How long (seconds) a finished probe's state lingers so a late subscriber can still read its outcome. |
| `probe_max_inflight` | `REKUEST__PROBE_MAX_INFLIGHT` | int | `32` | both | Maximum concurrent probes per caller. takt refuses a probe beyond it rather than queueing it (probes are hover-grade work); the server reports the cap in `probeStats`. |

None of the deadlines is a timer. Each starts at a database column and is enforced by takt's
sweeps (`takt/crates/facade/src/reaper.rs`), which run inside every takt replica. A tick
token in redis lets one replica sweep per tick; every transition is a row-locked claim with
exactly one winner, so any number may run. A replica holds no state: it can be killed at any
moment without losing a pending deadline, and while none runs, deadlines are late, not lost.

The sweeps, in the order a tick runs them: stale agents, disconnected agents, schedules (each
enabled schedule gets its next run), triggers (unprocessed signals are matched and fired), due
tasks (`not_before` has passed), unpicked tasks, due controls, expired tasks. Retention (task
trees and processed signals) runs on every 60th tick.

Agent heartbeats are not configuration: takt pings every 10 s, waits 5 s for the answer and
presumes a `connected` agent dead after 30 s without one
(`takt/crates/rekuest-server/src/settings.rs`).

The server runs no loop of its own. Two jobs need it — provisioning (the `services` catalog and the
`hook_agents`, both written through its models) and embedding the actions takt registered (only
its image carries the model) — and takt asks for each when it is due, at
`POST <server_url>/_rekuest/upkeep/{provision,reembed}`, signed with the instance key:
provisioning at start and every 5 minutes (30 s after a failed pass), embedding every 30 s.
`_rekuest` paths must not be routed at the edge. The server's `ht` answers for takt as well
(it asks takt's `ht`), so one health check covers the pair; takt's own is `takt healthcheck`,
which the takt image runs as its `HEALTHCHECK`.

#### Schedules

The server owns the schedule rows (GraphQL create, update, delete). takt is the only reader
of cron lines: the server asks it to validate a timing, and takt plans each schedule's next
run and handles "run now" (`triggerSchedule`).

- A cron line has five fields (minute, hour, day of month, month, day of week) and is read in
  the schedule's time zone. A six-field line is refused.
- A range that wraps (`5-1`) is refused.
- Across a daylight-saving change, a slot in the skipped hour runs at the first instant after
  it, and a fixed-time job in the repeated hour of a fall-back night runs once.

## Running more than one replica

Nothing needs to be configured to scale either program: state lives in Postgres and redis, no
request needs to return to the replica that served the last one (sticky sessions are **not**
required), and `manage.py migrate` takes a Postgres advisory lock so every server replica can
run it at boot with one winner. takt replicas all serve `/agent` and all run the sweeps; one of them asks for each upkeep job when it is due. What
does need attention:

- **`redis.channel_prefix` and `redis.key_prefix`** must be unique per service, and per
  deployment if two deployments share a redis. See above.
- **Clocks.** Liveness compares one takt replica's clock against another's writes, so hosts
  must be NTP-synced. An takt replica measures itself against the database clock and, if it is
  off by more than `(stale window − heartbeat interval − heartbeat timeout) / 2` (7.5 s), skips
  its sweeps and logs an error rather than deciding other replicas' agents are dead. See
  `takt/crates/facade/src/clock.rs`.
- **`control_deadline`** should stay non-zero. A Cancel/Interrupt frame can be lost when a
  connection is displaced or redis restarts, and nothing redelivers it — the deadline is what
  stops the database from saying `CANCELLING` forever while the agent runs on.
- **Postgres connections.** Each replica opens its own (an takt replica holds a pool of up to
  32); raise Postgres's `max_connections` before scaling a stack that shares one cluster between
  services.
- **The same version everywhere.** Server and takt replicas must come from the same release:
  takt's SQL is written against that release's migrations.

### `instance` — this instance's key and whom it trusts

One Ed25519 key per rekuest instance. It signs provenance tokens, takt's upkeep requests to
the server, and every request to the hub's services. The server and takt must hold
the same key, which they do by reading the same file. Required by both.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `private_key` 🔒 | `INSTANCE__PRIVATE_KEY` | str (PEM) | **required** | Ed25519 private key (PKCS#8 PEM). Its key id (`kid`) is its RFC 7638 thumbprint, which is what the hub's trust bundle lists it under. |
| `trust.jwks_uri` | `INSTANCE__TRUST__JWKS_URI` | str | `null` | Where the hub's instance public keys are fetched from (the coord's hub-keys URL). |
| `trust.jwks` | — (use YAML) | object | `null` | The trust bundle inline (a JWKS whose keys carry `service`), for a hub not enrolled yet. |

### `provenance` — provenance (attestation) policy

Rekuest acts as the provenance authority: takt signs an Ed25519 attestation JWT per
non-trivial assignment with the instance key (`instance.private_key`), and the server publishes
the verifying key at `/.well-known/jwks.json`. The key is **orthogonal** to the auth keys above
(different issuer, different lifetime); the private key never leaves Rekuest.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `issuer` | `PROVENANCE__ISSUER` | str | `rekuest` | Provenance token issuer (`iss`). |
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
| `sweep_interval` | `EMBEDDINGS__SWEEP_INTERVAL` | int | `30` | Unused by rekuest: stale rows are re-embedded when takt asks (every 30 s). Kept for parity with the other services' config. |
| `sweep_batch_size` | `EMBEDDINGS__SWEEP_BATCH_SIZE` | int | `200` | Rows re-embedded per batch. |

Rows that were written before embeddings were enabled, while the model could not be loaded,
or by a previous `model` are healed by the `reembed` upkeep job (`facade/upkeep.py`, asked for by takt) in row-locked
batches — no command, no cron, any number of replicas. Until healed, such rows are found by
the substring leg only.

**Changing the model.** Same `dimensions`: change `model`, restart, and the upkeep job re-embeds
every row within a few passes. Different `dimensions`: the column type changes, so write a
migration that first nulls the column (`UPDATE facade_action SET embedding = NULL,
embedding_model = ''` — Postgres refuses to retype non-empty vectors), then `AlterField`s it
to the new width, then change the config; the upkeep job refills it after boot. `migrate` refuses
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
# Required, even when empty: the provenance policy (every key has a default).
provenance: {}
# This instance's Ed25519 key. The server and takt both read it.
instance:
  private_key: |
    -----BEGIN PRIVATE KEY-----
    ...
    -----END PRIVATE KEY-----
rekuest:
  # Only where the pair is not named `rekuest` and `takt` (with django.force_script_name
  # appended if one is set):
  # takt_url: http://takt:8081
  # takt_socket: /run/takt/internal.sock
  # server_url: http://rekuest:80
# Optional — everything defaults; shown for the one knob worth tuning.
embeddings:
  distance_threshold: 0.55
```

Validate it with `python manage.py validate_settings`. Mount the same file into the takt
container at `/workspace/config.yaml`.

# rekuest-takt

The rekuest agent protocol server, in Rust. It lives in the `takt/` directory of the rekuest
server's repository and is released with it, under the same version, as the
`jhnnsrs/rekuest-takt` image. Always run it beside the rekuest server of the same version.

## What it owns

The whole agent protocol:

- the agent websocket `/agi`, the HookAgent HTTP intake `/agi/http/{agent_id}` and the signal
  intake `/agi/signal/{service}`;
- registration: an agent's `REGISTER` carries its declaration, and takt reconciles its
  implementations, states, locks and bloks;
- assign, cancel, interrupt, pause, resume, and probes;
- deleting agents and implementations, with their cascade;
- every sweep: stale and disconnected agents, schedules, triggers, due tasks, redelivery of
  unpicked tasks, control escalation, expiry, workflow resume, and retention.

The rekuest server (Python, the repository root) keeps GraphQL, its subscriptions, the catalogue
CRUD and the database migrations. It has no agent route and no agent code. Whatever a GraphQL
mutation needs done to a task or an agent, it asks takt for through the internal API.

## Why a separate program

An agent's reports and heartbeat answers must not wait behind GraphQL work. With GraphQL, every
agent socket and every subscription in one Python process, they did: under UI load agents were
kicked (heartbeat 3001), and a thread per connection did not help (the limit is the GIL).
Measured with the conformance benchmark (one agent streaming a report every 50 ms, two workers
querying GraphQL at the rate):

| GraphQL load | agent protocol in the Python process | takt (GraphQL in the Python process) |
|---|---|---|
| none | 19.5 reports/s, ack lag p50 0.22 s | 19.9 reports/s, p50 0.14 s |
| 10 queries/s × 2 | 9.2 reports/s, ack lag p50 5.3 s, **kicked (3001)** | 19.4 reports/s, p50 0.13 s |
| 26 queries/s × 2 | (the agent was already gone) | 19.2 reports/s, p50 0.12 s, no kicks |

## Shape

```
agents ──ws /agi──────────────┐
hook agents ──POST /agi/http──┤
hub services ─POST /agi/signal┤
                              ▼
                   takt (N replicas) ◀── POST /internal/<op> ── rekuest server (GraphQL)
                              │                                        │
                              └──────── Postgres · Redis ──────────────┘
```

Postgres holds the schema the rekuest server migrates; takt never migrates. Redis holds the
agent queues, probe state and the `channels_redis` groups both programs publish on.

A gateway routes `<script name>/agent*` and `<script name>/agi*` to takt and everything else to the server. Hub services
POST their HookAgent reports and signals to takt, so their `rekuest_url` points here.

## Configuration

takt reads the rekuest server's `config.yaml`
(`crates/rekuest-server/src/configuration.rs`); every key is documented in
[`../CONFIG.md`](../CONFIG.md).

- **Which file:** `TAKT_CONFIG`, else `ARKITEKT_CONFIG_FILE`, else `config.yaml` in the working
  directory. The image sets `TAKT_CONFIG=/workspace/config.yaml`.
- **Environment overrides:** `SECTION__KEY` variables go over the file, as they do for the
  server (`POSTGRES__PASSWORD`, `REKUEST__PICKUP_DEADLINE`, `DJANGO__DEBUG`). They apply to the
  blocks takt reads: `django`, `postgres`, `redis`, `authentikate`, `rekuest`, `provenance`,
  `instance`.
- **The `instance` block is required.** takt refuses to start without it: the server signs
  every internal request with the instance key, and takt verifies with the same key.
- **`TAKT_BIND`** is the listen address (default `0.0.0.0:8080`). Routes are served under the
  config's `django.force_script_name`.
- **`RUST_LOG`** sets the log filter (default `info`).

The deadlines (`grace_default`, `pickup_deadline`, `disconnected_expiry`, `control_deadline`),
the retention horizons, `trigger_max_depth`, the probe limits and the hook signature settings in
the `rekuest` block are read here, not by the server.

## Routes

All under the script-name prefix (`crates/rekuest-server/src/urls.rs`):

| Route | |
|---|---|
| `GET /agent` | the agent websocket |
| `POST /agent/http/{agent_id}` | the HookAgent intake: the frames a socket agent would send, signed |
| `POST /agent/signal/{service}` | a hub service's signal |
| `/agi`, `/agi/http/…`, `/agi/signal/…` | the same three under their former name, which released agents and services ask for |
| `POST /internal/<op>` | the internal API |
| `GET /ht` | 200 when Postgres and Redis answer |

`takt healthcheck` requests `/ht` on the local port and exits non-zero unless it answers 200.
The image uses it as its `HEALTHCHECK`, since it carries no HTTP client.

## The internal API

The rekuest server calls `POST {prefix}/internal/<op>` with a JSON body (`facade/takt.py` on
the server's side, `rekuest.takt_url` in its configuration). The route table, the request and
answer shapes and the authentication are documented at the top of
`crates/rekuest-server/src/internal.rs`.

Requests are signed with the instance key: a service token (`Authorization: RekuestService
<jwt>`) whose issuer and audience are both `rekuest.identifier`, bound to the method, the full
path and the body, valid for 60 seconds and accepted once. A refused operation answers `400` or
`403` with a message, which the server raises as the GraphQL error.

Nothing here is public API: only the rekuest server beside takt may call it.

## The schema contract

The rekuest server owns the tables and migrates them. takt reads and writes them with its own
SQL, bypassing Django. Three things keep the two in step:

- **Defaults are in the database.** Every defaulted column has a `db_default`, so an insert that
  does not name the column is still valid. The server's `tests/models/test_database_defaults.py`
  enforces it.
- **`schema-migrations.txt`** lists the migrations takt's SQL was written against. The server's
  `tests/test_takt_contract.py` fails when a new migration is not listed, which is the prompt
  to check takt's SQL against it.
- **takt waits at startup** until the database has every listed migration
  (`facade::schema::wait_until_migrated`), logging what is missing. A database ahead of the list
  is served.

Django emulates `on_delete` in Python, so takt writes the delete walk out itself
(`crates/facade/src/deletion.rs`).

## Sweeps

`facade::reaper` runs inside every takt replica, every `rekuest.sweep_interval` seconds. A tick
token in redis lets one replica sweep per tick; every transition is a row-locked claim with one
winner, so any number may run, and a replica may die at any instant without losing a deadline.

In tick order: stale agents, disconnected agents, schedules, triggers, due tasks, unpicked
tasks, due controls, expired tasks. Retention (terminal task trees and processed signals) runs
on every 60th tick. A replica whose clock is off the database's by more than the tolerated skew
skips its sweeps (`facade::clock`).

**Schedules.** The server owns the schedule rows; takt is the only reader of cron lines. It
validates a timing for the server, gives every enabled schedule its next run as a delayed task,
and handles "run now". A cron line has five fields and is read in the schedule's time zone. A
fixed-time job in the repeated hour of a fall-back night runs once. Six-field lines and wrapping
ranges such as `5-1` are refused (`facade::timing`).

**Triggers.** Unprocessed signals are matched against triggers and the matching triggers'
actions are assigned (`facade::triggers`). `rekuest.trigger_max_depth` bounds chains.


## Upkeep

Two periodic jobs need the Python server, which runs no loop: provisioning this hub's services
(`rekuest.service_agents`) and embedding the actions takt registered (only the server's image
carries the model). `facade::upkeep` asks for each when it is due, with
`POST <rekuest.server_url>/_rekuest/upkeep/<job>`, signed with the instance key: `provision` at
start and every 5 minutes (30 s after a failed pass), `reembed` every 30 s and again at once
while the server has more. When a job is next due is a redis key that expires then, so any
number of replicas may run and none holds a deadline of its own.

## Layout

A Cargo workspace. `rekuest` is the client's name (the Python SDK, and arkirust's crate), so the
server's own crates are named `rekuest-server-…`. The crates shared by every Arkitekt server
keep the names of their Python counterparts (`authentikate`, `kante`).

| Package (library) | Scope |
|---|---|
| `rekuest-server` (`rekuest_server`) | `configuration`, `settings`, `urls`, `internal`; the `takt` binary |
| `rekuest-server-facade` (`facade`) | the agent protocol: `consumers` (the socket and the agent queue), `persist`, `backend`, `registration`, `message_router`, `http_intake`, `signal_intake`, `reaper`, `schedules`, `triggers`, `retention`, `removal`, `provenance`, `probes`, … |
| `rekuest-server-core` (`rekuest_core`) | the declaration models and their validation, matching the server's `rekuest_core/` |
| `authentikate` | token verification (rsa, rsa_file, jwks_dict, jwks_uri; revocation lists), static tokens, and expansion to user/org/client/membership rows |
| `kante` | the channel layer, wire-compatible with `channels_redis`: groups, sends, receives; the change fan-out (`facade::signals`) runs on it |

## Design documents

The protocol's design documents live in [`docs/`](docs/): [agent protocol](docs/agent-protocol.md),
[caller protocol](docs/caller-protocol.md), [task lifecycle](docs/task-lifecycle.md),
[journal](docs/journal.md), [workflows](docs/workflows.md), [provenance](docs/provenance.md).
The server's side (identity, the data model, action matching, the realtime layer) is in
[`../docs/design/`](../docs/design/README.md).

## The contract

- **Wire:** the frames are the types of the
  [`rekuest-protocol`](https://github.com/arkitektio/arkirust/tree/main/crates/rekuest-protocol)
  crate, shared with the agents. That crate tests them against its own fixture of example
  frames (`tests/fixtures/agent_wire_examples.json`). Nothing in this repository generates that
  file: a change to the wire is a change to that crate and its fixture.
- **Declarations:** `crates/rekuest-server-core/tests/fixtures/` holds what the server's own
  input models make of real declarations (`declarations.json`, `units.json`). The generators
  beside them run with the server's venv, and CI fails when the committed fixtures differ from
  what the checkout's models produce.
- **Registration:** `crates/facade/tests/registration_snapshot.rs` checks what a registration
  writes, row by row, against a recorded snapshot. The fan-out payloads are checked against the
  server's own builders (`signals_contract.rs`).
- **Behaviour:** `conformance/`, a black-box pytest suite of real sockets against the pair.

## Development

Run from `takt/`.

```bash
eval "$(scripts/test-db.sh)"   # Postgres and redis, the schema migrated by this checkout's server
cargo test
cargo clippy --all-targets -- -D warnings
scripts/test-db.sh down
```

`scripts/test-db.sh` builds the rekuest server from this checkout, lets it migrate, and exports
`TAKT_TEST_DATABASE_URL` and `TAKT_TEST_REDIS_URL`. Tests that need a database or redis are
skipped without them.

The conformance suite:

```bash
cd conformance
uv run pytest                 # builds both images from this checkout, brings up stack/ with dokker
uv run pytest -m "not slow"   # skip the tests that wait out a protocol timer
# an already running pair: takt, and the server's GraphQL
CONFORMANCE_URL=http://127.0.0.1:8480 CONFORMANCE_GRAPHQL_URL=http://localhost:5690/graphql uv run pytest
# hook scenarios: takt POSTs to a receiver in the test process; name an address it reaches
CONFORMANCE_HOOK_HOST=172.23.0.1 CONFORMANCE_URL=… uv run pytest tests/test_hooks.py
# the liveness benchmark (skipped unless BENCH_SECONDS is set):
BENCH_SECONDS=30 BENCH_RATES=0,10,26 BENCH_CONCURRENCY=2 CONFORMANCE_URL=… uv run pytest -s tests/test_zz_bench_liveness.py
```

CI (`.github/workflows/takt.yaml` at the repository root) runs all of it on every push and
pull request: the Rust workspace against a database migrated by the checkout, a contract job
that regenerates the fixtures from the server's models, and the conformance suite against both
images built from the checkout.

To build the image: `docker build -t jhnnsrs/rekuest-takt takt` from the repository root.
The release workflow (`.github/workflows/release.yaml`) pushes it with the server's image under
the same tags.

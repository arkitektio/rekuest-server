# rekuest-server

The task service of an [Arkitekt](https://arkitekt.live) hub. Rekuest is the central
repository of the connected apps and the functionality they provide, their
[actions](https://arkitekt.live/docs/terminology/actions). Apps and users assign tasks to
it; rekuest routes each one to an app that implements the action, and routes the result back
to the caller. It is registered as `live.arkitekt.rekuest` and has a python client,
[`rekuest`](https://github.com/arkitektio/rekuest).

## Design

Rekuest itself is designed as a stateless service (in order to be able to scale horizontally), and
interfaces with proven open-source technologies, such as [Redis](https://redis.io/) and
[PostgreSQL](https://www.postgresql.org/), to route tasks to the appropriate apps. The following
diagram shows the high-level design of Rekuest:

![Rekuest Design](./docs/schema.png)

> **📐 Architecture documentation.** For a structured, in-depth explanation of the major elements of
> the service — the Caller/Agent identity model, the relational action-matching engine, the
> realtime layer and higher-order implementations — see **[`docs/design/`](./docs/design/README.md)**.
> The agent protocol, the task lifecycle and workflows are documented with their implementation,
> in [`takt/docs/`](./takt/docs/).

## Two programs, released together

This repository contains two programs. They share one database, one redis and one `config.yaml`,
and are released under the same version.

| program | where | what it does |
|---|---|---|
| **the rekuest server** (Python/Django) | the repository root | GraphQL (queries, mutations, subscriptions), the database migrations, and the two upkeep jobs takt asks it for (service agents, embeddings). |
| **takt** (Rust) | [`takt/`](./takt/README.md) | The whole agent protocol: the agent websocket `/agent`, the HookAgent intake `/agent/http/{agent_id}`, the signal intake `/agent/signal/{service}` (each also under its former name, `/agi`), registration, assign and control, probes, and every sweep (stale agents, deadlines, workflow resume, schedules, triggers, retention). |

The server has no websocket route for agents and no agent code: `rekuest/asgi.py` serves GraphQL
only. Whatever a mutation needs done to a task or an agent, the server asks takt for, through
takt's internal API (`facade/takt.py`), on a listener only the server reaches.

The server owns the schema and its migrations. takt writes the same tables with its own SQL, never
migrates, and waits at startup until the database has the migrations listed in
`takt/schema-migrations.txt`.

## Running it: one step, two processes

| process | command | role |
|---|---|---|
| migrate (once per release, before anything starts) | `python -m arkitekt_service migrate` | Waits for the database and applies the migrations, under an advisory lock. |
| web (any number of replicas) | `bash run.sh` (daphne) | Serves GraphQL and its subscriptions, and nothing else. Runs no loop: takt asks it to provision this hub's services and to embed new actions when those are due. Its health check (`ht`) answers for takt too. |
| takt (any number of replicas) | the `jhnnsrs/rekuest-takt` image, same `config.yaml` | The agent protocol (`agent`, formerly `agi`), every sweep, and the clock of the server's upkeep jobs. Healthcheck: `takt healthcheck`. |

The migrate step and the web process run from the `jhnnsrs/rekuest` image, which has no
default command. Always run the same version of both images. `run-debug.sh` migrates and
serves in one go with Django's autoreloading server, for development.

To run the pair locally from this checkout:

```bash
docker compose up --build
```

[`docker-compose.yaml`](./docker-compose.yaml) starts Postgres, redis, the server
(`http://localhost:8234/graphql`) and takt (`ws://localhost:8235/agent`), both
reading [`config.yaml`](./config.yaml). Tokens are verified against the issuers in that file's
`authentikate` block.

What a deployment has to get right:

- **The pair finds each other by name.** The server reaches takt at `rekuest.takt_url`
  (its internal listener: default `http://takt:8081/<script name>`, or the socket named by `rekuest.takt_socket`), takt reaches the server at `rekuest.server_url`
  (default `http://rekuest:80/<script name>`). Set them where the services are named
  otherwise; while takt is unreachable every assign, control, registration, delete and probe
  is refused and the server's `ht` is unhealthy.
- **The gateway** routes `/<script name>/agent*` and `/<script name>/agi*` to takt and
  everything else to the server, except `/<script name>/_rekuest*`, which it must not route.
- **Hub services** POST their HookAgent reports and signals to takt, so their `rekuest_url`
  points at takt, not at the server.
- **Both read the same `config.yaml`.** takt honours the same `SECTION__KEY` environment
  overrides and `ARKITEKT_CONFIG_FILE` as the server. See [CONFIG.md](./CONFIG.md).

The server holds no state and runs no loop. Every takt replica runs the sweeps and asks the
server for its upkeep jobs; a tick token in redis lets one of them sweep per tick. While no takt runs,
deadlines, schedules and triggers are late, never lost.

**Transport is Redis.** Agent commands travel through a per-agent Redis list that takt drains
(chosen over the Channels layer so a message pushed while an agent is briefly offline survives
its reconnect), and GraphQL subscriptions fan out through `channels_redis`, which takt speaks
too. There is no RabbitMQ and no Kafka; see
[Why Not?](https://arkitekt.live/docs/design/why-not) for the reasoning.

## API

GraphQL is served at `/graphql` (HTTP and WebSocket), with the SDL at `/schema`. The schema
is committed as [`schema.graphql`](./schema.graphql). The agent protocol is takt's and is
documented in [`takt/docs/`](./takt/docs/).

## Hub integration

Declared in [`rekuest/contract.py`](./rekuest/contract.py):

- **Scopes**: `rekuest_agent`, `rekuest_call`, `read`, `write`.
- **Roles**: `agent`, `caller`, `admin`.
- **Needs**: takt beside it, an instance key, `media` storage, tokens issued by lok.

Other hub services register with rekuest as a *service* (the structures they host and the
signals they emit) and, separately, as a *hook agent* (the actions they offer). Both lists
are part of the configuration (`rekuest.services`, `rekuest.hook_agents`).

## Configuration

Both programs read `config.yaml`, or the file named by `ARKITEKT_CONFIG_FILE`; any value can
be overridden by an environment variable (`POSTGRES__HOST`). `python manage.py
validate_settings` prints the configuration as the server reads it, with secrets redacted.

See [CONFIG.md](./CONFIG.md) for every value.

## Development

```sh
uv sync
uv run pytest
```

The suite runs against a real stack, brought up by [dokker](https://github.com/jhnnsrs/dokker)
from `tests/integration/docker-compose.yaml`: Postgres with pgvector (`jhnnsrs/daten:next`,
override with `DATEN_IMAGE`), Redis, and a real takt built from `./takt` (or the image named
by `TAKT_IMAGE`). It needs a running Docker daemon, and the first run builds the Rust image.
takt's own tests are described in [`takt/README.md`](./takt/README.md).

## Releases

Releases are tags: a push to `main` cuts a stable version, a push to `next` a release
candidate. Each one publishes `jhnnsrs/rekuest` and `jhnnsrs/rekuest-takt` under the same
version (`X.Y.Z`, `X.Y`, `X`), plus `latest` from `main` and `next` from `next`. The
`version` in `pyproject.toml` is a placeholder. [RELEASING.md](./RELEASING.md) has the
rules, including what counts as a breaking change. Release notes are on
[GitHub Releases](https://github.com/arkitektio/rekuest-server/releases); `CHANGELOG.md` is
frozen.

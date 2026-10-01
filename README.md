# Rekuest-Server (Next)

[![Maintenance](https://img.shields.io/badge/Maintained%3F-yes-green.svg)](https://github.com/arkitektio/rekuest-server/)
![Maintainer](https://img.shields.io/badge/maintainer-jhnnsrs-blue)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)


Rekuest is one of the core services of Arkitekt. It represents a central repository of
all the connected apps and their provided functionality, their [Actions](https://arkitekt.live/docs/terminology/actions).
It also provides ways of interacting with the apps, by providing a central access point, that
apps and users can assign tasks to. Rekuest then takes care of routing the requests to the
appropriate app, which executres the task and returns the result to rekuest, which in turn routes it back
to the caller. Similar to all other Arkitekt ervices, Rekuest exposes a GraphQL API, that can be used to interact with it.
You can find the interactive documentation for the API [here](https://arkitekt.live/explorer).

> [!NOTE]  
> What you are currently looking at is the next version of rekuest. It is currently under development and not ready for production. If you are looking for the current version of Rekeust, you can find it [here](https://github.com/arkitektio/rekuest-server).



## Rekuest Design

Rekuest itself is designed as a stateless service (in order to be able to scale horizontally), and
interfaces with proven open-source technologies, such as [Redis](https://redis.io/) and
[PostgreSQL](https://www.postgresql.org/), to route tasks to the appropriate apps. The following
diagram shows the high-level design of Rekuest:

![Rekuest Design](./docs/schema.png)

> **📐 Architecture documentation.** For a structured, in-depth explanation of the major elements of
> the service — the Caller/Agent identity model, the relational action-matching engine, the
> realtime layer and higher-order implementations — see **[`docs/design/`](./docs/design/README.md)**.
> The agent protocol, the task lifecycle and workflows are documented with their implementation,
> in [`agentd/docs/`](./agentd/docs/).

## Two programs, released together

This repository contains two programs. They share one database, one redis and one `config.yaml`,
and are released under the same version.

| program | where | what it does |
|---|---|---|
| **the rekuest server** (Python/Django) | the repository root | GraphQL (queries, mutations, subscriptions), the database migrations, and a small background loop. |
| **agentd** (Rust) | [`agentd/`](./agentd/README.md) | The whole agent protocol: the agent websocket `/agi`, the HookAgent intake `/agi/http/{agent_id}`, the signal intake `/agi/signal/{service}`, registration, assign and control, probes, and every sweep (stale agents, deadlines, workflow resume, schedules, triggers, retention). |

The server has no websocket route for agents and no agent code: `rekuest/asgi.py` serves GraphQL
only. Whatever a mutation needs done to a task or an agent, the server asks agentd for, through
agentd's internal API (`facade/agentd.py`, signed with the instance key).

The server owns the schema and migrates it. agentd writes the same tables with its own SQL, never
migrates, and waits at startup until the database has the migrations listed in
`agentd/schema-migrations.txt`.

## Running it: three processes

| process | command | role |
|---|---|---|
| web (any number of replicas) | `bash run.sh` (daphne) | Migrates on boot, then serves GraphQL and its subscriptions. |
| background loop (one is enough) | `bash run-reaper.sh` → `python manage.py reaper` | Provisions this hub's services as HookAgents and re-embeds actions whose embedding is stale. Healthcheck: `python manage.py reaper --check`. |
| agentd (any number of replicas) | the `jhnnsrs/rekuest-agentd` image, same `config.yaml` | The agent protocol and every sweep. Healthcheck: `agentd healthcheck`. |

The first two run from the `jhnnsrs/rekuest` image. Always run the same version of both images.

To run the pair locally from this checkout:

```bash
docker compose up --build
```

[`docker-compose.yaml`](./docker-compose.yaml) starts Postgres, redis, the server
(`http://localhost:8234/graphql`), the background loop and agentd (`ws://localhost:8235/agi`), all
reading [`config.yaml`](./config.yaml). Tokens are verified against the issuers in that file's
`authentikate` block.

What a deployment has to get right:

- **`rekuest.agentd_url`** must be set in `config.yaml` (or as `REKUEST__AGENTD_URL`): where the
  server reaches agentd, with the script name, e.g. `http://agentd:8080/rekuest`. Without it every
  assign, control, registration, delete and probe is refused.
- **The gateway** routes `/<script name>/agi*` to agentd and everything else to the server.
- **Hub services** POST their HookAgent reports and signals to agentd, so their `rekuest_url`
  points at agentd, not at the server.
- **Both read the same `config.yaml`.** agentd honours the same `SECTION__KEY` environment
  overrides and `ARKITEKT_CONFIG_FILE` as the server. See [CONFIG.md](./CONFIG.md).

The background loop holds no state and may run more than once side by side. Every agentd replica
runs the sweeps; a tick token in redis lets one of them sweep per tick. While no agentd runs,
deadlines, schedules and triggers are late, never lost.

## Developmental Notices

Transport is Redis: agent commands travel through a per-agent Redis list that agentd drains (chosen
over the Channels layer so a message pushed while an agent is briefly offline survives its
reconnect), and GraphQL subscriptions fan out through `channels_redis`, which agentd speaks too. There is no RabbitMQ and no Kafka. To learn more
about this design decision, please refer to the
[Why Not?](https://arkitekt.live/docs/design/why-not) section.

You can find the current developmental action of Rekuest [here](https://github.com/arkitektio/rekuest-server-next)
Efforts from this new repository will be merged into this repository once the new version is ready for production.



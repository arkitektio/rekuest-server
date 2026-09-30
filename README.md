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
> in [rekuest-agentd](https://github.com/arkitektio/rekuest-agentd/tree/main/docs).

## Running it: three processes

A rekuest deployment is **two processes from this image, plus agentd**:

| process | command | role |
|---|---|---|
| web (any number of replicas) | `bash run.sh` (daphne) | GraphQL and its subscriptions. Everything that writes task state or registrations goes to agentd. |
| scheduler (one is enough) | `bash run-reaper.sh` → `python manage.py reaper` | Service-agent provisioning, schedules, triggers, embeddings, retention. Healthcheck: `python manage.py reaper --check`. |
| [agentd](https://github.com/arkitektio/rekuest-agentd) (any number of replicas) | the `rekuest-agentd` image, same `config.yaml` | The agent protocol: `/agi` sockets, HookAgent and signal intakes, assign/control, registration, the agent sweeps. Set `rekuest.agentd_url` so this server reaches its internal API. |

> [!IMPORTANT]
> Route `/<script name>/agi*` to agentd, not to the web replicas: the agent protocol is only served
> there. The scheduler holds no state and may run more than once side by side; while none runs,
> schedules and triggers are late, never lost.

## Developmental Notices

Transport is Redis: agent commands travel through a per-agent Redis list that agentd drains (chosen
over the Channels layer so a message pushed while an agent is briefly offline survives its
reconnect), and GraphQL subscriptions fan out through `channels_redis`, which agentd speaks too. There is no RabbitMQ and no Kafka. To learn more
about this design decision, please refer to the
[Why Not?](https://arkitekt.live/docs/design/why-not) section.

You can find the current developmental action of Rekuest [here](https://github.com/arkitektio/rekuest-server-next)
Efforts from this new repository will be merged into this repository once the new version is ready for production.



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
> task lifecycle, the agent WebSocket protocol, the realtime layer, and higher-order
> implementations — see **[`docs/design/`](./docs/design/README.md)**.

## Developmental Notices

Transport is Redis: agent commands travel through a per-agent Redis list (chosen over the Channels
layer so a message pushed while an agent is briefly offline survives its reconnect), and GraphQL
subscriptions fan out through `channels_redis`. There is no RabbitMQ and no Kafka. To learn more
about this design decision, please refer to the
[Why Not?](https://arkitekt.live/docs/design/why-not) section.

You can find the current developmental action of Rekuest [here](https://github.com/arkitektio/rekuest-server-next)
Efforts from this new repository will be merged into this repository once the new version is ready for production.



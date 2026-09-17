# Rekuest — Design Documentation

This folder is the **authoritative architecture reference** for the `rekuest` service. It explains
*why* the major elements exist and *how* a request flows end-to-end, rather than just listing the
GraphQL surface (see [`../API_DOCUMENTATION.md`](../API_DOCUMENTATION.md) for that) or the dev
workflow (see [`../DEVELOPMENT.md`](../DEVELOPMENT.md)).

> These documents describe the code as it stands today. Where a name recently changed (e.g.
> `Registry` → `Caller`), a short historical note is included so older code still reads sensibly.

## What is Rekuest?

Rekuest is the broker at the centre of the [Arkitekt](https://arkitekt.live) ecosystem. It is a
**GraphQL + WebSocket facade** that mediates between two kinds of participants:

- **Callers** — users and frontend apps that *request* work ("run this action with these args").
- **Agents** — connected runtimes that *provide* implementations and actually *execute* the work.

A caller never talks to an agent directly. A **user** issues an `assign` over GraphQL — the only way
to originate a **root** task, because roots must trace to an accountable human. An **agent** may
assign *dependent* work beneath a task it is running, over the same `/agi` WebSocket it registered on
(`AssignRequest`) or via an HMAC-signed HTTP POST for server-to-server agents. Rekuest resolves which implementation/agent should run it, records an `Task`, pushes
the work to the agent over a WebSocket, persists the events the agent streams back, and re-broadcasts
them to the caller — over a GraphQL subscription **or** as `…Event` mirrors on the same socket.
Rekuest owns the **catalogue** (which actions exist, who can run them, with what data types) and the
**routing + bookkeeping**; it does not run user code itself.

```mermaid
flowchart LR
    subgraph Clients
        FE["Frontend / user<br/>(Caller)"]
        AG["App runtime<br/>(Agent)"]
    end

    subgraph Rekuest
        direction TB
        GQL["GraphQL HTTP<br/>(kante + strawberry)"]
        WS["WebSocket /agi<br/>(AgentConsumer)"]
        BE["Backend orchestration<br/>(backend.py / persist_backend.py)"]
        SIG["Signals + channels<br/>(realtime fan-out)"]
    end

    PG[("PostgreSQL<br/>catalogue + log")]
    RD[("Redis<br/>channel layer + agent queue")]

    FE -- "queries / mutations" --> GQL
    FE -- "subscriptions" --> GQL
    AG -- "register / events" --> WS
    GQL --> BE
    WS --> BE
    BE --> PG
    BE -- "push work" --> RD
    RD -- "deliver" --> WS
    BE --> SIG
    SIG --> RD
    RD --> GQL
```

## How the service boots

`rekuest/asgi.py` assembles one ASGI application via kante's `router`:

- **HTTP GraphQL** is served from `facade.schema.schema` (a `kante.Schema` with `Query` /
  `Mutation` / `Subscription` roots) at the `schema` path.
- **WebSockets** are routed by `re_dynamicpath(r"agi", AgentConsumer.as_asgi())` — every agent
  connection lands on `facade.consumers.async_consumer.AgentConsumer`.

Configuration is loaded from `config.yaml` via OmegaConf in `rekuest/settings.py`. The settings
that shape runtime behaviour the most:

| Setting | Role |
| --- | --- |
| `AGENT_HEARTBEAT_INTERVAL` | How often the server pings a connected agent. |
| `AGENT_HEARTBEAT_RESPONSE_TIMEOUT` | How long the agent has to answer a ping before it is closed. |
| `AGENT_STALE_AFTER` | Lease window: how long without a heartbeat before a `connected` agent is presumed dead. Defaults to `3 × AGENT_HEARTBEAT_INTERVAL`. |
| `AGENT_REDIS_HOST` / `AGENT_REDIS_PORT` | Redis used by the hand-rolled agent message queue. |
| Channel layer (`channels_redis`) | Redis backing the realtime subscription fan-out. |

Persistence is PostgreSQL — the relational port-matching engine relies on Postgres-specific
features (`jsonb_path_match`, JSONPath), so Postgres is required in any environment that exercises
action matching.

## Reading order

Start at the top and follow the flow of a request:

1. **[identity.md](identity.md)** — the `(client, user, organization)` triple, and the two
   identities it powers: **Caller** (who asks) and **Agent** (who provides). Read this first;
   everything else references it.
2. **[domain-model.md](domain-model.md)** — the full data model with an ER diagram and the
   uniqueness/cardinality rules that encode the business logic.
3. **[ports.md](ports.md)** — what each `PortKind` means, which of children, identifier and
   choices it carries, how defaults and assignment values are checked, and which widgets fit.
4. **[action-matching.md](action-matching.md)** — how an Action's `provides`/`requires`
   descriptors compile to JSONPath and how the relational port engine finds matching actions.
4. **[task-lifecycle.md](task-lifecycle.md)** — `assign`, the
   Task event state machine, and how results flow back to the caller.
5. **[agent-protocol.md](agent-protocol.md)** — the WebSocket wire protocol: register, authenticate,
   the liveness lease and its fencing token, task delivery, and connection takeover.
6. **[caller-protocol.md](caller-protocol.md)** — sub-assignment on the same socket: how an agent
   assigns *dependent* work (`AssignRequest`), controls its lifecycle
   (cancel/interrupt/pause/resume), and observes results (`…Event` mirrors); plus the HTTP intake.
7. **[realtime.md](realtime.md)** — channels, signals, topic keys, and how subscriptions consume
   them.
8. **[higher-order.md](higher-order.md)** — higher-order implementations (one implementation
   wrapping another) and server-side event unfolding.
9. **[provenance.md](provenance.md)** — Rekuest as the provenance authority: the signed
   attestation token minted at dispatch, its claim vocabulary, the human-root invariant, and the
   JWKS endpoint downstream services verify against.

## The one-paragraph mental model

Everything is anchored on the `(client, user, organization)` identity triple derived from the auth
token. A **Caller** is that triple acting as a requestor; an **Agent** is that triple (plus an
app/release/device) acting as a provider. An **Action** is an abstract, versioned function
contract; an **Implementation** binds an Action to an Agent. A caller's `assign` creates an
**Task** (the execution log) stamped with the caller, routed to an agent; the agent streams
**TaskEvents** back, which are persisted and fanned out to the caller's realtime topics
(`root_tasks_caller_{id}` for its own feed, `task_caller_{id}` for the agent-socket mirror). That is
the whole system in miniature; the rest is detail.

# Rekuest Server API Documentation

> **Looking for the architecture / the "why"?** See the design docs in
> [`design/`](design/README.md). This file is the GraphQL **API reference**; the authoritative,
> always-current surface is the live schema via GraphQL introspection (the playground at
> `/graphql`).

## Overview

Rekuest is the central broker of the Arkitekt ecosystem. It provides a GraphQL API (with WebSocket
subscriptions) for registering agents, defining actions, routing task execution, and managing agent
state. See [`design/README.md`](design/README.md) for the end-to-end picture.

## Architecture

- **GraphQL + WebSocket facade** — a single schema serves HTTP queries/mutations and realtime
  subscriptions; agents connect over a separate WebSocket at `/agi`.
- **PostgreSQL** for persistent storage. The relational port-matching engine uses Postgres-specific
  `jsonb_path_match`/JSONPath, so Postgres is required (SQLite is not sufficient for matching).
- **Redis** for both the realtime channel layer (subscription fan-out) and the hand-rolled agent
  delivery queue (work survives an agent being briefly offline).
- Horizontally scalable: the GraphQL/WS workers are stateless; shared state lives in Postgres and
  Redis.

> Historical note: older docs mention RabbitMQ. The current implementation routes work through Redis
> and the Channels layer; there is no hard RabbitMQ dependency.

## Core Concepts

See [`design/identity.md`](design/identity.md) and [`design/domain-model.md`](design/domain-model.md)
for detail. In brief:

### Identity — Caller and Agent
Every authenticated request carries a `(client, user, organization)` triple.

- **Caller** — that triple acting as a **requestor** (who asks for work). Owns tasks; keys the
  realtime topics `root_tasks_caller_{id}` and `task_caller_{id}`. A frontend has a Caller and no Agent.
- **Agent** — that triple plus an `app`/`release`/`device`, acting as a **provider** (who executes
  work). Connects over the WebSocket and runs implementations.

### Actions and Implementations
- **Action** — an abstract, versioned function contract (`app`, `key`, `version`, `hash`, typed
  `args`/`returns` ports).
- **Implementation** — binds an Action to an Agent via an `interface`. Carries bound `params`,
  dependencies, and optional higher-order wrapping.

### Tasks
- **Task** — one task execution: the central log, stamped with the caller, routed to an
  agent, accumulating `TaskEvent`s. See
  [`design/task-lifecycle.md`](design/task-lifecycle.md).

### State management
- **StateDefinition** — the schema for a kind of agent state.
- **State** / **Patch** / **Snapshot** — current value, incremental JSON-Patch history, and
  checkpoints, with a `global_rev` revision counter.

## GraphQL API Reference

> Field names below match the current schema (`facade/schema.py`). Selection sets are illustrative —
> use introspection for the full set of fields and input arguments.

### Queries

```graphql
# List compute agents (organization-scoped)
query Agents {
  agents {
    id
    name
    connected
    client { clientId }
    user { username }
    organization { slug }
  }
}

# Fetch one agent by ID (or by app/version/device_id)
query Agent($id: ID!) {
  agent(id: $id) {
    id
    name
    active
    implementations { id interface action { name } }
  }
}

# List / fetch actions
query Actions {
  actions { id name description hash kind }
}

# Registered implementations
query Implementations {
  implementations { id interface agent { id name } action { id name } }
}

# Tasks (filtered to the calling caller)
query Tasks {
  tasks { id reference latestEventKind isDone }
}

# State schemas and current state
query StateDefinitions {
  stateDefinitions { id name hash ports }
}
```

State is read with `state_for` / `checkout` / `checkout_agent` and the revision-aware queries
(`state_at_global_rev`, `snapshots_around_rev`, `forward_events_after_rev`, …). See
[`design/realtime.md`](design/realtime.md) for the snapshot-then-stream model.

### Mutations

```graphql
# Ensure an agent record exists / is up to date (creates the row agents register against)
mutation EnsureAgent($input: AgentInput!) {
  ensureAgent(input: $input) { id name }
}

# Assign a task. Provide exactly one routing target: action, implementation,
# actionHash, agent + interface, or a dependency (+ method/parent). Plus args, hooks, etc.
mutation Assign($input: AssignInput!) {
  assign(input: $input) { id reference latestEventKind }
}

# Steer a running task
mutation Cancel($input: CancelInput!)   { cancel(input: $input)   { id latestInstructKind } }
mutation Pause($input: PauseInput!)     { pause(input: $input)    { id } }
mutation Resume($input: ResumeInput!)   { resume(input: $input)   { id } }
mutation Interrupt($input: InterruptInput!) { interrupt(input: $input) { id } }
```

Other notable mutations (see `facade/schema.py` for the full list): `create_implementation`,
`delete_implementation`, `set_higher_order`, `implement_agent`, `block`/`unblock`, `bounce`/`kick`,
`pin_agent`/`pin_implementation`, `update_agent`/`delete_agent`, `auto_resolve` and
`create/update/delete_resolution`, `log_patches`/`log_snapshot`, plus the Blok/Dashboard/Toolbox/3D
families.

### Subscriptions

```graphql
# Updates on the caller's own tasks
subscription Tasks {
  tasks { create { id latestEventKind } event { id kind progress } }
}

# Agent connection/status changes within the organization
subscription Agents {
  agents { create update delete }
}

# Watch a state: current snapshot, then a stream of patches
subscription WatchState($stateId: ID!) {
  watchState(stateId: $stateId) { __typename }
}
```

Other streams (SDL names): `newActions`, `mytasks`, `tasks`, `agents`, `childTasks`, `agentTasks`,
`implementations` / `implementationChange`, `stateUpdateEvents`, `latestPatches`, `watchAgent`,
`watchState`, `probeEvents`.

## Authentication

All operations require authentication via the [Authentikate](https://github.com/arkitektio) system:

1. **Token-based** — Bearer JWT in the `Authorization` header.
2. **Client registration** — clients must be registered.
3. **Identity triple** — the token expands to `(client, user, organization)`; operations run in that
   context (this is what becomes the Caller / Agent identity).
4. **Organization scope** — resources are scoped to the user's organization via
   `build_prescoped_queryset` (`facade/types/base.py`).

```http
POST /graphql
Authorization: Bearer YOUR_JWT_TOKEN
Content-Type: application/json

{ "query": "query { agents { id name } }" }
```

Agents authenticate the same way over the WebSocket — the first frame is a `Register` carrying the
token; see [`design/agent-protocol.md`](design/agent-protocol.md).

## Error Handling

Standard GraphQL errors:

```json
{
  "data": null,
  "errors": [
    { "message": "Agent not found", "locations": [{"line": 2, "column": 3}], "path": ["agent"] }
  ]
}
```

Common categories: validation errors (bad input), not-found, permission denied, and authentication
required.

## Development Setup

### Prerequisites
- Python 3.12+
- PostgreSQL (required for action matching)
- Redis

### Installation
```bash
git clone https://github.com/arkitektio/rekuest-server-next.git
cd rekuest-server-next
uv sync                      # or: pip install -e ".[dev]"
cp config.yaml.example config.yaml
python manage.py migrate
python manage.py runserver
```

### Testing
```bash
# Postgres + Redis come up via the tests' docker-compose fixture; do not pre-start them.
uv run pytest tests/ --ignore=tests/test_integration.py
```

See [`DEVELOPMENT.md`](DEVELOPMENT.md) for the full workflow.

### GraphQL Playground
Visit `http://localhost:8000/graphql` to explore the schema and run queries interactively.

## Production Deployment

### Environment
- `DATABASE_URL` — PostgreSQL connection
- `REDIS_URL` / `AGENT_REDIS_HOST` / `AGENT_REDIS_PORT` — Redis for channels + agent queue
- `SECRET_KEY`, `DEBUG=false`, `ALLOWED_HOSTS`

### Scaling
- Run multiple stateless GraphQL/WS workers behind a load balancer.
- Use PostgreSQL with connection pooling; consider Redis HA for the channel layer and queue.

## Performance & Security

- Use `select_related`/`prefetch_related` (the `DjangoOptimizerExtension` is enabled) and the
  relational port indexes for matching (see [`design/action-matching.md`](design/action-matching.md)).
- Always use HTTPS in production, validate input, scope by organization, and sanitize error
  messages. Access is role/organization-scoped and agent ownership is validated.

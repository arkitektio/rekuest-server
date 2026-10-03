# Rekuest Server API Documentation

> **Looking for the architecture / the "why"?** See the design docs in
> [`design/`](design/README.md). This file is the GraphQL **API reference**; the authoritative,
> always-current surface is the live schema via GraphQL introspection (the playground at
> `/graphql`).

## Overview

Rekuest is the central broker of the Arkitekt ecosystem. It provides a GraphQL API (with WebSocket
subscriptions) for registering agents, defining actions, routing task execution, and managing agent
state. See [`design/README.md`](design/README.md) for the end-to-end picture.

This file covers the GraphQL API, which the rekuest server serves. The agent protocol (the `/agi`
WebSocket, the HookAgent and signal intakes) is served by takt and documented in
[`../takt/docs/`](../takt/docs/).

## Architecture

- **Two programs.** The rekuest server serves this GraphQL API (HTTP queries and mutations,
  WebSocket subscriptions) and owns the database schema. takt serves the agent protocol at
  `/agi`. A gateway routes `<prefix>/agi*` to takt and everything else to the server.
- **Mutations that touch a task or an agent are executed by takt.** `assign`, `cancel`,
  `interrupt`, `pause`, `resume`, the probe mutations, `ensureAgent`, `implementAgent`,
  `deleteAgent`, `deleteImplementation`, `createHigherOrderImplementation`, `bounce`, `kick`,
  `block`, `unblock`, `collect`, the drawer mutations and the schedule timing calls go through
  takt's internal API (`facade/takt.py`). A refusal comes back as an ordinary GraphQL error;
  if takt is unreachable the mutation fails.
- **PostgreSQL** for persistent storage. The relational port-matching engine uses Postgres-specific
  `jsonb_path_match`/JSONPath, so Postgres is required (SQLite is not sufficient for matching).
- **Redis** for both the realtime channel layer (subscription fan-out) and the per-agent delivery
  queue takt drains (work survives an agent being briefly offline).
- Horizontally scalable: server and takt replicas are stateless; shared state lives in Postgres
  and Redis.

## Core Concepts

See [`design/identity.md`](design/identity.md) and [`design/domain-model.md`](design/domain-model.md)
for detail. In brief:

### Identity — Caller and Agent
Every authenticated request carries a `(client, user, organization)` triple.

- **Caller** — that triple acting as a **requestor** (who asks for work). Owns tasks; keys the
  realtime topics `root_tasks_caller_{id}` and `task_caller_{id}`. A frontend has a Caller and no Agent.
- **Agent** — that triple plus an `app`/`release`/`device`, acting as a **provider** (who executes
  work). Connects to takt over the WebSocket and runs implementations.

### Actions and Implementations
- **Action** — an abstract, versioned function contract (`app`, `key`, `version`, `hash`, typed
  `args`/`returns` ports).
- **Implementation** — binds an Action to an Agent via an `interface`. Carries bound `params`,
  dependencies, and optional higher-order wrapping.

### Tasks
- **Task** — one task execution: the central log, stamped with the caller, routed to an
  agent, accumulating `TaskEvent`s. See
  [`../takt/docs/task-lifecycle.md`](../takt/docs/task-lifecycle.md).

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

# Automation: schedules, triggers, the signals services sent, and what became of each trigger
query Automation {
  schedules(filters: {enabled: true}) { id name runCount lastRunAt exhausted upcoming(count: 3) }
  triggers(filters: {failing: true}) { id name lastError firings(limit: 5) { outcome reason } }
  signals(filters: {matched: false}) { id identifier object firings { outcome reason trigger { name } } }
  wiregrams { key name schedules { id } triggers { id } }
}
```

State is read with `stateFor` / `checkout` / `checkoutAgent` and the revision-aware queries
(`stateAtGlobalRev`, `snapshotsAroundRev`, `forwardEventsAfterRev`, …). See
[`design/realtime.md`](design/realtime.md) for the snapshot-then-stream model.

### Mutations

```graphql
# Ensure an agent record exists / is up to date. A socket agent does not need this: its
# Register creates the row. Dashboards and HookAgents (kind, hookUrl) use it.
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

Other notable mutations (see `facade/schema.py` for the full list):

- **Agents:** `implementAgent` (the GraphQL twin of the declaration a socket `Register`
  carries), `updateAgent`, `deleteAgent`, `pinAgent`, `block` / `unblock`, `bounce` / `kick`.
- **Implementations:** `createHigherOrderImplementation`, `deleteImplementation`. An ordinary
  implementation is not created over GraphQL: an agent declares its implementations when it
  registers.
- **Schedules:** `createSchedule`, `updateSchedule`, `deleteSchedule`, `triggerSchedule` (run
  now). A schedule's `cron` is a five-field line read in its `timezone`; six-field lines and
  wrapping ranges such as `5-1` are refused.
- **Triggers:** `createTrigger`, `updateTrigger`, `deleteTrigger`, and `fireTrigger` to replay a
  trigger on a stored signal. `matchingSignals` and `Trigger.matchingSignals` are a dry run:
  which stored signals a rule would fire on.
- **Policies** on both: `description`, `endsAt`, `maxRuns`; `debounceSeconds` on a trigger;
  `overlap` and `catchUp` on a schedule. `updateSchedule` / `updateTrigger` can also change the
  target (action, agent, interface, port); the result is checked as a whole.
- **Wiregrams:** `importWiregram`, `deleteWiregram`, `exportWiregram`. A wiregram is one
  document of schedules and triggers; nothing is wired on a hub unless a user creates a rule or
  imports one. Rules name their target as an agent's name and interface. Importing the same
  `key` again updates what the earlier import created and removes what the document no longer
  lists; all or nothing.
- **Feeds:** the `signals`, `schedules` and `triggers` subscriptions.
- **Probes:** `probe`, `cancelProbe`, `pauseProbe`, `resumeProbe`.
- **Drawers:** `shelveInMemoryDrawer`, `unshelveMemoryDrawer`, `collect`.
- **Resolution:** `autoResolve`.
- Plus the Blok / Dashboard / Shortcut / test-case / 3D families.

### Subscriptions

```graphql
# Updates on the caller's own tasks
subscription Tasks {
  tasks { create { id latestEventKind } event { id kind progress } }
}

# Agent connection/status changes within the organization
subscription Agents {
  agents {
    create { id name connected }
    update { id name connected }
    delete
  }
}

# Watch a state: current snapshot, then a stream of patches
subscription WatchState($stateId: ID!) {
  watchState(stateId: $stateId) { __typename }
}
```

All streams (SDL names): `mytasks`, `tasks`, `agents`, `childTasks`, `agentTasks`,
`implementations` / `implementationChange`, `stateUpdateEvents`, `watchAgent`, `watchState`,
`probeEvents`.

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

Agents authenticate the same way over takt's WebSocket — the first frame is a `Register` carrying
the token; see [`../takt/docs/agent-protocol.md`](../takt/docs/agent-protocol.md).

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
uv sync
python manage.py migrate
python manage.py runserver
```

`config.yaml` is checked in with development values. Mutations that touch a task or an agent
need a running takt and `rekuest.takt_url` pointing at it; `docker compose up --build` at the
repository root starts the pair.

### Testing
```bash
# Postgres + Redis come up via the tests' docker-compose fixture; do not pre-start them.
uv run pytest tests/ --ignore=tests/test_integration.py
```

takt's own suites are in `takt/` (`cargo test`, and `takt/conformance`).
See [`DEVELOPMENT.md`](DEVELOPMENT.md) for the full workflow.

### GraphQL Playground
Visit `http://localhost:8000/graphql` to explore the schema and run queries interactively.

## Production Deployment

### Environment

Both programs are configured by one `config.yaml`, with `SECTION__KEY` environment overrides
(`POSTGRES__PASSWORD`, `REDIS__HOST`, `DJANGO__DEBUG`, …). See [`../CONFIG.md`](../CONFIG.md).
`rekuest.takt_url` and the `instance` block are required.

Images: `jhnnsrs/rekuest` (the server) and `jhnnsrs/rekuest-takt`,
released under the same version tags. Run the same version of both.

### Scaling
- Run multiple server replicas (GraphQL) and multiple takt replicas (`/agi`) behind a gateway
  that routes `<prefix>/agi*` to takt.
- Use PostgreSQL with connection pooling; consider Redis HA for the channel layer and queue.

## Performance & Security

- Use `select_related`/`prefetch_related` (the `DjangoOptimizerExtension` is enabled) and the
  relational port indexes for matching (see [`design/action-matching.md`](design/action-matching.md)).
- Always use HTTPS in production, validate input, scope by organization, and sanitize error
  messages. Access is role/organization-scoped and agent ownership is validated.

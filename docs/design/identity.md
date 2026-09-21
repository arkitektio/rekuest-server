# Identity: Caller vs Agent

Every authenticated interaction with Rekuest is rooted in one thing: the
**`(client, user, organization)` identity triple** carried by the auth token. Two distinct models
are built on that triple, and keeping them distinct is the single most important design decision in
the service.

- **`Caller`** — the triple acting as a **requestor** (who is asking for work).
- **`Agent`** — the triple (plus an app/release/device) acting as a **provider** (who executes work).

This document explains the triple, the two models, and why they are deliberately *not* the same row.

## The identity triple

Authentication is handled by [`authentikate`](../DEVELOPMENT.md). A validated token expands into
three independent entities:

| Component | Meaning |
| --- | --- |
| `client` | The app instance / OAuth client making the request. |
| `user` | The acting user within that client. |
| `organization` | The tenant the request is scoped to. |

The triple is expanded in two places, depending on transport:

- **HTTP GraphQL** — the request context already exposes `info.context.request.{client,user,organization}`.
- **WebSocket** — `default_authenticator` (`facade/consumers/agent_protocol.py`) calls
  `aexpand_{user,client,organization}_from_token` to derive the same triple from the agent's token.

Both paths converge on the same identity space, which is what lets a frontend caller and an agent
runtime that belong to the same app/user/org line up correctly.

## `Caller` — the requestor identity

`facade/models/caller.py`:

```python
class Caller(models.Model):
    client = models.ForeignKey(Client, ...)
    user = models.ForeignKey(User, ...)
    organization = models.ForeignKey(Organization, ...)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "user", "organization"],
                name="No multiple Callers for same App and User in the same organization allowed",
            )
        ]
```

A `Caller` is **`get_or_create`d on every request that needs to record who is asking**, via
`get_caller_for_context` (`facade/backend.py`):

```python
def get_caller_for_context(info: Info) -> models.Caller:
    caller, _ = models.Caller.objects.get_or_create(
        client=info.context.request.client,
        user=info.context.request.user,
        organization=info.context.request.organization,
    )
    return caller
```

The Caller's roles:

- **Owns requests.** It is stamped on `Task.caller` — the record of
  *who requested* the work, stored separately from *who executes* it.
- **Keys the realtime topics.** Task events are broadcast to `task_caller_{caller_id}`, and root
  task changes additionally to `root_tasks_caller_{caller_id}` (`facade/signals.py`); the caller
  subscribes there to watch its own work. See
  [realtime.md](realtime.md).
- **Exists without an Agent.** A pure frontend that only assigns and watches has a `Caller` and no
  `Agent` at all. This is the normal case, not an edge case.

There is exactly **one Caller per `(client, user, organization)`**.

## `Agent` — the provider runtime

`facade/models/agent.py`. An Agent carries the same triple **plus** the runtime descriptors that
make it a provider, and a pile of connection/liveness state:

| Field group | Fields | Why |
| --- | --- | --- |
| Identity | `client`, `user`, `organization` | The same triple, owned directly. |
| Runtime | `app`, `release`, `device`, `hash` | What code/where it runs; `hash` detects definition changes. |
| Connection | `connected`, `last_seen`, `active_connection_id`, `unique`, `kind`, `hook_url` | Live WebSocket/webhook state. |
| Lifecycle | `latest_event` (CONNECT/DISCONNECT), `health_check_interval`, `blocked` | Health + admin control. |

```python
class Agent(models.Model):
    client = models.ForeignKey(Client, related_name="agents", ...)
    user = models.ForeignKey(User, ...)
    organization = models.ForeignKey(Organization, ...)
    # ... app / release / device / connection state ...

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "user", "organization"],
                name="one_agent_per_client_user_organization",
            )
        ]

    @property
    def is_active(self):
        return self.connected and self.last_seen > timezone.now() - timedelta(minutes=5)
```

There is exactly **one Agent per `(client, user, organization)`** — the provider complement of the
Caller constraint. An Agent owns `Implementation`s (the actions it can run), `State`s, `Lock`s and
the `Task`s routed to it.

> **Note on creation:** the WebSocket `Register` creates the Agent for the token's identity when
> none exists (`facade.registration.ensure_agent`, shared with the `ensureAgent` mutation): the
> `app`/`release` come from the token's client. Nothing has to happen over GraphQL before an agent
> connects (guarded by `test_register_for_uncreated_agent_creates_it`).

## Why two models and not one

The 1:1-looking shape (`Agent` duplicates `Caller`'s `user`/`organization`) makes "just merge them"
tempting. It is wrong, for concrete reasons:

- **Caller-without-agent is normal.** Frontends assign, query and subscribe with a Caller and no
  Agent. Merging would force every requestor to own an Agent row — which means `app`/`release`/
  `device` and "I am a provider" — conflating *requestor* with *executor*.
- **Requestor ≠ executor on a single record.** `Task.caller` (who asked) and
  `Task.agent` (who runs) are deliberately different FKs. User A can assign to user B's
  agent. A merged model would have to denormalize the triple onto every task and
  lose the single, deduplicated identity object.
- **They evolve independently.** Provider cardinality might later become 1-per-device (same user,
  two machines); requestor identity stays 1-per-triple. Two models keep that future open.

The deliberate trade-off: the triple is denormalized onto both models and there is **no FK from an
Agent to "its" Caller**. That is fine — no code needs that link. Every place that used to read
`agent.registry.organization` is really the agent reading *its own* identity, now read directly as
`agent.organization` / `agent.user` / `agent.client`.

## Historical note: `Registry` → `Caller`

This identity object was previously a single `Registry` model that played *both* roles (requestor
and agent-owner, via a 1:1 `Agent.registry` FK). It was split:

- `Registry` was renamed to **`Caller`** to name its real role (the requestor).
- `Agent` gained its **own** `client` FK and **dropped** `registry`, so it stands alone.
- The requestor FK on `Task` / `TaskInstruct` was renamed
  `registry` → **`caller`**. The realtime topic was renamed with it, and has since been split into
  `task_caller_{id}` (every event of work this identity originated) and `root_tasks_caller_{id}`
  (the root-task change feed).

If you encounter `registry` in old branches, migrations, or external schema snapshots, read it as
"the Caller / the agent's own identity" depending on context.

## Where this shows up next

- The full model graph and constraints: [domain-model.md](domain-model.md).
- How a Caller's `assign` becomes routed work: [task-lifecycle.md](task-lifecycle.md).
- How an Agent authenticates and connects: [agent-protocol.md](agent-protocol.md).

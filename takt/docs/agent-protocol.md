# Agent Protocol: the WebSocket wire protocol

> Code references are to takt's `facade` crate, `takt/crates/facade/src/`, unless a path
> says otherwise.

Agents connect to Rekuest over a WebSocket at `/agi`, served by takt, and hold a long-lived,
stateful conversation: register, prove identity, receive work, stream results, answer heartbeats.
This document describes that protocol, how one connection is served, the single-live-connection
guarantee, and the at-least-once delivery queue.

Key files: `consumers/agent_protocol.rs` (the conversation), `consumers/agent_queue.rs` (the
delivery queue), `consumers/connections.rs` (this process's live connections), `persist/` (what
the protocol persists: `leases.rs`, `reports.rs`, `state.rs`, `transitions.rs`, `reconcile.rs`),
`message_router.rs` (where a registered agent's frames go), `messages.rs` (the frames; the wire
types are the `rekuest-protocol` crate's).

> **Everyone on `/agi` is an agent.** There are no connection modes and no capability layer: any
> token that authenticates connects as an agent, executes work, and holds that agent's write-lease.
> The same socket also lets an agent **assign dependent work** and drive its lifecycle — see
> [caller-protocol.md](caller-protocol.md) for `AssignRequest`, the lifecycle controls, and the
> `…Event` mirrors. What it may *not* do is originate a **root** task: roots must trace to an
> accountable human, so they come only from the GraphQL `assign` mutation (see the human-root
> invariant in [provenance.md](provenance.md)).

## How one connection is served

`serve` (`consumers/agent_protocol.rs`) takes one socket from its first frame to its close. A
writer task owns the socket's sending half and is fed by a channel (`Sender`), so frames from
different tasks never interleave on the wire.

Before `REGISTER`, `first_frame` parses and gates: the first frame must be a `REGISTER` whose
token authenticates, whose agent is not blocked and whose lease it wins (`register`). After it,
`run_session` runs side by side:

- the **read loop**, which answers heartbeats itself and hands every other frame, in order, to
- the **worker** (`work`), which routes them (`message_router::route`: persistence, replies);
- the **heartbeat**, which pings, waits for the answer and renews the lease;
- the **drain**, which delivers the agent's queue, fenced by the lease on every frame; and
- the **mirror**, which forwards the events of work this agent assigned (its caller group,
  `task_caller_{caller}`) as `…_EVENT` frames.

A heartbeat answer never waits behind the reports the agent sent before it: liveness means "the
agent answers", not "the backlog is short".

## Connect → register → run

```mermaid
sequenceDiagram
    autonumber
    participant AG as Agent
    participant S as serve (socket, writer)
    participant P as register / run_session
    participant AU as authentikate
    participant PB as registration + persist::leases
    participant Q as Redis agent queue

    AG->>S: WebSocket connect
    S->>P: first_frame (connection_id)
    AG->>P: Register{token, force, session_id, supervisor, name, hash, implementations, states, locks, bloks}
    P->>AU: authenticate_token, expand_token_context
    P->>PB: ensure_agent (+ memory shelve)
    alt agent.blocked
        P-->>AG: close(AGENT_IS_BLOCKED)
    end
    opt declaration carried and hash differs from the stored one
        P->>PB: implement_agent (one atomic registration)
        alt refused (catalog mismatch, ownership conflict)
            P-->>AG: ProtocolError + close(AGENT_REGISTRATION_REJECTED)
        end
    end
    P->>PB: on_agent_connected(agent, connection_id, session_id, force)
    note over PB: gate + claim under one row lock
    alt live incumbent && !force
        PB-->>P: LeaseClaim{claimed=false}
        P-->>AG: ProtocolError + close(AGENT_ALREADY_CONNECTED)
    else claimed
        PB-->>P: LeaseClaim{orphaned, displaced_incumbent}
    end
    opt displaced_incumbent
        P->>P: connections.kick_others() (best-effort; the new connection id already fenced them)
    end
    P-->>AG: Init{agent, hash, diagnostics, nonce, positions}
    par session tasks
        P->>Q: drain: read after commands_seen → send at pos (COMMAND_ACK trims)
        P-->>AG: heartbeat: periodic Heartbeat
    end
    loop while connected
        AG->>P: HeartbeatAnswer / Yield / Completed / StatePatch ...
        P->>PB: message_router::route(frame)
        opt after each answered Heartbeat
            P->>PB: renew_agent_lease(agent, connection_id)
            PB-->>P: no row matched → close(AGENT_REPLACED)
        end
    end
    AG->>S: WebSocket disconnect
    P->>PB: on_agent_disconnected → release_lease(connection_id)
```

### First frame must be `Register`

`first_frame` enforces that the first frame is a `REGISTER`; anything else closes the socket.
Frames are parsed into `AgentFrame` (`messages.rs`, the `rekuest-protocol` types), so text that
is not JSON or a frame that does not match the schema is rejected with a specific close code
(`codes.rs`: 3002, 3003).

`Register` carries `token` (identity), `force` (take over an existing connection), `session_id`
(the per-process reclaim signal: same id on reconnect ⇒ the process survived, reclaim its in-flight
work; a different id ⇒ a fresh process, fail-and-cascade) and the agent's **declaration** —
`name`, `hash`, `implementations`, `states`, `locks`, `bloks`, the same shapes as
`ImplementAgentInput`. Registering *is* implementing (see the lifecycle below); a `Register`
without a declaration only ensures the agent exists.

An optional `supervisor` names the app the agent says controls its lifecycle (e.g.
`plugin_engine`, set by the engine that started its container as `ARKITEKT_SUPERVISOR`). It is
not part of the declaration or its hash: every `Register` writes it to `Agent.supervisor` (none
clears it), including the hash-equal fast path. It is self-declared, so it is a hint (the
descriptor `@rekuest/supervisor` of `@rekuest/agent`, for pickers), never authority: a
supervisor maps the agent to its own records before acting on it.

### Authentication — ensure semantics

`register` verifies the token (`authentikate::authenticate_token`), expands it into
`(client, user, organization)` (`authentikate::expand::expand_token_context`) and calls
`registration::ensure_agent`: the agent keyed on that triple is created if it does not exist (a
new agent takes `app`/`release` from the client's release and is named after the client until its
first declaration names it), with a `MemoryShelve` beside it. Every stale `MemoryDrawer` is
deleted when the lease is claimed, unless `session_id` is the one the previous connection
registered with: a new process holds nothing in memory, while a reconnect of the same process
keeps its drawers. The socket is therefore the agent's complete control plane: no GraphQL call
precedes it (covered by `conformance/tests/test_registration.py`). The `ensureAgent` /
`implementAgent` mutations remain for dashboards and for a HookAgent's bootstrap (`kind`,
`hook_url`, `hook_url_secret`); the server forwards them to takt's internal API
(`agent/ensure`, `agent/implement`), which runs the same functions (`mutations/agent.rs`).

A token that does not authenticate closes the socket with 3003. A `blocked` agent is closed
immediately after authentication (`AGENT_IS_BLOCKED`, 4003). A handshake the server could not
serve (its database, its redis, the issuer's keys out of reach) is not a refusal: it closes with
3005, on which an agent comes back.

### Init + background loops

On successful register the protocol sends an `Init` carrying the agent id, the definition `hash`
the backend now holds for it (`null` when it was never implemented), the `diagnostics` of the
registration the `Register` carried, the `nonce` of that `Register` and the `positions` the server
has projected of each journal session it named. `run_session` then spawns the session's tasks:

- **`drain`** — relays queued work to the agent (see delivery below).
- **`heartbeat`** — liveness (see below).
- **`mirror`** and the **worker** — see "How one connection is served" above.

### Registration lifecycle — registering is implementing

```
Register{token, force, session_id, supervisor, name, hash, implementations, states, locks, bloks}
  → (declaration carried, hash differs) one atomic registration
      ↳ refused: ProtocolError{error} + close(AGENT_REGISTRATION_REJECTED)
  → Init{agent, hash, diagnostics, nonce, positions}
  → SessionInit{session_id, states} → StatePatch / StateSnapshot / Lock / Unlock / … as before
```

The declaration a `Register` carries is the socket twin of the `implementAgent` mutation and runs
the same function (`implement_agent` in `registration.rs`): one atomic reconciliation under the
organization lock — locks, actions + implementations, state definitions + states upserted,
undeclared ones reaped, bloks materialized. It runs *before* the lease is claimed, so a refused
registration strands no lease. When `Register.hash` equals the hash the backend already holds
nothing is reconciled (the reconnect fast path); a `Register` with no declaration at all only
ensures the agent exists. Its `State` rows are what the state stream (`SessionInit`,
`StatePatch`) needs, so an agent that has never registered over GraphQL can stream state.
`Init.diagnostics` carries the non-fatal findings (unknown catalog operations and the like); a
catalog mismatch or an ownership conflict aborts the whole registration and refuses the
connection — the agent is told why in a `ProtocolError` and closed with
`AGENT_REGISTRATION_REJECTED` (4006). A `Register` whose declaration fails *schema* validation
closes like any other malformed frame (3003).

The registration runs inline in the handshake; it is sub-second, and no heartbeat is pending
before `Init`.

**Protocol hints.** A definition may carry `protocols: ["on_scene_annotation", …]`: names of
where the action means to hook in. They are hints for whoever fires that hook (it finds agents
with `AgentFilter.implementsProtocols` and still matches the ports itself), never a contract:
nothing checks the ports against them. The action is listed under them beside the protocols
inferred from its ports (`predicate`, `hook`, `agent`); a declared name an inference also
writes keeps the inference's description. They are not part of `unique_hash`, so the action
keeps its hash, and its protocol rows are rewritten on every registration that reconciles, not
only when the definition changed. The agent's own hash covers them, so a change to the hints
alone does reconcile. An agent that hints nothing sends no `protocols` field: a takt that
predates hints refuses the field (`deny_unknown_fields`, 3003), so hints need this takt.

Shelving is never answered. The agent mints the `resource_id` itself and keeps the value:
`Shelve{identifier, resource_id, label, description}` records it, and `Unshelve{drawer}`
drops it, by that id. A refusal is only logged. `Collect{drawers}` remains the server's outbound request to drop drawers,
which the agent answers with `Unshelve`. Both are twins of the GraphQL `shelveInMemoryDrawer` /
`unshelveMemoryDrawer` mutations.

A `Shelve` frame upserts the drawer on `(shelve, resource_id)` with `agent_minted = true`.
`Collect` names agent-minted drawers by `resource_id` and the rest (drawers made through GraphQL)
by pk; the GraphQL `collect` and `unshelveMemoryDrawer` accept either form.

All outbound frames go through the connection's `Sender` to one writer task, because the
heartbeat, the drain, the mirror and the worker can all send at the same time; without that
their frames could interleave on the wire. A close is sent the same way and ends the writer.

## Liveness: the read predicate

An agent is live iff `connected AND last_seen > now − stale window` (`liveness.rs`; the window is
30 s, three heartbeat intervals). The
asymmetry between the two halves is deliberate and is why the predicate needs no repair:

- `connected = False` is a **definitive negative** — somebody observed a clean close, or the sweep
  revoked the lease. The agent is instantly, correctly not-live.
- `connected = True` is only **not-yet-refuted**. The disconnect handler runs solely on a clean
  close, so a crashed or SIGKILLed worker leaves the flag stuck True forever. The heartbeat lease
  (`last_seen`) is what makes True trustworthy: it expires on its own, with no writer.

One consequence worth stating: a stuck `connected = True` is harmless (the lease overrides it), but
a wrong `connected = False` would not be — so only the three transition paths below may write it.

## The two identifiers

| Column | Chosen by | Lifetime | Answers |
| --- | --- | --- | --- |
| `active_session_id` | the **client** (`Register.session_id`) | the executor **process** — deliberately survives reconnects | "is the same process back?" → reclaim vs. cascade |
| `active_connection_id` | the server (uuid4 per socket) | one **socket** = one ownership generation | "may you still write?" → the fencing token, and the disconnect guard |

`active_connection_id` is the fencing token. A claim writes the new socket's id, a revoke sets it
to `NULL`, and a uuid is never reused, so "the row still carries my id and is `connected`" is
exactly "nobody claimed or revoked since I did".

`session_id` cannot double as the fencing token: it is *required to match* on precisely the case
that needs fencing — a process blips, its old socket is wedged but alive, and it reconnects with
the same session to reclaim its work. A compare-and-set on the session would let the wedged socket
keep matching. It is also client-supplied and optional.

## Single live connection per agent

Only one connection may own an agent at a time. The gate and the claim happen **together**, inside
one `SELECT … FOR UPDATE` transaction in `on_agent_connected` (`persist/leases.rs`) — with the
two split (gate on a row read during authentication, write afterwards), two concurrent
registrations could both observe the same stale incumbent, both pass, and both be taken for the
process the in-flight work belongs to.

1. The new connection calls `on_agent_connected(…, force)`, which returns a `LeaseClaim`.
2. The claim is refused (`claimed = false` → `AGENT_ALREADY_CONNECTED`, 4004) only when the
   incumbent is *provably live*, `force` was not set, and it is not the same process's previous
   connection (same `session_id`). A **stale** incumbent — `connected` stuck true with an expired
   lease — is displaced **without** `force`, so a dead connection never wedges the agent behind
   a `--force` reconnect.
3. On success the claim writes this socket's `active_connection_id`; `connections.kick_others()` then
   tells every other connection of that agent **in this process** to stop (`Control::Displace` →
   close with `AGENT_REPLACED`, 4005).

`kick_others` is an **optimization, not a correctness dependency**. It reaches only connections
in the same takt process (`consumers/connections.rs`). A connection on another replica, or one
whose worker is wedged, is fenced by the new connection id: its next delivery or renewal finds it
is no longer the active connection and it closes. The displaced connection's
`on_agent_disconnected` is guarded on `active_connection_id`, so a departing stale connection
cannot clobber the live one's state.

## Heartbeats and the write-lease

`heartbeat` loops: sleep the heartbeat interval (10 s), arm a waiter, send `Heartbeat`, then wait
for the answer within the response timeout (5 s). A timeout closes the socket
(`HEARTBEAT_NOT_RESPONDED`, 3001) — which is what handles half-open sockets, so the residual
causes of a stuck `connected` are displacement and hard worker death, not hung TCP. Both values
are constants (`takt/crates/rekuest-server/src/settings.rs`), not configuration.

The read loop resolves the waiter itself, as soon as the `HeartbeatAnswer` is read: the answer
does not queue behind the reports the worker is still persisting. The heartbeat task then renews
the lease (`renew_agent_lease`, `persist/leases.rs`):

```sql
UPDATE facade_agent SET last_seen = $now
 WHERE id = $agent AND active_connection_id = $my_connection AND connected
-- no row matched: close(AGENT_REPLACED), this connection may no longer execute
```

Two properties of that one statement:

- **The rowcount is the answer.** No read-then-write, so no window to lose.
- **A fenced connection terminates itself.** It has lost the right to execute work, so it stops
  draining the queue rather than keep running against a lease it no longer holds.

The heartbeat never writes `connected`. That flag belongs to the transitions; re-asserting it
every beat would let a stalled worker resurrect itself *after* the sweep had already failed its
in-flight work.

### The write rule

| | Path | Mechanism |
| --- | --- | --- |
| **Transition** | claim (connect), release (disconnect), revoke (sweep) | a row lock (`SELECT … FOR UPDATE`), then the update |
| **Renewal** | heartbeat | lock-free compare-and-set on `active_connection_id` |

Renewal publishes nothing: no org-wide `AgentChange` is broadcast per heartbeat. A revoke
publishes the agent's change to the GraphQL agent feeds (`signals::agent_saved`).

## The stale sweep

`reconcile_stale_agents` (`persist/reconcile.rs`, driven by the sweep loop in `reaper.rs`, which
runs inside every takt replica) finds agents that are stuck-connected past the stale window and
revokes them: `connected = false` and `active_connection_id = NULL`, under a row lock that re-checks staleness
(`revoke_lease`). That lock is also the **claim** — several replicas sweep side by side, so only
the one that actually flips a row goes on to `reconcile_orphaned_executor_work`. The task transitions inside
that reconcile are claimed by the same rowcount discipline, so concurrent sweeps produce exactly
one terminal `TaskEvent` per task rather than one each.

Revocation is edge-triggered and cannot be made stateless: "executor died → transition its work" is
an exactly-once side effect that no derived predicate performs. What the fencing token buys is that
the sweep's decision **sticks** — a resumed worker's late heartbeat matches no row.

The stale sweep is the first of the sweeps a tick runs. The others (disconnected agents,
schedules, triggers, due tasks, unpicked tasks, due controls, expired tasks, retention) are
listed in [task-lifecycle.md](task-lifecycle.md).

## Task delivery — the agent's commands (kept until acknowledged)

The path to a socket agent is redis (`consumers/agent_queue.rs`), **not** the channel layer, on
purpose: a command pushed while the agent is away persists and survives the reconnect, whereas a
group send to an empty group would be dropped. The contract is in [journal.md](journal.md)
("Commands: the other direction"); this is how the server keeps it.

- **Producer:** `transport::deliver_to_agent` (`transport.rs`) is the one place that knows how to
  reach an agent. For a WEBSOCKET agent it appends the serialized frame to the stream
  `{prefix}:agent:{agent_id}:commands` at the next position (`agent_queue::push`: one script takes the
  position from `…:command_pos` and appends at it, so positions are given out in the order frames are
  appended). A probe's frame goes to `…:priority` instead, without a position. A WEBHOOK agent gets a
  signed POST (`hooks.rs`). Every key is built by `redis_keys.rs` under `redis.key_prefix`.
- **Consumer:** `drain` reads both streams from where the connection stands (the `commands_seen` of
  its `REGISTER`, then the last position it sent), a probe's frames first, stamps each command's `pos`
  and writes it to the socket. Sending removes nothing: the agent's `COMMAND_ACK {through}` trims the
  stream (`work` → `agent_queue::ack`). A probe's frame is forgotten once sent.

So a command is delivered **at least once on the wire and handled once by the agent**, which skips a
position it has seen. What the old list-and-processing-list pair could not do: a frame written into a
half-dead connection is still there, because only the agent's word removes it.

The drain is the *only* way work reaches an agent, while liveness is decided by the heartbeat loop
next to it, so it must never stop on its own:

- a **redis failure** is survived: reopen, back off, continue from the same position (and make sure
  numbering has not fallen behind what the agent has seen, should redis have lost its data);
- a **socket write failure** closes the connection (`AGENT_TRANSPORT_FAILED_CODE`, 3005); nothing was
  forgotten, so the next connection sends it again;
- a **displaced** connection stops draining the moment it is told (`Control::Displace`), and asks
  `holds_lease(agent, connection_id)` before every send: what it has not sent is the new holder's;
- an Assign whose task the server has **already finalized** (`is_stale_assign` →
  `reports::is_task_open`) is not sent; the next acknowledgement trims it with the rest.

A process that registers anew (`commands_seen` 0) is sent every command its predecessor never
acknowledged, behind the same fences. A task sent again has the assign it was first given
withdrawn (`agent_queue::withdraw_assigns`), so only the one that says how to go on arrives.

`BOUNCE` and `KICK` are not commands: they are orders to the connection there is now
(`transport::order_connection`), sent on the probes' lane without a position, and dropped by a
drain that began after they were given.

An acknowledgement is acted on only by the connection that holds the lease, and the pickup it
means is recorded before the stream is trimmed. So an assign that is neither picked up nor in the
stream never arrived (the push was lost, or redis's data was), and its task ends `LOST`
(`reconcile_unreachable_assigns`): it would wait for ever otherwise.

### Pickup, and asking

For a socket agent the **acknowledgement is the pickup**: when a `COMMAND_ACK` covers an `ASSIGN`,
the task's `picked_up_at` is set (`agent_protocol.rs`, `picked_up`). Nothing is ever sent a second
time as a new command, and there is no deadline on a socket agent's pickup: the assign waits in
the stream until the agent has it. An assign that cannot be queued at all (redis failed) ends the
task `LOST` there and then (`backend::dispatch`). Trying again is the caller's to decide, never the
server's.

What became of an acknowledged task is the agent's to report and the caller's to **ask**
(`inquiry.rs`):

- Whoever assigned a task asks about it: an agent with `INQUIRE_REQUEST` over its socket, anyone
  else with the GraphQL `inquire` mutation (`/internal/inquire`). The Python postmans ask by
  themselves after a task has been silent for `inquire_after` seconds (30 by default, 0 never).
- The server asks the task's agent with `INQUIRE {task, request}`, on the lane that jumps the
  backlog and keeps nothing. The agent answers at once, from memory, with
  `INQUIRY_ANSWER {task, status, request, step, reported_at, progress, message}`. `status` is
  `UNKNOWN`, `WAITING`, `RUNNING`, `PAUSED`, `HELD` or `ENDED`; the rest is what the task last
  said of itself (its last step, when it last reported, its last progress and message).
- The agent is asked only when its word settles something. While it is away, or has not
  acknowledged the assign yet, the server answers with what it knows (`asked: false` and why).
  An agent that does not answer within `ANSWER_TIMEOUT` is reported as silent.
- One answer is acted on: **`UNKNOWN` from the agent the task was given to ends the task `LOST`**,
  unless the task was given anew while the question was out. Everything else goes back to whoever
  asked, untouched.
- An agent that comes back with the session it had is asked about what it had under way, the same
  way (`ask_about`). `INIT` carries no list of inquiries any more.

**Cancel does not escalate by itself.** A cancel arms no deadline: turning an unconfirmed cancel
into an interrupt is the caller's decision, asked for with the cancel (`CANCEL_REQUEST`'s
`auto_interrupt`, a postman's `escalate_to_interrupt`). An interrupt nobody confirmed is still
finalized by the server after `control_deadline`. Neither clock runs while a socket agent is
away: the command is only waiting for it.

HookAgents keep a pickup watchdog (`reconcile_unpicked_tasks`, now for `WEBHOOK` agents only): over
HTTP the response is the only acknowledgement there is, so a task its endpoint reported nothing on
within `pickup_deadline` is posted once more, and ends `LOST` if it stays silent.

## Message catalogue

Messages are split by direction (`messages.rs`, re-exporting the `rekuest-protocol` types):

**Server → agent (`ToAgent`)** — `Init`, `Assign`, the lifecycle control messages `Cancel` /
`Interrupt` / `Pause` / `Resume`, `Decide`, `BlokRefused`, `Collect`, `Bounce`, `Kick`, `Heartbeat`,
`ProtocolError`, `JournalAck` / `JournalNack`, `Inquire` and `InquireResponse`. (The caller-bound
`…Event` mirrors and the `AssignResponse`/`ControlResponse` replies are `ToAgent` frames too, but
are addressed to the agent as a caller — see [caller-protocol.md](caller-protocol.md).)

**Agent → server (`FromAgent`)**, routed by `message_router::route`. The same router serves the
socket and the HookAgent HTTP intake:

| Message | Handled by | Effect |
| --- | --- | --- |
| `HeartbeatAnswer` | the read loop | resolves the pending heartbeat; the heartbeat task renews the lease |
| `Started` / `Progress` / `Log` | `persist::reports::on_report` | `TaskEvent(STARTED / PROGRESS / LOG)` |
| `Yield` | `persist::reports::on_report` | `TaskEvent(YIELD, returns)` + higher-order unfold |
| `Completed` | `persist::reports::on_report` | terminal: `is_done`, `finished_at` |
| `Cancelled` | `persist::reports::on_report` | terminal — confirms a `Cancel` (→ `CANCELLED`) |
| `Interrupted` | `persist::reports::on_report` | terminal — confirms an `Interrupt` (→ `INTERRUPTED`) |
| `Paused` | `persist::reports::on_report` | non-terminal — confirms a `Pause` (→ `PAUSED`) |
| `Resumed` | `persist::reports::on_report` | non-terminal — confirms a `Resume` (→ `RESUMED`) |
| `Held` | `holds::on_self_hold` | non-terminal — the task waits for a decision (→ `HELD`); under `AUTO` the server answers `Decide` at once (→ `DECIDED`) |
| `Failed` / `Critical` | `persist::reports::on_report` | terminal with message |
| `StatePatch` / `StateSnapshot` / `SessionInit` / `Lock` / `Unlock` | `persist::state::on_state` | append a `Patch`, write `Snapshot`s, initialize a `Session`, record the lock report |
| `Shelve` | `registration::shelve` | upsert a `MemoryDrawer` on the agent's shelve, agent-minted; no reply |
| `Unshelve` | `registration::unshelve` | drop the drawer if it is the agent's, by `resource_id`, else by pk; no reply |
| `Effect` | `persist::reports::on_report` | `TaskEvent(EFFECT)`: a value the task took from outside, kept for replay (see [journal.md](journal.md)) |
| `Inform` | `holds` | a blok shown to a person while the task goes on |
| `CommandAck` | the worker (`agent_queue`) | records the pickup of the assigns it covers, then trims the command stream |
| `JournalSkip` | `persist::positions` | passes over positions the agent no longer holds |
| `InquiryAnswer` | `inquiry` | resolves the question it answers; `UNKNOWN` ends the task `LOST` |
| `InquireRequest` | `inquiry` | asks about a task this agent assigned; answered with `InquireResponse` |

Frames whose task is a probe (`p-…`) go to the redis-held probe handlers (`probes/`), never to
the database.

The four lifecycle **confirmation events** are the executor's half of the two-phase controls: the
server forwards a `Cancel` / `Interrupt` / `Pause` / `Resume`, and the executing agent reports the
matching event above when it has acted (terminal for cancel/interrupt, non-terminal for
pause/resume). A numbered report is confirmed by the cumulative `JournalAck`; no report gets a reply of its
own. The router also handles the sub-assignment requests (`AssignRequest`,
`Cancel/Interrupt/Pause/ResumeRequest`, `StateRevisionRequest`, `ProbeRequest`) through
`persist::caller_ops` — see [caller-protocol.md](caller-protocol.md).

The declaration a `Register` carries is reconciled from the handshake itself (see the
registration lifecycle above), not through the router.

A second `Register` after registration is a protocol violation: the read loop closes the
connection (3003).

## Shutdown

When the socket closes, `run_session` stops executing first and then tears down: it aborts the
drain, the heartbeat and the mirror, lets the worker finish the frames it already has, leaves
the caller group, and calls `on_agent_disconnected(agent, connection_id)`. That releases the
lease only if it is still this connection's (a displaced one releases nothing) and fails the
agent's probes at once. The released lease marks the agent offline; after the grace window its
still-running tasks end `LOST` (workflows are resumed) — see
[task-lifecycle.md](task-lifecycle.md).

### Server-initiated closes stop the drain themselves

Every path where the *server* decides the connection is finished aborts the drain before it
sends the close frame:

- the fenced-lease path (`AGENT_REPLACED`), from the heartbeat's renewal or a displacement, and
- the unanswered-heartbeat path (`HEARTBEAT_NOT_RESPONDED`).

Otherwise a connection the server has just declared dead could keep popping Assigns off the
redis queue and acking them — the exact behaviour the liveness model exists to prevent.

## Workflows (resume, holds, guards)

See [workflows.md](workflows.md). On the wire:

- `ASSIGN` may carry `resume: {last_step, effects: [{key, effect, value}]}`: a workflow sent again
  after its agent died. The agent replays those effects by key and numbers new reports after
  `last_step`.
- `EFFECT` carries `key`; kinds `RECORD` and `HOLD` join `NOW`, `RANDOM`, `SLEEP`.
- `ASSIGN_REQUEST` carries `call_key`; a refused one answers `Nondeterministic workflow: …` when the
  key names another call than before.
- `PAUSED` from the agent may carry `message` and `details` (a hold of an agent from before `HELD`).
- `HELD {task, message?, prompt?, details?}`: the task waits for a person; the server answers with
  `DECIDE {task, approved, answer?, auto}`. A denial does not end the task. See [Held work](task-lifecycle.md#held-work).
- `ASSIGN_REQUEST` may carry `prompt {title, description?}`: why the caller wants the child run, shown
  to whoever decides it when the run's policy holds it.
- `STATE_REVISION_REQUEST {parent, dependency, state, since?, paths}` → `STATE_REVISION_RESPONSE
  {request, revision, changed?, detail?, error?}`: a workflow's guard.


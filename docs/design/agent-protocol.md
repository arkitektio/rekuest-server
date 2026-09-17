# Agent Protocol: the WebSocket wire protocol

Agents connect to Rekuest over a WebSocket at `/agi` and hold a long-lived, stateful conversation:
register, prove identity, receive work, stream results, answer heartbeats. This document describes
that protocol, the **humble-object** design that makes it testable, the single-live-connection
guarantee, and the at-least-once delivery queue.

Key files: `facade/consumers/agent_protocol.py` (the protocol), `async_consumer.py` (the Channels
adapter), `agent_queue.py` (the delivery queue), `facade/persist_backend.py` (the backend port),
`facade/messages.py` (the message catalogue).

> **Everyone on `/agi` is an agent.** There are no connection modes and no capability layer: any
> token that authenticates connects as an agent, executes work, and holds that agent's write-lease.
> The same socket also lets an agent **assign dependent work** and drive its lifecycle — see
> [caller-protocol.md](caller-protocol.md) for `AssignRequest`, the lifecycle controls, and the
> `…Event` mirrors. What it may *not* do is originate a **root** task: roots must trace to an
> accountable human, so they come only from the GraphQL `assign` mutation (see the human-root
> invariant in [provenance.md](provenance.md)).

## The humble-object design

The conversation logic lives in `AgentProtocol` — a **plain object with injected dependencies that
knows nothing about Django Channels or WebSockets**. The transport (`send`, `close`), the message
`queue`, the `backend` port (`persist_backend`), the `authenticator`, and the group hooks
(`register_connection`, `kick_others`) are all injected. Because every collaborator is injected, the
whole protocol/lifecycle/heartbeat behaviour is unit-testable with fakes — no docker, no DB, no
monkeypatching.

`AgentConsumer` (`async_consumer.py`) is the thin Channels adapter: on `connect` it accepts the
socket, mints a `connection_id`, and builds an `AgentProtocol` whose `send`/`close` close over the
WebSocket and whose `queue` is a `RedisAgentQueue`. `receive` forwards frames to the protocol;
`disconnect` calls `protocol.shutdown()`.

## Connect → register → run

```mermaid
sequenceDiagram
    autonumber
    participant AG as Agent
    participant AC as AgentConsumer (transport)
    participant P as AgentProtocol
    participant AU as default_authenticator
    participant PB as persist_backend
    participant Q as Redis agent queue

    AG->>AC: WebSocket connect
    AC->>P: build protocol (connection_id)
    AG->>P: Register{token, force}
    P->>AU: authenticate(token) → Agent
    alt agent.blocked
        P-->>AG: close(AGENT_IS_BLOCKED)
    end
    P->>AC: register_connection(agent.pk) (join group)
    P->>PB: on_agent_connected(agent.pk, connection_id, force)
    note over PB: gate + claim under one row lock
    alt live incumbent && !force
        PB-->>P: LeaseClaim{claimed=False}
        P-->>AG: ProtocolError + close(AGENT_ALREADY_CONNECTED)
    else claimed
        PB-->>P: LeaseClaim{epoch, tasks, displaced_incumbent}
    end
    opt displaced_incumbent
        P->>AC: kick_others() (best-effort; the epoch bump already fenced them)
    end
    P-->>AG: Init{agent, inquiries=[AssignInquiry...]}
    par background loops
        P->>Q: listen_for_tasks: pop → send → ack
        P-->>AG: heartbeat: periodic Heartbeat
    end
    loop while connected
        AG->>P: HeartbeatEvent / YieldEvent / DoneEvent / StatePatch ...
        P->>PB: dispatch(message)
        opt HeartbeatEvent
            P->>PB: renew_agent_lease(agent.pk, epoch)
            PB-->>P: rowcount 0 → close(AGENT_REPLACED)
        end
    end
    AG->>AC: WebSocket disconnect
    AC->>P: shutdown() → on_agent_disconnected(connection_id)
```

### First frame must be `Register`

`receive` enforces that the first validated frame is a `messages.Register`; anything else closes the
socket. Frames are parsed and validated through a discriminated-union pydantic model
(`FromAgentPayload`), so malformed JSON or schema-mismatched frames are rejected with a specific
close code (`codes.py`).

`Register` carries `token` (identity), `force` (take over an existing connection), and `session_id`
(the per-process reclaim signal: same id on reconnect ⇒ the process survived, reclaim its in-flight
work; a different id ⇒ a fresh process, fail-and-cascade).

Unlike every other message, `Register` **rejects unknown fields**. It used to carry a `mode`, and a
client still sending `mode: "OBSERVER"` must be told to update rather than be silently admitted as a
full agent — which would claim the write-lease and displace the real executor.

### Authentication

`default_authenticator` expands the register token into `(client, user, organization)` and does
`Agent.objects.aget_or_create(client=, user=, organization=, defaults=dict(name=client.client_id))`.

> **Pinned behaviour:** the create-branch omits the required `app`/`release`/`device` columns, so
> this can only *find* an agent that was already created out-of-band by the `ensureAgent` mutation.
> Registering for an uncreated agent is rejected (guarded by
> `test_register_for_uncreated_agent_is_rejected`). The authenticator is injected, so deployments can
> swap it.

A `blocked` agent is closed immediately after authentication.

### Init + background loops

On successful register the protocol sends an `Init` carrying the agent id and an `AssignInquiry`
per pending task (work that was queued/unfinished while it was away — returned by
`on_agent_connected`). It then spawns two background tasks:

- **`listen_for_tasks`** — relays queued work to the agent (see delivery below).
- **`heartbeat`** — liveness (see below).

All outbound frames funnel through a single `_send` guarded by an `asyncio.Lock`, because the
heartbeat loop, the listen loop, and `receive` can all try to send concurrently on the same event
loop; without serialization their frames could interleave on the wire. `close` deliberately stays
outside the lock (and is never called while it is held) to avoid deadlock.

## Liveness: the read predicate

An agent is live iff `connected AND last_seen > now − AGENT_STALE_AFTER` (`facade.liveness`). The
asymmetry between the two halves is deliberate and is why the predicate needs no repair:

- `connected = False` is a **definitive negative** — somebody observed a clean close, or the sweep
  revoked the lease. The agent is instantly, correctly not-live.
- `connected = True` is only **not-yet-refuted**. The disconnect handler runs solely on a clean
  close, so a crashed or SIGKILLed worker leaves the flag stuck True forever. The heartbeat lease
  (`last_seen`) is what makes True trustworthy: it expires on its own, with no writer.

One consequence worth stating: a stuck `connected = True` is harmless (the lease overrides it), but
a wrong `connected = False` would not be — so only the three transition paths below may write it.

## The three identifiers, and why there are three

| Column | Chosen by | Lifetime | Answers |
| --- | --- | --- | --- |
| `active_session_id` | the **client** (`Register.session_id`) | the executor **process** — deliberately survives reconnects | "is the same process back?" → reclaim vs. cascade |
| `active_connection_id` | the server (uuid4 per socket) | one **socket** | "which socket is this?" → routing, disconnect guard |
| `lease_epoch` | the server (monotonic `+1`) | one **ownership generation** | "may you still write?" |

`session_id` cannot double as the fencing token: it is *required to match* on precisely the case
that needs fencing — a process blips, its old socket is wedged but alive, and it reconnects with
the same session to reclaim its work. A compare-and-set on the session would let the wedged socket
keep matching. It is also client-supplied and optional. `active_connection_id` is closer, but a
UUID is a *name*, not a *generation*: there is no value meaning "nobody owns this", so the sweep
cannot revoke without naming a successor. With an integer, revoke is `+1`.

## Single live connection per agent

Only one connection may own an agent at a time. The gate and the claim happen **together**, inside
one `select_for_update` transaction in `on_agent_connected` — splitting them (gate on an instance
loaded during authentication, write afterwards) let two concurrent registrations both observe the
same stale incumbent, both pass, and both be handed the in-flight work as `Init` inquiries.

1. The new connection **joins the group first** (so it can later be kicked), then calls
   `on_agent_connected(..., force=...)`, which returns a `LeaseClaim`.
2. The claim is refused (`claimed = False` → `AGENT_ALREADY_CONNECTED`) only when the incumbent is
   *provably live* and `force` was not set. A **stale** incumbent — `connected` stuck True with an
   expired lease — is displaced **without** `force`, so a dead connection never wedges the agent
   behind a `--force` reconnect.
3. On success the claim bumps `lease_epoch` and returns it; `kick_others()` then `group_send`s the
   displacement (`agent_displace` → close with `AGENT_REPLACED`, skipping the initiator).

`kick_others` is now an **optimization, not a correctness dependency**. It is a best-effort
channel-layer fanout: if it is dropped, or the incumbent's worker is partitioned, the epoch bump
still fences that connection at its next heartbeat. The displaced connection's
`on_agent_disconnected` remains guarded on `active_connection_id`, so a departing stale connection
cannot clobber the live one's state.

## Heartbeats and the write-lease

`heartbeat` loops: sleep `AGENT_HEARTBEAT_INTERVAL`, arm a fresh future, send `Heartbeat`, then
`wait_for` the answer within `AGENT_HEARTBEAT_RESPONSE_TIMEOUT`. A timeout closes the socket
(`HEARTBEAT_NOT_RESPONDED`) — which is what already handles half-open sockets, so the residual
causes of a stuck `connected` are displacement and hard worker death, not hung TCP.

When the agent answers, `on_agent_heartbeat` resolves the future **before** the DB write — the
write is a round-trip, and doing it first could push resolution past the timeout and wrongly close
a connection that actually answered. It then renews the lease:

```python
rows = Agent.objects.filter(id=agent_id, lease_epoch=my_epoch).update(last_seen=now())
if rows == 0:
    close(AGENT_REPLACED)   # displaced or revoked — this connection may no longer execute
```

Three properties of that one statement:

- **The rowcount is the answer.** No read-then-write, so no window to lose.
- **A fenced connection terminates itself.** It has lost the right to execute work, so it must stop
  draining the queue rather than keep running against a lease it no longer holds.
- **Only executors renew.** Observers and callers share the executor's `Agent` row (identity is
  `client`/`user`/`organization`), so their `lease_epoch` is `None` and their heartbeat stays purely
  transport-level. Otherwise an open dashboard would keep `last_seen` fresh — forging executor
  liveness for an agent with no executor attached, and blinding the stale sweep for as long as it
  stayed open.

The heartbeat also no longer writes `connected`. That flag belongs to the transitions; re-asserting
it every beat is what let a stalled worker resurrect itself *after* the sweep had already failed
its in-flight work.

### The write rule

| | Path | Mechanism | Fires `agent_post_save`? |
| --- | --- | --- | --- |
| **Transition** | claim (connect), release (disconnect), revoke (sweep) | `select_for_update` + `save()` | yes — the agent feeds must see it |
| **Renewal** | heartbeat | lock-free compare-and-set, `update()` | no — nothing observable changed |

Keeping renewal out of the signal path also removes an org-wide `AgentChange` broadcast that
previously fired every `AGENT_HEARTBEAT_INTERVAL` for every connected agent.

## The stale sweep

`reconcile_stale_agents` (driven by the in-process `reaper` loop of every backend — there is no
management command) finds agents that are stuck-connected past the stale window and revokes them: `connected = False`
plus an epoch bump, under a row lock that re-checks staleness. That lock is also the **claim** —
production runs several daphne processes, each with its own reaper, so only the worker that
actually flips a row goes on to `reconcile_orphaned_executor_work`. The task transitions inside
that reconcile are claimed by the same rowcount discipline, so concurrent sweeps produce exactly
one terminal `TaskEvent` per task rather than one each.

Revocation is edge-triggered and cannot be made stateless: "executor died → transition its work" is
an exactly-once side effect that no derived predicate performs. What the fencing token buys is that
the sweep's decision **sticks** — a resumed worker's late heartbeat matches no row.

## Task delivery — the agent queue (at-least-once)

The backend→agent path is a hand-rolled Redis queue (`agent_queue.py`), **not** the Channels layer,
on purpose: a message pushed while the agent is briefly offline persists in Redis and survives
reconnect, whereas a `group_send` to an empty group would be dropped.

- **Producer:** `AgentConsumer.broadcast(agent_id, message)` (called from backend/signal code)
  pushes the serialized message with `lpush` onto `{prefix}:agent:{agent_id}:queue`, reusing a pooled sync Redis
  connection. Every key this service writes is built by `facade/redis_keys.py` under
  `redis.key_prefix` (default `rekuest`): Redis is shared infrastructure, and a bare `42_my_queue`
  is one integer away from another deployment's agent 42 receiving this one's Assigns. Frames left
  under the old un-namespaced keys by a previous release are adopted once per connection
  (`_adopt_legacy_lists`), oldest first, so nothing already queued is stranded.
- **Consumer:** `listen_for_tasks` calls `queue.pop`, which uses `blmove` to atomically move the
  message into a per-agent processing list `{prefix}:agent:{agent_id}:processing` (it stays there), then the
  protocol **delivers first, then `ack`s** (`lrem` from the processing list).

The send-then-ack ordering gives **at-least-once** semantics: a crash between `pop` and `ack` leaves
the message in the processing list, and the next connection that wins the agent's lease **recovers
it** — `queue.recover` moves everything still in `{prefix}:agent:{agent_id}:processing` back to the head of the
queue (oldest first) before it starts popping. The queue is an abstract port (`AgentQueue`) with a
`RedisAgentQueue` for real deployments and an `InMemoryAgentQueue` for unit tests.

The drain loop is the *only* way work reaches an agent, while liveness is decided by the heartbeat
loop next to it — so it must never stop on its own, or the agent keeps looking alive, keeps being
selected, and receives nothing:

- a **queue failure** (redis restart, a half-dead connection — pops block for a finite
  `POP_BLOCK_SECONDS`, never forever) is survived: drop the connection, back off, recover, continue;
- a **socket write failure** closes the connection (`AGENT_TRANSPORT_FAILED_CODE`, 3005) with the
  frame left in the processing list, so the agent reconnects and the frame is recovered;
- a **displaced** connection stops draining the moment it is told (`agent_displace`), not at its
  next heartbeat — until then it would compete for the new connection's Assigns;
- an Assign whose task the server has **already finalized** (the *delivery-time fence*,
  `is_task_open`) is acked and dropped instead of delivered.

At-least-once means an agent can see the same task id twice; agents dedupe an Assign by task id.

Two connections can briefly contend for one agent's queue, because the displacement hint
(`agent.displace`) rides the channel layer and the channel layer drops messages when a process's
receive queue is full. So ownership is not taken on trust: `listen_for_tasks` asks
`holds_lease(agent, epoch)` — one primary-key lookup — immediately before every `_send`. A
connection that has been fenced hands the frame back with `queue.requeue` (atomic: `LREM` from
in-flight, `RPUSH` to the head, so a concurrent `recover` by the new holder cannot duplicate it)
and closes itself. That narrows the window from one heartbeat interval to a single frame; it
cannot be closed entirely, because a socket write is not transactional with the lease. A lease
holder also calls `recover()` whenever a pop comes back empty, which rescues frames stranded by a
holder that died between its own pop and requeue.

### The pickup watchdog

Everything above still cannot prove an Assign *arrived*. The server therefore tracks, per task,
`dispatched_at` / `dispatch_attempts` (handed to the transport) and `picked_up_at` (the first report
of **any** kind from the agent — `latest_event_kind` cannot serve, because `Progress`/`Log`/`Yield`
never move it off `QUEUED`). `reconcile_unpicked_tasks` redelivers, once, a task whose *live* agent
(or webhook endpoint) has reported nothing within `pickup_deadline`; silent again → `CRITICAL`.
Physical-effect work that verifiably left is never redelivered. Tasks of agents that are not live
are left to the disconnect path. A reconnecting agent is not *inquired* about work it never picked
up (it would answer "unknown → Critical" for a task it is about to receive); that work's clock is
restarted instead.

## Message catalogue

Messages are split by direction (`facade/messages.py`):

**Server → agent (`ToAgentMessage`)** — `Init`, `Assign`, the lifecycle control messages `Cancel` /
`Interrupt` / `Pause` / `Resume`, `Collect`, `Bounce`, `Kick`, `Heartbeat`, `ProtocolError`, and
inquiries (`AssignInquiry`). (The caller-bound `…Event` mirrors and the `AssignResponse`/`ControlResponse` acks also ride
`ToAgentMessage` but are addressed to callers — see [caller-protocol.md](caller-protocol.md).)

**Agent → server (`FromAgentMessage`)**, dispatched (via the shared
`facade/message_router.py:route_from_agent_message`) to the backend:

| Message | Handler | Effect |
| --- | --- | --- |
| `HeartbeatEvent` | `on_agent_heartbeat` | liveness ack |
| `ProgressEvent` | `on_agent_progress` | `TaskEvent(PROGRESS)` |
| `LogEvent` | `on_agent_log` | `TaskEvent(LOG)` |
| `YieldEvent` | `on_agent_yield` | `TaskEvent(YIELD, returns)` + higher-order unfold |
| `DoneEvent` | `on_agent_done` | terminal: `is_done`, `finished_at` |
| `CancelledEvent` | `on_agent_cancelled` | terminal — confirms a `Cancel` (→ `CANCELLED`) |
| `InterruptedEvent` | `on_agent_interrupted` | terminal — confirms an `Interrupt` (→ `INTERRUPTED`) |
| `PausedEvent` | `on_agent_paused` | non-terminal — confirms a `Pause` (→ `PAUSED`) |
| `ResumedEvent` | `on_agent_resumed` | non-terminal — confirms a `Resume` (→ `RESUMED`) |
| `ErrorEvent` / `CriticalEvent` | `on_agent_error` / `on_agent_critical` | terminal with message |
| `StatePatchEvent` | `on_agent_state_patch` | append a `Patch` |
| `StateSnapshotEvent` | `on_agent_state_snapshot` | write `Snapshot`s |
| `SessionInitMessage` | `on_agent_session_init` | initialize a `Session` |

The four lifecycle **confirmation events** are the executor's half of the two-phase controls: the
server forwards a `Cancel` / `Interrupt` / `Pause` / `Resume`, and the executing agent reports the
matching event above when it has acted (terminal for cancel/interrupt, non-terminal for
pause/resume). Each terminal/confirmation report is acked with an `EventAck` so the agent can stop
retaining it. The router also handles the sub-assignment requests (`AssignRequest`,
`Cancel/Interrupt/Pause/ResumeRequest`) — see [caller-protocol.md](caller-protocol.md).

A second `Register` after registration is a protocol violation — it must not re-run `on_register`
(that would orphan the first listen/heartbeat pair); it falls through to the catch-all and closes.

## Shutdown

`AgentProtocol.shutdown` (called from the consumer's `disconnect`) cancels the listen and heartbeat
tasks, calls `on_agent_disconnected(agent.pk, connection_id)` (which no-ops if displaced), and closes
the queue connection. The persisted disconnect marks the agent offline and flags its still-running
tasks `DISCONNECTED` — see [task-lifecycle.md](task-lifecycle.md).

### Server-initiated closes must stop the drain themselves

`close()` only sends the close frame. **Channels does not invoke `disconnect()` for a
server-initiated close**, so `shutdown` may not run until the peer acknowledges — or at all, if it
never does. Every path where the *server* decides the connection is finished therefore calls
`RegisteredSession.stop_executing()` first, which cancels `listen_for_tasks` immediately:

- the fenced-lease path (`AGENT_REPLACED`), and
- the unanswered-heartbeat path (`HEARTBEAT_NOT_RESPONDED`).

Without it, a connection the server has just declared dead keeps popping Assigns off the redis
queue and acking them — the exact behaviour the liveness model exists to prevent. Cancelling twice
is safe, so `shutdown` still cancels unconditionally when it does run.

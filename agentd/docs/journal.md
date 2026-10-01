# Agent reports: order, dedup and task history

> Code references are to agentd's `facade` crate, `agentd/crates/facade/src/`, unless a path
> says otherwise.

This is the contract between the rekuest server and every agent that reports over the agent socket:
the Rust agent (`rekuest` crate in `arkirust`) and the Python agent (`arkitekt-runtime` for numbering,
the gate and `Task`; `rekuest` for the wire). On the server's side the contract is agentd's:
`message_router.rs` and `persist/positions.rs`.

The frames are the types of the `rekuest-protocol` crate, which agentd and the Rust agent share
and which is tested against its own fixture of example frames
(`tests/fixtures/agent_wire_examples.json` in that crate). The Python agent packages carry
canonical examples of the numbered frames in their own `tests/fixtures/agent_wire.json`. A
change to the wire changes those fixtures.

## Two scopes

- **The session is the agent process.** It numbers everything the process reports, in the order it
  happened (`pos`). The server keeps one watermark per session, "projected up to `pos` N", and
  that watermark alone makes a resent frame a no-op. A session ends when the process ends.
- **The task is the durable unit.** Everything a task did is numbered per task (`task_step`) and
  stored with the task: its `TaskEvent`s (logs, progress, yields, lifecycle, effects), the `Patch`es
  it caused, and its child tasks (by `parent_step`). A task outlives the session that ran it. That is
  what a later replay engine will read to resume a task in a new session. There is no separate
  per-frame journal on the server.

## Numbered frames

These frames carry the numbering fields next to `id` (and `seq`, where it exists):

- the task reports: `STARTED`, `PROGRESS`, `LOG`, `YIELD`, `PAUSED`, `RESUMED`, `COMPLETED`, `FAILED`,
  `CRITICAL`, `CANCELLED`, `INTERRUPTED`, and `EFFECT`;
- the state frames: `SESSION_INIT`, `STATE_PATCH`, `STATE_SNAPSHOT`;
- `LOCK`, `UNLOCK`, `SHELVE`, `UNSHELVE`.

`UNLOCK` names the task that held the lock in `task`. `SHELVE` names the task that shelved the value in
`task`, or omits it when no task shelved it.

| field | |
|---|---|
| `pos` | 1, 2, 3, … per session, with no gaps. `(journal_session, pos)` is the frame's durable key. |
| `journal_session` | The session `pos` belongs to: the `session_id` the process registered with. (It isn't called `session_id` because `STATE_PATCH` already has that field.) |
| `agent_ts` | When the agent recorded the frame, in epoch seconds (float). |
| `task_step` | Only on frames of a task (`STATE_PATCH`: the changing task; `UNLOCK`: the holder; `SHELVE`: the task that shelved, if any). The value is 1, 2, 3, … per task. It isn't called `step` because `ASSIGN` already has a `step` flag. |

**Not numbered:**
- `REGISTER`, `HEARTBEAT_ANSWER`, and the request/reply frames (`ASSIGN_REQUEST`, `PROBE_REQUEST`, the
  control requests). Each has its own reply.
- A **probe's task reports**: frames whose `task` has the id prefix `p-` (an `ASSIGN` with
  `probe: true`). Probes are ephemeral by design: the server keeps them in redis and never in the
  database, so they have no durable key and are never retained or resent.
  - State frames are the exception. A `STATE_PATCH` a probe caused is still numbered, because the
    `global_rev` chain must not have holes. It carries no `task_step`, and its `task_id` stays the
    probe id.

**Task steps also number child calls.** A task that calls another action takes the next
`task_step` for it and sends it on `ASSIGN_REQUEST` as `parent_step`, next to `parent`. (It isn't
called `task_step` because that name is the numbered frames' own stamp.) The request
stays unnumbered (no `pos`): it has its own reply. The server stores the step as the child's
`parent_step` and makes the request idempotent on `(parent, parent_step)`, so a call re-issued after
a restart returns the same child whatever its `reference`. `reference` stays the caller's own
idempotency key, idempotent on `(caller, reference)`, and is optional. That step has no
`TaskEvent` of its own: the child task with that `parent_step` is its record.
So a task's steps are gapless across its `TaskEvent`s, its `Patch`es and its child tasks together.

**Steps belong to the process that ran the task.** Another process can report on a task it never
ran: a new process answering a `PAUSE` or an inquiry for a task its predecessor held. That report
carries `pos` but no `task_step`, because only the process that ran the task knows its steps.

## Ordering rules (agent side)

1. `pos` and `task_step` are assigned, and the frame is handed on to be sent, under **one lock**. So
   frames go out in `pos` order.
2. Within a task, frames follow program order: `STARTED` < `PROGRESS`/`LOCK` < patches, yields,
   effects, … < the terminal report < `UNLOCK`.
3. **Nothing is recorded for a task after its terminal report.** Each task has a *gate*:
   - Reports and state changes enter the gate for as long as they run synchronously.
   - The check happens before anything changes, so a multi-op update is all or nothing.
   - The terminal report closes the gate first. Closing refuses new entries and waits for those in
     flight.
   - Entering again from inside an entry (a log inside an update closure) is allowed.
   - `LOCK`/`UNLOCK` are not gated, but a task's `UNLOCK`s still come **after** its terminal
     report.
   - A lifecycle request (`PAUSE`, `RESUME`, …) for a task that already ended records nothing.
4. A `STATE_SNAPSHOT` at revision N comes before the patch that reaches N+1, and already contains it.

## Effects

An `EFFECT` records a value a task took from outside itself, so that a replay can return the same
value instead of taking a new one.

```json
{"type": "EFFECT", "id": "…", "task": "42", "effect": "NOW", "value": 1790000000.25,
 "pos": 17, "journal_session": "…", "agent_ts": 1790000000.25, "task_step": 5}
```

| `effect` | `value` | task helper |
|---|---|---|
| `NOW` | epoch seconds (float) | `task.now()` |
| `RANDOM` | the bytes as hex | `task.random(n)` |
| `SLEEP` | the deadline, in epoch seconds (float) | `task.sleep(seconds)`, which records the deadline and then sleeps until it |

Each carries a `key` (by default its kind and occurrence, `NOW:1`); EFFECT events are unique per
`(task, key)`. A resumed workflow looks each key up first and gets the recorded value back (see
[workflows.md](workflows.md)). `RECORD` (`task.record`) and `HOLD` (a hold a person resumed) are
effects too.

## Delivery, dedup and acks

- **The agent retains every numbered frame** until a `JOURNAL_ACK` covers it. After `INIT` (and
  never between `REGISTER` and `INIT`), it sends the retained frames in `pos` order, starting from
  the lowest unacked one, followed by new frames.
- **The server keeps `Session.projected_pos`** for each `(agent, journal_session)` and handles
  each numbered frame by its `pos`:

  | `pos` | handling |
  |---|---|
  | `≤ projected_pos` | Already handled. Skip it, and ack again. |
  | `= projected_pos + 1` | **Claim, project, confirm** (`persist/positions.rs`). The claim is a conditional update of `claimed_pos` that succeeds only while no other replica has one in flight. So when two replicas get the same frame, one projects it and the other waits for the confirm, then skips it. A claim left unconfirmed for 30 s (its replica died mid-projection) is taken over. A projection that fails gives its claim back, so the resend projects it. |
  | `> projected_pos + 1` | The agent no longer holds the frames in between, because it always sends from its lowest unacked position, in order. The gap is logged, and the frame is handled as the next one. |

  A frame whose projection is refused (an unknown state, another agent's task) still counts as
  projected. Otherwise the agent would resend it forever and hold the watermark back.
- **`JOURNAL_ACK {journal_session, pos}`** (server → agent) is cumulative: "projected up to `pos`".
  It is debounced, and sent at once after a terminal report. Once a connection has sent a numbered
  frame, the server stops sending `EVENT_ACK` on it.
- **The agent persists what it retains.** A frame recorded but not yet acked must survive a restart.
  - The Rust agent keeps a local SQLite file with the `journal` and `journal_sync (session_id,
    acked_pos)` tables. `acked_pos` is only ever raised.
  - The Python agent must at least keep its retained frames across a session change and a restart.
  - Acked frames may be pruned locally after a retention window (default 7 days).

## After a restart

- Once `INIT` is received, the new process first sends the **earlier sessions'** unacked frames,
  in `(session created, pos)` order, each with its own `journal_session`. Then it sends its own.
- When the new session registers, the server has already handled the earlier session's in-flight
  tasks: a plain task ends `LOST`, a workflow is resumed (see [workflows.md](workflows.md)).
- **LOST is final.** A terminal report arriving after it (from an earlier session, or this one after
  a partition longer than the grace window) is kept as a `LATE_REPORT` event beside it. For any
  other outcome the server wrote itself (a terminal `TaskEvent` without `agent_pos`), a terminal
  report from an earlier session still **replaces** it; it never replaces one the agent reported.
- The server records no non-terminal report (`LOG`, `PROGRESS`, `YIELD`, `EFFECT`, …) on a task
  that is already done.
- A `SHELVE` from an earlier session is ignored: the value died with that process.

## Shelving

- **`shelve(value)` is synchronous.**
  - It mints `resource_id` (uuid4 hex), stores the value in the agent's local shelf, and sends a
    numbered `SHELVE {ref, identifier, resource_id, label?, description?}` with `ref = resource_id`.
  - The reference it returns is `{"__identifier": …, "object": resource_id}`.
  - Nothing is replied (`JOURNAL_ACK` covers it).
- **Dropping** a value (on `COLLECT`, or on its own) removes it from the shelf and sends a numbered
  `UNSHELVE {ref, drawer: resource_id}`.
- **The server** upserts the drawer on `(agent's shelve, resource_id)`. `COLLECT`, `UNSHELVE` and the
  GraphQL drawer operations name it by `resource_id`. Drawers are cleared when a new session
  registers, and kept when the same session reconnects.

## Server storage

| row | journal fields |
|---|---|
| `TaskEvent` | `step`, `agent_pos`, `agent_ts`; kind `EFFECT` with `effect` and `value` |
| `Task` | `parent_step`; unique on `(parent, parent_step)` |
| `Patch` | `step`, `agent_pos`, `agent_ts`; unique on `(session, global_rev, state)` |
| `Session` | `projected_pos` |

The history of a task is its `TaskEvent`s and `Patch`es ordered by `step`, plus its child tasks by
`parent_step`. The history of a session is its rows ordered by `agent_pos`.

## Served agents (local, not part of the wire)

An agent that serves its own HTTP/websocket API (the Rust `serve` module, `arkitekt-fastapi`) may
keep a local journal of its session. It can offer `/journal` routes, state at a position, and
websocket resume with `resume_after`. These are local debugging features: they use the same
`pos`, but the server neither needs nor mirrors them.

# Agent reports: order, dedup and task history

> Code references are to takt's `facade` crate, `takt/crates/facade/src/`, unless a path
> says otherwise.

This is the contract between the rekuest server and every agent that reports over the agent socket:
the Rust agent (`rekuest` crate in `arkirust`) and the Python agent (`arkitekt-runtime` for numbering,
the gate and `Task`; `rekuest` for the wire). On the server's side the contract is takt's:
`message_router.rs` and `persist/positions.rs`.

The frames are the types of the `rekuest-protocol` crate, which takt and the Rust agent share
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
- the state frames: `SESSION_INIT`, `STATE_UPDATE`, `STATE_SNAPSHOT` (and `STATE_PATCH`, from
  agents that predate updates);
- `LOCK`, `UNLOCK`, `SHELVE`, `UNSHELVE`.

`UNLOCK` names the task that held the lock in `task`. `SHELVE` names the task that shelved the value in
`task`, or omits it when no task shelved it.

| field | |
|---|---|
| `pos` | 1, 2, 3, … per session, with no gaps. `(journal_session, pos)` is the frame's durable key. |
| `journal_session` | The session `pos` belongs to: the `session_id` the process registered with. (It isn't called `session_id` because `STATE_PATCH` already has that field.) |
| `agent_ts` | When the agent recorded the frame, in epoch seconds (float). |
| `task_step` | Only on frames of a task (`STATE_UPDATE`: the changing task; `UNLOCK`: the holder; `SHELVE`: the task that shelved, if any). The value is 1, 2, 3, … per task. It isn't called `step` because `ASSIGN` already has a `step` flag. |

**Not numbered:**
- `REGISTER`, `HEARTBEAT_ANSWER`, the agent's own acknowledgements (`COMMAND_ACK`, `JOURNAL_SKIP`), and
  the two requests that are safe to lose: `PROBE_REQUEST` (a probe is ephemeral) and
  `STATE_REVISION_REQUEST` (a read). Everything else a task sends is numbered, the requests it makes
  included (`ASSIGN_REQUEST`, the control requests) and what it asks a person (`HELD`, `INFORM`).
- A **probe's task reports**: frames whose `task` has the id prefix `p-` (an `ASSIGN` with
  `probe: true`). Probes are ephemeral by design: the server keeps them in redis and never in the
  database, so they have no durable key and are never retained or resent.
  - State frames are the exception. A `STATE_UPDATE` a probe caused is still numbered, because the
    `global_rev` chain must not have holes. It carries no `task_step`, and its `task` stays the
    probe id.

**Task steps also number child calls.** A task that calls another action takes the next
`task_step` for it and sends it on `ASSIGN_REQUEST` as `parent_step`, next to `parent`. (It isn't
called `task_step` because that name is the numbered frames' own stamp.) The request is
numbered in the session like a report, and carries no `task_step` of its own: it belongs to no task
of this agent. Its answer comes back as a command (below), queued before the request's position is
confirmed, so a request the server took is never left without its answer and a resend of it is
skipped. The server stores the step as the child's
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
4. A `STATE_SNAPSHOT` at revision N comes before the update that reaches N, and already contains it.

## State updates

A state changes by **updates**. An update takes the state from one revision to the next, and
whoever follows the state sees an update whole or not at all.

```json
{"type": "STATE_UPDATE", "id": "…", "state": "stage", "global_rev": 41,
 "ops": [{"op": "replace", "path": "/x", "value": 10}, {"op": "replace", "path": "/y", "value": 20}],
 "task": "42", "pos": 17, "journal_session": "…", "agent_ts": 1790000000.25, "task_step": 5}
```

- `ops` are RFC 6902 operations, applied in order. One operation on a state is one update however
  many operations describe it (`moves.update({...})` is one per key), and so is everything a task
  does inside `with task.updating(state):`.
- `global_rev` is the agent's state revision once the update is applied: one revision per update.
- The session it belongs to and when it was made are the frame's own (`journal_session`,
  `agent_ts`). `lifecycle` and `qualifiers` say what a patch said before: they are the update's.
- The server stores one `Patch` row per update (`ops`), in the transaction that counts the frame's
  position, and the patch feeds carry it whole: a client applies the operations of one event
  together before it shows the state.
- `STATE_PATCH` (one operation, with its own `session_id`, `ts` and `task_id`) is what agents sent
  before, and is still taken: it is stored as an update of one operation.

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
  | `= projected_pos + 1` | **The watermark moves in the transaction that projects the frame** (`persist/positions.rs`, `advance`): `projected_pos = pos WHERE projected_pos = pos - 1`, first in that transaction. It takes the session's row lock, so when two replicas get the same frame the second waits for the first to commit and then finds the position taken. A projection that fails rolls the watermark back with it, so the resend projects it. See "One transaction" below. |
  | `> projected_pos + 1` | A gap. The frame is **not taken**: the server answers `JOURNAL_NACK {journal_session, expected}` (once per gap) and the agent sends again from `expected`, in order. |

  Only the agent passes a position over: if it no longer holds `expected` (its journal file is gone)
  it says `JOURNAL_SKIP {session, first, last}`, the server logs the loss as an error and goes on
  after `last`. (Over the HookAgent HTTP intake there is nobody to ask again: a gap is passed over
  there, with a warning, as before.)

  A frame whose projection is refused (an unknown state, another agent's task) still counts as
  projected. Otherwise the agent would resend it forever and hold the watermark back.
- **`JOURNAL_ACK {journal_session, through}`** (server → agent) is cumulative: "projected through
  this position". It is debounced, and sent at once after a terminal report. An unnumbered frame gets
  no ack. (`through`, not `pos`: a frame's own `pos` is where it stands in its sender's stream.)
- **The handshake says where both sides stand.** `REGISTER` lists the `sessions` the agent still holds
  frames of, and `INIT` answers with `positions {session: projected_pos}`: the agent drops what is at
  or below and sends the rest. `REGISTER` also carries a `nonce`, new per connection attempt, which
  `INIT` says again: an `INIT` that names another connection (one gone since) is ignored.
- **A report is one event.** A `TaskEvent` records the report's `(agent_session, agent_pos)` under a
  unique constraint: a report projected twice finds its event there and writes nothing.
- **The agent sends through a window.** At most a fixed number of numbered frames (256) are sent
  and not yet acknowledged; the rest wait in the retained backlog, in order, and go out as
  `JOURNAL_ACK`s come in. The acknowledgements are the credit: what the server holds of one agent
  is bounded, and a backlog built up while the server was away arrives at the pace it is taken.
  A `JOURNAL_NACK` has the session it names sent again from `expected`, not everything retained.

### One transaction

Frames come in two kinds (`message_router.rs`):

- A **light** frame's whole projection is a few writes: the reports a running task streams
  (`LOG`, `PROGRESS`, `YIELD`, `EFFECT`) and the agent's state and locks (`STATE_UPDATE`,
  `STATE_SNAPSHOT`, `SESSION_INIT`, `LOCK`, `UNLOCK`). It is projected inside the watermark's
  transaction: recorded exactly when its position is counted, never twice, never in part. The
  frames that arrived together are projected together (`route_light`): one commit for a run of
  them, so the ops of one state update that were sent back to back become visible at once.
- Any other frame (the end of a task, a hold, a request) runs through the task machinery, which
  commits on its own. The watermark's transaction is held open around it: its lock keeps a second
  replica out, the answer to a request is kept for the agent in that same transaction, and a
  failure rolls the watermark back. What such a projection writes is idempotent on the report's
  own key (the event's `(agent_session, agent_pos)`, a child's `(parent, parent_step)`), for the
  one case left: the process dying between the projection's commit and the watermark's.

What a projection tells whoever follows (the feeds, a caller's wake) is told after the commit.

## Commands: the other direction

What the server sends an agent and must not lose is numbered the same way, in a stream of its own:

- A **command** carries `pos`: 1, 2, 3, … per agent, in order. (What an agent is sent may skip a
  position: an `ASSIGN` whose task has ended meanwhile is not sent, and one withdrawn because its
  task was assigned anew is gone.) Commands are `ASSIGN`, `CANCEL`,
  `INTERRUPT`, `PAUSE`, `RESUME`, `DECIDE`, `COLLECT`, `BLOK_REFUSED`, the answers to numbered requests
  (`ASSIGN_RESPONSE`, `CONTROL_RESPONSE`) and every `…_EVENT` mirror of a task the agent assigned.
- The server keeps a command in its database (`facade_command`, `consumers/agent_queue.rs`) until
  the agent's cumulative **`COMMAND_ACK {through}`** covers it. Sending forgets nothing.
- **A command is written in the transaction that makes it true**: the mirror of a task event in the
  statement that inserts the event, the answer to a request with the request's position. There is
  no moment in which the task says one thing and the agent's commands another.
- **A position is given when a command is sent, not when it is written**: the connection holding
  the agent's lease numbers what waits (`agent_queue::number`), in the order written; only the
  answer to a request for a child goes ahead of that child's events, which were written first. A command that commits late is numbered late, so an agent never meets
  a position below one it has handled.
- The mirror of an event is a row that names the event; the frame is built from the event when it
  is sent. Redis carries only what is worth nothing later: a probe's frames, a question, an order
  to a connection, and the wake that tells a connection a command was committed.
- `REGISTER` carries `commands_seen`, the highest position the process has handled (0 for a fresh
  process): the server sends from the one after. So a command written into a connection that died is
  sent again, and the agent, which hands a position on once, does not carry it out twice.
- Not commands: `INIT`, `HEARTBEAT`, `JOURNAL_ACK`, `JOURNAL_NACK`, `PROTOCOL_ERROR`, `KICK`, `BOUNCE`,
  the answers to unnumbered requests, and every frame of a probe, which travels on a lane of its own
  ahead of the commands and at most once.
- `KICK` and `BOUNCE` are **orders to a connection**, not to the agent: leaving is all an agent does
  with one, so nothing would ever acknowledge it. They travel on the probes' lane, and one given
  before a connection began is not sent to it (`agent_queue::is_connection_order`). An agent that
  meets one at a position (a server from before this) counts the position as seen.
- **An assign that was never written is an outcome too.** An `ASSIGN` follows its task's commit
  (dispatch), so a process can die in between. A `COMMAND_ACK` records the pickup in the statement
  that forgets the command, so an assign that is neither picked up nor among the commands never
  was written: its task ends `LOST` (`reconcile::reconcile_unreachable_assigns`). An agent that
  has seen more commands than the database numbered (it was restored from an earlier state) has
  what waits numbered anew, behind what it has seen.
- **A new process is owed less.** When another process takes an agent over, what only its
  predecessor could use is dropped: the answers to its requests, the events of the tasks it
  assigned, what steered the tasks it had picked up. Work nobody picked up yet stays, with what
  steers it.
- A child's mirror names its `event` (the `TaskEvent` id); a mirror seen twice (a replay for a child
  found again, overlapping what arrived live) is the same event. There is no `seq` any more, on
  mirrors or on reports.

**Delivered once, handled once, never tried again.** Sending a frame again is how "once" is kept
over a connection that can drop: the receiver knows it by its position and does nothing. It is not
a retry. Whatever handling a frame leads to is its outcome, a failure included: it is reported once,
and trying again belongs to whoever called (see "Pickup, and asking" in
[agent-protocol.md](agent-protocol.md)).

- A command the agent cannot handle fails its task (`CRITICAL`) and is acknowledged like any other.
- A control for a task the agent does not have is acknowledged and answered with nothing: an agent
  ends no task it knows nothing of. What became of such a task is learned by asking.
- A task sent again (`on_lost`: `RESUME`, `RESTART`) has the assign it was first given withdrawn
  from the stream: a process that never saw it is given only the new one.
- A request whose parent task has ended is answered with an error.
- A database or redis failure on the server is not the frame's fault: the connection is closed with
  a code the agent reconnects on (3005), and the frame, never handled, is delivered again. That
  holds for the handshake as well: a `REGISTER` the server could not serve is not a refused one.
  A frame the server cannot read at all is schema drift, and closes for good.

An agent says it speaks this with `protocol_2` in `REGISTER`'s `features`. The server refuses one
that does not.
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
| `Patch` | `ops`, `step`, `agent_pos`, `agent_ts`; unique on `(session, global_rev, state)`: one row per update |
| `Session` | `projected_pos` |

The history of a task is its `TaskEvent`s and `Patch`es ordered by `step`, plus its child tasks by
`parent_step`. The history of a session is its rows ordered by `agent_pos`.

## Served agents (local, not part of the wire)

An agent that serves its own HTTP/websocket API (the Rust `serve` module, `arkitekt-fastapi`) may
keep a local journal of its session. It can offer `/journal` routes, state at a position, and
websocket resume with `resume_after`. These are local debugging features: they use the same
`pos`, but the server neither needs nor mirrors them.

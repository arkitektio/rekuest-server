# Caller sessions: the human side of the wire (`/session`)

> **Status: implemented (v1, 2026-11-23)** in `crates/facade/src/session/` (frames, topics, calls,
> registry, connection, the dead man sweep), tested in `crates/facade/tests/session.rs` with the wire
> contract `tests/fixtures/session_wire_examples.json`; the orkestrator's client is
> `rekuest/session/`. Not built yet: `ASK` (§7), the shared request types for `/internal/*` (§1),
> the benchmark (§11). Decided: takt owns caller sessions; the
> WebSocket below is the primary way a person's app calls actions and follows tasks, agents and
> states; GraphQL call mutations become a secondary adapter onto the same logic. One socket per
> app window. The frontend is (nearly) stateless: takt keeps what a session watches and how far
> it got; the window keeps its session id and one number. Tasks never depend on a window unless
> they ask to (a dead man switch, §7). Open questions are listed at the end.

An agent talks to rekuest over `/agent` ([agent-protocol.md](agent-protocol.md)) and assigns
children over the same socket ([caller-protocol.md](caller-protocol.md)). A person's app (the
orkestrator, one connection per window) has had no socket of its own: it calls through GraphQL
mutations, which the Python server forwards to takt's internal API, and it follows tasks through
GraphQL subscriptions, which the Python process relays from the channel layer one generator per
subscriber. This document is the replacement: a **caller session** on takt.

## 1. Where it sits

All front doors end in one caller core (`persist/caller_ops.rs` and the request handlers next to
it). A front door only turns its input into caller requests and their answers back.

| Front door | Who | What it carries |
| --- | --- | --- |
| **`/session` (WebSocket)** | a person's app window | calls, control, decisions, watches: **primary** |
| `/agent` (WebSocket) | agents | execution, and sub-assignment (`caller-protocol.md`) |
| `/internal/*` (unix socket) | the Python GraphQL server | the same requests as `/session`, one at a time |
| `/agent/http` | hook agents, hub services | the agent frames, HMAC-signed |

**GraphQL becomes an adapter.** *(v1: the session's calls and the internal API's handlers call the
same backend functions — `assign_with_status`, `request_control`, `decide_remembering`, `inquire`,
the probe backend — so validation, ownership and provenance are already one implementation; the
shared request types below are still to do.)* `/internal/*` takes the **same request types** as the session
frames and goes through the same routing function, the way `message_router::route` serves both
the agent socket and the HTTP intake. A GraphQL `assign` is a one-shot session: one request in, one
answer out, no watches. There is then one implementation of validation, idempotency, ownership and
provenance, and the two paths cannot drift. The answer carries the task record takt already has,
so the Python resolver need not read it again.

GraphQL **queries** (catalogue, lists, detail pages) stay in Python. GraphQL **subscriptions** are
frozen: kept for their current consumers (Python clients, data-borne blok queries), not extended.
The orkestrator moves off them.

## 2. Connection and session lifecycle

```
client                                        takt
  │── HELLO{token, session?, last_pos?} ───────▶│  verify the lok token (authentikate)
  │◀─ WELCOME{session, resumed, limits} ────────│  session registry entry in redis
  │── WATCH / ASSIGN / … ──────────────────────▶│
  │◀─ DATA{pos} / RESYNC{pos} / ASSIGNED{pos} ──│  every push numbered per session
  │── PING{pos} ─▶ ◀─ PONG                       │  liveness; the ping carries the newest pos seen
  ╳ connection lost                              │  session kept for `session_grace`
  │── HELLO{token, session, last_pos} ─────────▶│  resume: takt replays from its own cursors (§5)
```

- **One socket per window.** A window is one session. Two windows of the same user are two
  sessions of one caller `(client, user, organization)`.
- **The session registry is redis, not Postgres:** session id, caller, client info (app, window
  label), connected / last seen, the interest set **with a cursor per topic**, the recent pushes'
  `(pos, topic, cursor)` (§5.4), and what the session owns (its probes, its dead man tasks). It is
  ephemeral by nature: no task's history depends on it, and losing it (a redis restart) only
  costs the open windows a fresh start. The one thing a task may depend on, its dead man switch,
  is kept on the task in Postgres, not here (§7).
- **What the window keeps:** its session id, the newest `pos` it received, and the requests it is
  still waiting on (it holds those anyway). Nothing per topic. A reloaded window keeps nothing at
  all: it starts a new session and gets snapshots.
- **`session_grace`** (default 60 s): a session whose socket is gone is kept that long. A `HELLO`
  naming it within the grace takes it back (`resumed: true`) with everything it watched; after it,
  the session is ended (§7) and a `HELLO` naming it gets a new, empty session (`resumed: false`):
  the window watches again from snapshots.
- **The token** is checked at `HELLO` and again on `REAUTH{token}` before it expires. An expired
  token closes the socket with a code the client reads as "sign in again", never as "retry".

## 3. Frames

All frames carry `type` and `id` (the client's id for a request; the server's for a push), as the
agent protocol's envelope does. JSON by default; MessagePack when the client asks for the
`rekuest.session.msgpack` subprotocol (large state values).

**Client → server**

| Frame | Fields | Answer |
| --- | --- | --- |
| `HELLO` | `token`, `session?`, `last_pos?`, `client_info` | `WELCOME` |
| `REAUTH` | `token` | `OK` / close |
| `WATCH` | `topic` | a `RESYNC` (the snapshot), then live `DATA` |
| `UNWATCH` | `topic` | `OK` |
| `ASSIGN` | `reference`, targeting, `args`, `hooks?`, `step?`, `deadman?` | `ASSIGNED{task, created, record}` / `REFUSED` |
| `CANCEL` / `INTERRUPT` / `PAUSE` / `RESUME` | `request`, `task`, (`auto_interrupt`, `step`) | `ACCEPTED` / `REFUSED`; the outcome as task events |
| `DECIDE` | `request`, `task`, `hold`, `values` | `ACCEPTED` / `REFUSED` |
| `INQUIRE` | `request`, `task` | `ACCEPTED`; ended tasks' events re-sent |
| `DEADMAN` | `request`, `task`, `seconds \| null` | `ACCEPTED` / `REFUSED`; arms or disarms the switch (§7) |
| `PROBE` / probe control | as the GraphQL `probe` mutation | `PROBE_STARTED{probe}` / `REFUSED` |
| `PING` | `pos` (the newest push received) | `PONG` |

**Server → client** (every push carries `pos`, numbered per session)

| Frame | Fields | Meaning |
| --- | --- | --- |
| `WELCOME` | `session`, `resumed`, `limits` | the session is open |
| `DATA` | `pos`, `topic`, `item` | one item of a watched topic, in order |
| `RESYNC` | `pos`, `topic`, `reason`, `snapshot` | replace what you hold for this topic (§5) |
| `ASSIGNED` | `pos`, `request`, `reference`, `task`, `created`, `record` | the task, with its live record: no read after |
| `ACCEPTED` / `REFUSED` | `request`, `error?` | the answer to a request |
| `ASK` | `task`, `hold`, `prompt` | a held task waits for this person (§7) |
| `CLOSING` | `reason` | the server is about to close (deploy, token, limit) |

## 4. Topics and their cursors

A topic is something to follow. A cursor is how far the session has it; **takt keeps it**, per
session and topic, the window never sees it. Each topic has **one** cursor kind, chosen so that
"everything after this" is a question the database can answer exactly.

| Topic | Items | Cursor | Source of truth |
| --- | --- | --- | --- |
| `task:{id}` | the task's events | `seq` (per-task, see §5.1) | `facade_taskevent` |
| `children:{root}` | changes of the tree's tasks | per-task `revision` map | `facade_task` |
| `tasks:mine` | changes of the caller's root tasks | none: snapshot on (re)watch | `facade_task` |
| `tasks:org` | the organization's root tasks | none: snapshot on (re)watch | `facade_task` |
| `agents:org` | agent create / update / delete | none: snapshot on (re)watch | `facade_agent` |
| `agent:{id}/tasks` | the agent's task changes | none: snapshot on (re)watch | `facade_task` |
| `state:{agent}/{interface}` | patches of one state | `(agent_session, global_rev)` | `facade_patch`, `facade_snapshot` |
| `states:{agent}` | patches of all its states | `(agent_session, global_rev)` | as above |
| `probe:{id}` | a probe's events | per-probe counter | redis, while the probe lives |

Lists (`tasks:*`, `agents:org`, `agent:*/tasks`) carry no cursor on purpose: what a list needs
after a gap is its **current** rows, not every change it missed. A resume sends the window's
rows (their `revision` included) as a `RESYNC`, then live changes.

## 5. Reconnect guarantees

This is the part a client builds on. Stated per topic kind, then how each is kept.

**The guarantees**

1. **Event streams (`task:{id}`) are gap-free and in order, across reconnects.** A window that
   resumes its session (`HELLO{session, last_pos}` within `session_grace`) receives every event
   recorded after the last one it received, exactly in recording order, then the live ones, with
   no gap at the seam. Duplicates can occur at the seam and are recognisable by `seq`.
2. **States (`state:*`, `states:*`) are gap-free within one agent session.** On resume the window
   receives every patch after the last one it received, in order. If the agent restarted meanwhile
   (a new agent session), or the patches were pruned, it receives a `RESYNC` with the current value
   instead.
3. **Lists converge.** After a resume the window holds the list's current rows; changes in between
   are not replayed one by one, only their result.
4. **Past the grace, nothing is half-kept.** A session that expired is gone with its watches; the
   window starts a new one and gets snapshots. It never has to merge an old view with a new one.
5. **A request sent is answered once in effect.** An `ASSIGN` re-sent with the same `reference`
   returns the same task (`created: false`); a control or decide re-sent with the same `request`
   id returns the first answer. A window may therefore re-send every unanswered request after a
   reconnect without doubling work.
6. **Nothing is silent.** When a guarantee cannot be kept (history pruned, replay too long, agent
   session changed), the server says so with a `RESYNC` naming the reason. The window never has
   to guess whether it missed something.

**Not guaranteed:** intermediate `PROGRESS` values (progress is a level: replay and live both send
the newest per run of progress, §6); a probe's events after the probe expired (probes are not
history); the intermediate states of a list.

### 5.1 Prerequisite: a commit-ordered per-task sequence

Event ids are **not** a safe cursor. They are drawn from a sequence when the row is inserted, but
the row becomes visible when its transaction commits, and two writers of one task (an agent's
report, a control request, a sweep marking it lost) can commit in the other order. The GraphQL
`taskEvents` subscription already notes this ("events of one task may be published out of id
order") and dedupes by id, but its `after` floor still drops an event whose id is below the
floor and that committed after the floor was seen.

So every task event gets a **`seq`**, numbered per task from a counter on the task's row. Today
`insert_event` inserts the event first and updates the task row after it (to keep `latest_*`),
so the row lock comes too late to number the event. The statement is turned around: lock the task
row and take the next `seq` from its counter first, then insert the event with it, then keep
`latest_*`. The lock is held to commit, so the next writer of the task waits and takes the number
after. A resent report (the `ON CONFLICT … DO NOTHING` path for the same journal position) must
not use a number up: the check for the existing event comes before the counter moves. Per task,
`seq` is then contiguous (1, 2, 3, …) and in commit order. That gives two things an id cannot:

- **a safe cursor:** "after `seq` n" misses nothing, whatever the commit order of other tasks;
- **gap detection:** a live item with `seq` ≠ last + 1 means something was not delivered (§5.3).

This is a schema change (a column on `facade_taskevent`, a counter on `facade_task`, both
migrated by the rekuest server; existing events numbered once by id order) and the GraphQL
`taskEvents(after:)` should move to it too.

Task `revision` is already incremented under the row lock on every transition (`reports.rs`,
`transitions.rs`, `reconcile.rs`), so it is a sound per-task cursor for task *changes*. State
patches are numbered per agent session and unique per `(session, global_rev, state)`, so
`(agent_session, global_rev)` is sound for states.

### 5.2 Replay from a cursor

Whenever takt has to bring a topic up to date from a cursor (a resume, §5.4; a gap, §5.3):

1. **Join the live feed first**, buffering what arrives.
2. **Read the history after the cursor** from Postgres (events by `seq`, patches by `global_rev`).
3. **Send the history**, then the buffered live items whose cursor is past the last history item,
   then live. The seam can only duplicate, never drop.

This is the order the GraphQL `taskEvents` subscription already uses (join, then read).

The replay is bounded by `limits.replay_max` (items). Past it, the server does not stream a long
backlog into a window that only shows the tail: it sends a `RESYNC` (`reason: "too_far_behind"`)
with the current record and the last `replay_max` items. The same `RESYNC` follows a cursor whose
history is gone (`"pruned"`, the retention sweeps) and a state cursor from an earlier agent
session (`"agent_restarted"`).

A new `WATCH` has no cursor: it is answered with a `RESYNC` (`"new"`) holding the snapshot, and the
cursor starts there.

### 5.3 Gaps while connected

The live path (redis fan-out between replicas) is best effort, as the channel layer is today. The
session does not rely on it alone. Per watched topic it keeps the last cursor it sent, and when a
live item does not follow it (`seq` ≠ last + 1, `global_rev` skipping a patch), it reads the
missing items from Postgres before sending the live one. A dropped broadcast costs a query, never
a hole in the client's view.

A gap at the end of a topic (the last broadcast lost, nothing after it) has no next item to reveal
it. So takt checks its own watches every `limits.watch_check` (default 30 s): per watched task it
compares the cursor sent with the task's counter in Postgres (one query for all of a session's
tasks) and sends what is missing. `INQUIRE` stays for a person who wants to know now.

### 5.4 Resume: the window says one number

takt has to know how far the window really got, not how far it sent: what was in flight when the
socket dropped never arrived. So every push carries the session's `pos`, and the window reports
the newest `pos` it received, on every `PING` and in the resuming `HELLO`. That is all it keeps.

takt keeps, per session, the topics' cursors as of the newest **reported** `pos`, plus a small
record of the pushes since then: `(pos, topic, cursor)` each, not their payloads (those are in
Postgres). On `HELLO{session, last_pos}`:

1. It moves each topic's cursor to that of the newest push at or before `last_pos`.
2. It replays every watched topic from its cursor (§5.2), lists as a `RESYNC`.
3. It re-sends the answers (`ASSIGNED`, `ACCEPTED`, `REFUSED`) pushed after `last_pos`; those are
   kept with their payload, being small and not in Postgres.

A topic watched so recently that its first `RESYNC` was still in flight (its `pos` after
`last_pos`) has no cursor to roll back to: it gets a fresh `RESYNC`.

The record is bounded (`limits.resume_window` pushes). A window that reports so rarely that it
falls out of the record gets its topics resent as `RESYNC` (`"too_far_behind"`); with a ping every
few seconds that does not happen.

### 5.5 Requests in flight

- **`ASSIGN`** is idempotent on `(caller, reference)`, the existing unique constraint. The
  orkestrator generates the reference before sending and keeps it until `ASSIGNED` arrives (it
  waits on the request anyway); after a reconnect it re-sends every unanswered `ASSIGN`.
- **Control, decide, inquire** carry a `request` id. The answer is kept in redis under
  `(session's caller, request)` for `limits.request_memory` (default 10 min): a re-send within it
  gets the kept answer instead of being applied again. Today a second cancel of a task is refused
  as "already cancelling", which a client after a reconnect cannot tell from a real refusal.
- **Answers are bound to the caller, not the socket:** an `ASSIGNED` that could not be sent before
  the socket closed is not lost, because the re-sent `ASSIGN` returns it again.

### 5.6 What the window does

- Remembers its session id and the newest `pos` it received; reports the `pos` on every `PING`.
- Applies items idempotently (by the item's `seq` or revision), so the seam's duplicates are
  no-ops.
- On reconnect: `HELLO{session, last_pos}`, then re-sends its unanswered requests. It does not
  re-send its watches: the session has them. On `resumed: false` it watches again.
- On `RESYNC`: replaces what it holds for that topic.
- A reloaded window keeps nothing: new session, snapshots.

In the orkestrator this sits where deltas already become cache writes (`createTaskEventQueue`,
`liveStateStore`, `TaskUpdater`, `useRun`); components and the blok runtime (`taskRoot`, live
state) keep reading the same cache.

## 6. Delivery: coalescing and backpressure

- **Progress is a level.** Between two other events of a task, only the newest `PROGRESS` is sent,
  live (as `progress_gate` already does, per task, at most every 100 ms) and in replay. Every report
  is still recorded.
- **State patches** of one state inside one delivery tick (`limits.tick`, default 50 ms) are sent
  as one `DATA` with their operations in order; the cursor is the last `global_rev`.
- **Backpressure:** when a session's send buffer passes `limits.buffer_max`, the server stops
  sending items of the topics that are furthest behind and, once the buffer drains, sends those
  topics a `RESYNC` (`"slow_consumer"`). A slow window gets a fresh view, never an unbounded
  backlog, and never takes the replica's memory with it.

## 7. What takt can do because it owns the sessions

- **Asking the right person** *(not built in v1; the frame is reserved).* A task that becomes HELD waiting for a person is sent as `ASK` to
  that person's live sessions, instead of waiting for someone to open the page. No live session:
  it stays a notification, as today.
- **Tasks do not depend on windows.** Closing, reloading or crashing a window changes nothing
  about the tasks it started: they run on, and their history is there when any window looks
  again. The session holds no task state the task needs.
- **Dead man switch, opt-in per assign.** `ASSIGN{deadman: <seconds>}` ties the task to the
  session that assigned it: if that session is gone (no socket, no resume) for longer than
  `deadman`, takt cancels the task, with `auto_interrupt` so a silent agent is interrupted too.
  The cancel is recorded with its reason (`CANCELLING` naming the session), so the log says why it
  stopped. For runs that must not go on unwatched: a stage moving under manual control, a live
  acquisition, a pump. Without the flag a task is never cancelled for a window.
  - The clock is the session's, so a resume within `deadman` keeps the task, and a second window
    of the same person does **not** keep it alive: only the assigning session counts.
  - Any session of the task's owner (the existing ownership gate: the caller that assigned it)
    may disarm it, re-arm it, or take it over (`DEADMAN{task, seconds | null}`): "keep running,
    I am closing the laptop", or another window adopting the run after the first one crashed.
    The switch then follows the session that sent the frame.
  - **It is durable and fires only on positive absence.** The arming (seconds and the owning
    session) is a column on the task in Postgres, and each session's last-seen time is written
    there on a coarse beat (every few seconds), not only in redis. A sweep, like the existing
    control escalation, cancels a running task whose owning session was last seen more than
    `deadman` ago **while takt was serving**: a session missing from redis is not absence (a redis
    restart fires nothing), and time when no takt replica was up does not count (a deploy or an
    outage longer than `deadman` fires nothing). Replicas share the sweep, as they share the
    other sweeps, so one replica dying fires nothing either.
- **Session-owned probes.** A session's probes end with the session (after `session_grace`), as if
  each had a dead man switch of `session_grace`.
- **Demand-driven streaming (later).** takt knows which states and tasks anyone watches. It can
  tell an agent that nobody watches a state, so high-rate state patches are only produced while
  someone looks, saving work in the agent, takt, redis and every client.

## 8. Calls and provenance

The session's caller is the human the token names, so a session may assign **roots**.
[provenance.md](provenance.md) currently says roots originate solely from the GraphQL `assign`
mutation; that becomes "roots originate from a human-authenticated caller session", with the
GraphQL mutation as one kind of session (a one-shot one, §1). The lineage rules (children inherit
the root human) do not change. The agent socket stays unable to assign roots.

## 9. Replicas and fan-out

Sessions and the agents whose events they watch are generally on different takt replicas. Live
items cross replicas on the channel layer, as they always did: each watched topic gets a channel of
its own (`kante::ChannelLayer::subscribe`) joined to the groups the GraphQL subscriptions listen on
(`root_tasks_caller_*`, `task_events_*`, `child_tasks_*`, `agent_tasks_*`, `agents_for_*`,
`patches_state_*`, `patches_agent_*`, `state_*`, `probe_events_*`), so a message's topic is the
channel it came in on, and nothing new is published. Groups are re-joined every hour, as the agent
socket's caller group is, and left on `UNWATCH` and close. Whatever writes to those groups (takt, or
the Python server) reaches sessions too. Postgres stays the source for every cursor, so the fan-out
carries no guarantee of its own (§5.3).

## 10. Scoping

`WATCH` is authorised once, when it is made: the topic's object must be visible to the caller, by
the same rules as Python's `scoped_get` (organization of the task's agent, of the agent, of the
state's agent). These rules then exist in two languages, so they get shared conformance cases,
like the blok `validation_cases.json` that keeps rekuest and takt in step.

## 11. Gate: measure before and after

takt exists because a benchmark showed agents kicked under GraphQL load (README). This gets the
same treatment: the conformance benchmark gains a UI load (N windows × M watched tasks and
states) and reports event-to-client latency, rekuest CPU and takt CPU, against GraphQL
subscriptions first and the session second. The read side (watches) ships only if it wins there.

## 12. Order of work

0. The benchmark (§11) against today's GraphQL path.
1. The per-task `seq` (§5.1), used by GraphQL `taskEvents` as well: it fixes today's subscription
   too.
2. The session with watches, replay and `RESYNC` (§2–§6); the orkestrator's cache writers fed
   from it.
3. Calls over the session (§3, §8), `/internal/*` on the same request types (§1), provenance.md
   rewritten.
4. The dead man switch and session-owned probes, `ASK`, then demand-driven streaming (§7).

## Open questions

1. `session_grace` and `request_memory`: 60 s and 10 min are guesses; the benchmark and real
   laptop sleeps should set them.
2. ~~Dead man switch while held~~ — decided: a HELD task is not cancelled (it is not running), and
   the GraphQL `assign` does not offer the switch (a one-shot session has no socket to lose).
3. Lists with a cursor: if a list's snapshot turns out expensive on every reconnect (large org
   task lists), a per-caller change feed with its own sequence would let lists replay too.
4. ~~Where the resume record lives~~ — decided as below: the record stays in the serving
   replica's memory; cursors as of the last reported `pos` go to redis on each `PING`. Writing `(pos, topic, cursor)` to redis on every push adds
   writes to the path this is meant to speed up. Alternative: the record stays in the serving
   replica's memory, and only "cursors as of the last reported `pos`" go to redis on each `PING`.
   A replica dying then widens the replayed overlap (duplicates, applied idempotently), never
   loses an item. Answers (`ASSIGNED`, …) go to redis either way.
5. Where the frame types live: beside `rekuest-protocol` in arkirust as their own crate, with
   generated TypeScript for the orkestrator and the same wire-fixture contract the agent protocol
   has.

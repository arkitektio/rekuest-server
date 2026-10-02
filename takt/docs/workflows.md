# Workflows, LOST, and resume

> Code references are to takt's `facade` crate, `takt/crates/facade/src/`, unless a path
> says otherwise.

What happens to a task whose agent dies depends on one property of its implementation:
`Implementation.execution`.

| What died | Who handles it | How |
|---|---|---|
| the agent of a **plain** task | whoever called | the task ends **LOST**; a caller gets `LostEvent` (an agent) or a LOST task event (GraphQL), and decides |
| the agent of a **workflow** | the server | the workflow is **resumed**: sent again with its journal, it replays what it recorded |
| a step inside a workflow | the workflow's code | the call raises `AgentLost`; `task.retry` / `task.hold` cover the usual answers |

Here "the server" is takt: the disconnect handling and the sweeps in `persist/reconcile.rs`.

The server re-runs nothing on its own. The one exception is a task that was never picked up
(no report, not even `STARTED`, which an agent reports before anything else): it is redelivered.

## LOST

A terminal kind: not failed, how it ended is unknown. The LOST event keeps what is known in its
`value`, for whoever decides:

| field | |
|---|---|
| `started` | whether the task ever reported `STARTED`. If not, nothing ran: sending it again is safe. |
| `last_progress` | the last progress it reported |
| `effects` | the implementation's `effects` (`NONE`, `REPEATABLE`, `UNKNOWN`, `IRREVERSIBLE`): informational, never the server's decision |
| `reason` | in words |

A task ends LOST when its agent was lost while it ran: after the grace window, or at once when a
new process takes the agent over. Never-picked-up work whose redelivery budget is spent, and
undelivered work of an agent that never came back, end LOST with `started: false`.

**LOST is final.** A later outcome from the agent (a partition that outlasted the grace window, a
resend from the dead process's journal) is stored as a `LATE_REPORT` event, a late result too, and
never replaces LOST: whoever called may already have acted on it.

## Workflows

Only a workflow may call other actions: `@app.workflow` registers an implementation with
`execution: WORKFLOW`, and a plain implementation's call raises `NotAWorkflowError`.

A workflow's code must be deterministic: outside values come in only through calls and the task
(`task.now()`, `task.random()`, `task.sleep()`, `task.record(fn)`, `task.hold()`), each recorded as
an `EFFECT` under a **key** (`NOW:1`, `RECORD:3`, or the caller's own).

**Calls are keyed too.** `AssignRequest.call_key` is what the parent calls the child: by default
the target, a hash of the args, and the occurrence (`atest.double:054b7d8d:1`); a flow engine passes
the node and its count. The server is idempotent on `(parent, call_key)`, checked before
`(parent, parent_step)`: concurrent calls take their steps in no fixed order, keys stay the same.

## Resume

When a workflow's agent dies, the server claims the task back to `QUEUED` and sends its `Assign`
again with `resume`:

```json
{"last_step": 5, "effects": [{"key": "NOW:1", "effect": "NOW", "value": 1790715513.22}]}
```

- Each effect helper looks its key up first. A recorded value comes back instead of a new one (a
  resumed `sleep` waits only until its recorded deadline); a value of another kind under the key
  raises `NonDeterministicWorkflow`.
- Each call is re-issued with the same key and finds its child. A **duplicate `AssignRequest`
  (`created: false`) is followed by the child's stored events** as mirrors: a finished child's
  result comes back without it running again; a running child's events so far come first, live
  ones after. The caller drops duplicates by `seq`.
- A call key found again must name the same call; if the resumed run names another action, the
  request is refused as `Nondeterministic workflow: …`.
- New reports continue after `last_step`.
- **The successor completes the journal.** The server's journal only has what it received; the
  successor adds the task's EFFECT frames it still holds unsent in its on-disk journal, and their
  steps. EFFECT events are unique per `(task, key)`, so the late resend of such a frame never adds
  a second value.

**Limits.**
- **Code pin.** `Task.code_hash` is the implementation's `code_hash` at dispatch (a hash of its
  source). If it changed, the workflow is not resumed but ends LOST ("code changed"). Only the
  function's own source is hashed: an edit to a helper it calls is not seen.
- **Resume cap.** `Task.resumes` counts resumes; after `MAX_RESUMES` (3, `persist/reconcile.rs`)
  the task ends LOST.
- A resent workflow whose agent never comes back ends LOST with `started: true`.

## Holds

`task.hold(message, lost=...)` pauses the task from the inside: `PAUSED` carries `message` and
`details` (for a lost step: its effects, last progress), stored on the event. A person resumes it
(`resume` mutation) or cancels it. Once resumed, the hold is recorded (`HOLD:n`), so a resumed run
does not hold again; a workflow that died while held holds again after its resume.

## Guards

`task.guard(handler.plate, "barcode")` watches a dependency's state across a resume. The first run
records the state's revision (`{session, global_rev}`, via `StateRevisionRequest` without `since`).
A resumed run entering again asks with `since`: the state has changed if, on the guarded paths,
a patch came from anything but the workflow's own call tree (its `dispense` changes the plate too),
or its agent restarted and set it up again (a new session). Then `StateChanged` is raised.

- Only a dependency resolved to one agent can be guarded.
- Changes while the workflow runs are not watched.

## Known gaps

- `PROGRESS` and `LOG` a resumed run repeats are recorded again (a second "Queued for running").
- A resumed workflow's history shows its first run's reports, then `QUEUED` ("sent again"), then
  the resumed run's reports.

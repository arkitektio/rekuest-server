//! Held work: what waits for a person before it may go on, and the decision that releases it.
//!
//! Two things wait.
//!
//! - **A child an agent asks for.** Under an `ASK` run, an `ASSIGN_REQUEST` creates the child
//!   `HELD` and hands nothing to its agent. A person approves it (`decide`), and only then is
//!   its Assign sent, with the person's answer as its `message`. Denied, it ends `DENIED`
//!   without ever running. The agent that asked cannot get around this: it cannot set the
//!   policy, and it may not decide.
//! - **A task that holds itself** (`HELD` from its agent, a workflow's `hold`). It waits on its
//!   agent for the server's `DECIDE`, which carries the answer back. A denial does not end it.
//!
//! Whether a run asks is its `policy`: a root's is set on its GraphQL assign, and every child
//! takes its parent's. Under `AUTO` nothing waits: a child is dispatched at once, and a task's
//! own hold is approved by the server the moment it arrives. Children the server makes itself
//! (init hooks) are never held.
//!
//! The `HELD` event's `value` says what waits, for whoever decides. The prompt is the agent's
//! own words; next to it is what the server knows for certain:
//!
//! ```json
//! {"kind": "assign", "prompt": {"title", "description"} | null,
//!  "action": {"id", "name", "hash"}, "agent": {"id", "name"}, "args": {...},
//!  "caller_task": "<parent id>", "caller_agent": {"id", "name"}}
//! ```
//!
//! The parent (and the root, when it is another task) gets a `HELD` event too, with the same
//! value plus `"held": "<child id>"`: whoever follows the run sees that it waits, and which
//! task to decide. A task's own hold is `{"kind": "self", "prompt", "details"}`. `DECIDED` is
//! `{"approved", "answer", "auto", "user"}` on the held task and on whoever got its `HELD`;
//! `DENIED` is `{"answer", "user"}`.

use chrono::Utc;
use serde_json::{json, Map, Value};
use sqlx::{PgConnection, Postgres, Transaction};

use crate::backend::{parse_id, ActionRow, BackendError, BackendResult};
use crate::caller_context::CallerContext;
use crate::context::Context;
use crate::messages::{AssignPrompt, ToAgent};
use crate::persist::reconcile::build_redispatch_assign;
use crate::persist::transitions::{self, insert_event, update_task_kind, Claim, NewEvent};
use crate::persist::PersistResult;
use crate::{signals, transport};

/// An agent's children wait for a person.
pub const ASK: &str = "ASK";
/// Nothing waits (auto-mode).
pub const AUTO: &str = "AUTO";

/// A root's policy as its assign gives it: `ASK` unless it says `AUTO`.
pub fn root_policy(given: Option<&str>) -> BackendResult<&'static str> {
    match given.filter(|p| !p.is_empty()) {
        None | Some("ASK") => Ok(ASK),
        Some("AUTO") => Ok(AUTO),
        Some(other) => Err(BackendError::Refused(format!(
            "Unknown policy {other:?}: it is ASK or AUTO."
        ))),
    }
}

/// An agent's name as people see it: the one a user gave it, else the one it declares.
async fn agent_name(conn: &mut PgConnection, agent: i64) -> Result<String, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT COALESCE(NULLIF(display_name, ''), name) FROM facade_agent WHERE id = $1",
    )
    .bind(agent)
    .fetch_one(conn)
    .await
}

/// The child an agent asked for, as the person deciding sees it.
pub(crate) struct HeldChild<'a> {
    pub task: i64,
    pub parent: i64,
    pub root: i64,
    pub action: &'a ActionRow,
    pub agent: i64,
    pub args: &'a Map<String, Value>,
    pub prompt: Option<&'a AssignPrompt>,
}

/// Write the `HELD` events of a held child in its creation's transaction: on the child, its
/// parent and (when it is another task) its root. Returns the events, to fan out after commit.
pub(crate) async fn record_held_child(
    tx: &mut Transaction<'_, Postgres>,
    child: &HeldChild<'_>,
) -> Result<Vec<i64>, sqlx::Error> {
    let caller_agent: i64 = sqlx::query_scalar("SELECT agent_id FROM facade_task WHERE id = $1")
        .bind(child.parent)
        .fetch_one(&mut **tx)
        .await?;
    let caller_name = agent_name(tx, caller_agent).await?;
    let target_name = agent_name(tx, child.agent).await?;
    let message = child
        .prompt
        .map(|prompt| prompt.title.clone())
        .filter(|title| !title.trim().is_empty())
        .unwrap_or_else(|| format!("{caller_name} wants to run {}", child.action.name));
    let value = json!({
        "kind": "assign",
        "prompt": child.prompt,
        "action": {"id": child.action.id.to_string(), "name": child.action.name, "hash": child.action.hash},
        "agent": {"id": child.agent.to_string(), "name": target_name},
        "args": child.args,
        "caller_task": child.parent.to_string(),
        "caller_agent": {"id": caller_agent.to_string(), "name": caller_name},
    });
    let event = |value: Value| NewEvent {
        message: Some(message.clone()),
        value: Some(value),
        ..NewEvent::default()
    };
    let mut events =
        vec![insert_event(&mut **tx, child.task, "HELD", &event(value.clone())).await?];
    let mut upstream = value;
    upstream["held"] = json!(child.task.to_string());
    for watcher in watchers(child.parent, child.root) {
        events.push(insert_event(&mut **tx, watcher, "HELD", &event(upstream.clone())).await?);
    }
    Ok(events)
}

/// Who is told about a held child besides itself: its parent, and its root when that is another.
fn watchers(parent: i64, root: i64) -> Vec<i64> {
    if root == parent {
        vec![parent]
    } else {
        vec![parent, root]
    }
}

/// A task's own hold (`HELD` from its agent). Under `AUTO` the server approves it at once.
pub async fn on_self_hold(
    ctx: &Context,
    agent: i64,
    task: &str,
    message: Option<&str>,
    prompt: Option<&AssignPrompt>,
    details: Option<&Value>,
    stamp: crate::persist::positions::Stamp,
) -> PersistResult<()> {
    let Some(row) = crate::persist::reports::agent_task(ctx, agent, task).await? else {
        return Ok(());
    };
    if row.is_done {
        return Ok(());
    }
    let message = message
        .filter(|m| !m.is_empty())
        .map(str::to_owned)
        .or_else(|| prompt.map(|p| p.title.clone()));
    let won = transitions::claim(
        ctx,
        row.id,
        Claim {
            event: Some(NewEvent {
                message,
                value: Some(json!({"kind": "self", "prompt": prompt, "details": details})),
                ..NewEvent::stamped(stamp)
            }),
            ..Claim::to("HELD")
        },
    )
    .await?;
    if !won {
        return Ok(());
    }
    let policy: String = sqlx::query_scalar("SELECT policy FROM facade_task WHERE id = $1")
        .bind(row.id)
        .fetch_one(&ctx.db)
        .await?;
    if policy != AUTO {
        return Ok(());
    }
    let decided = transitions::claim(
        ctx,
        row.id,
        Claim {
            only_if: Some(&|row: &transitions::TaskRow| row.latest_event_kind == "HELD"),
            event: Some(NewEvent {
                message: Some("Approved by auto-mode.".into()),
                value: Some(decision(true, None, true, None)),
                ..NewEvent::default()
            }),
            ..Claim::to("DECIDED")
        },
    )
    .await?;
    if decided {
        transport::broadcast(
            ctx,
            agent,
            ToAgent::Decide {
                task: row.id.to_string(),
                approved: true,
                answer: None,
                auto: true,
            },
            false,
        )
        .await;
    }
    Ok(())
}

fn decision(approved: bool, answer: Option<&str>, auto: bool, user: Option<&str>) -> Value {
    json!({"approved": approved, "answer": answer, "auto": auto, "user": user})
}

#[derive(Debug, sqlx::FromRow)]
struct HeldRow {
    is_done: bool,
    latest_event_kind: String,
    agent_id: i64,
    parent_id: Option<i64>,
    root_id: Option<i64>,
    organization_id: i64,
    /// The client the task was assigned from: for a child, the agent that asked for it.
    caller_client: Option<i64>,
    /// The task's own agent's client: for a task's own hold, the agent that holds.
    agent_client: i64,
    /// The user the run is for: its root's caller.
    root_user: Option<i64>,
    /// What waits: `assign` (a child never dispatched) or `self` (the task's own hold).
    held_kind: Option<String>,
}

/// A person's answer to a held task (`decide`). Only the user the run is for may decide, and
/// never from the client of the agent that asked or holds: an agent does not approve itself.
pub async fn decide(
    ctx: &Context,
    principal: &CallerContext,
    task: &str,
    approved: bool,
    answer: Option<String>,
) -> BackendResult<i64> {
    let task_id = parse_id(task)?;
    let row: Option<HeldRow> = sqlx::query_as(
        "SELECT t.is_done, t.latest_event_kind, t.agent_id, t.parent_id, t.root_id,
                a.organization_id, c.client_id AS caller_client, a.client_id AS agent_client,
                rc.user_id AS root_user,
                (SELECT e.value->>'kind' FROM facade_taskevent e
                  WHERE e.task_id = t.id AND e.kind = 'HELD' ORDER BY e.id DESC LIMIT 1) AS held_kind
           FROM facade_task t
           JOIN facade_agent a ON a.id = t.agent_id
           LEFT JOIN facade_caller c ON c.id = t.caller_id
           LEFT JOIN facade_task r ON r.id = COALESCE(t.root_id, t.id)
           LEFT JOIN facade_caller rc ON rc.id = r.caller_id
          WHERE t.id = $1",
    )
    .bind(task_id)
    .fetch_optional(&ctx.db)
    .await?;
    let Some(row) = row else {
        return Err(BackendError::Refused(
            "Task matching query does not exist.".into(),
        ));
    };
    if principal.organization != Some(row.organization_id) {
        return Err(BackendError::Forbidden(format!(
            "Task {task} is not in your organization."
        )));
    }
    if row.is_done {
        return Err(BackendError::Refused("Task is already terminal".into()));
    }
    if row.latest_event_kind != "HELD" {
        return Err(BackendError::Refused(format!(
            "Task {task} is not held ({}): there is nothing to decide.",
            row.latest_event_kind
        )));
    }
    if row.root_user != Some(principal.user) {
        return Err(BackendError::Forbidden(
            "Only the user this run is for may decide what it holds.".into(),
        ));
    }
    let asking_client = match row.held_kind.as_deref() {
        Some("self") => Some(row.agent_client),
        _ => row.caller_client,
    };
    if asking_client == Some(principal.client) {
        return Err(BackendError::Forbidden(
            "An agent may not decide its own hold: a person decides, from another client.".into(),
        ));
    }

    let decided = decision(
        approved,
        answer.as_deref(),
        false,
        Some(&principal.user_sub),
    );
    if row.held_kind.as_deref() == Some("self") {
        decide_self(ctx, task_id, row.agent_id, approved, answer, decided).await?;
    } else {
        let watchers = row
            .parent_id
            .map(|parent| watchers(parent, row.root_id.unwrap_or(parent)))
            .unwrap_or_default();
        decide_child(ctx, task_id, &watchers, approved, answer, decided).await?;
    }
    Ok(task_id)
}

/// The answer to a task's own hold: `DECIDED`, and the `DECIDE` its agent waits for.
async fn decide_self(
    ctx: &Context,
    task: i64,
    agent: i64,
    approved: bool,
    answer: Option<String>,
    decided: Value,
) -> BackendResult<()> {
    let won = transitions::claim(
        ctx,
        task,
        Claim {
            only_if: Some(&|row: &transitions::TaskRow| row.latest_event_kind == "HELD"),
            event: Some(NewEvent {
                message: Some(if approved { "Approved." } else { "Denied." }.into()),
                value: Some(decided),
                ..NewEvent::default()
            }),
            ..Claim::to("DECIDED")
        },
    )
    .await?;
    if !won {
        return Err(BackendError::Refused(
            "The task is no longer held: someone decided it first.".into(),
        ));
    }
    transport::broadcast(
        ctx,
        agent,
        ToAgent::Decide {
            task: task.to_string(),
            approved,
            answer,
            auto: false,
        },
        false,
    )
    .await;
    Ok(())
}

/// The task an approved child's Assign goes to: the child itself, or, for a higher-order
/// wrapper (which is never sent anywhere), the lower child that runs it.
async fn runner_of(conn: &mut PgConnection, task: i64) -> Result<i64, sqlx::Error> {
    let lower: Option<i64> = sqlx::query_scalar(
        "SELECT id FROM facade_task WHERE parent_id = $1 AND is_higher_order_child ORDER BY id LIMIT 1",
    )
    .bind(task)
    .fetch_optional(conn)
    .await?;
    Ok(lower.unwrap_or(task))
}

/// The answer to a held child: approved, it is dispatched as any assign is; denied, it ends
/// `DENIED`, never having run. Its parent and root get the `DECIDED` too.
async fn decide_child(
    ctx: &Context,
    task: i64,
    watchers: &[i64],
    approved: bool,
    answer: Option<String>,
    decided: Value,
) -> BackendResult<()> {
    let mut tx = ctx.db.begin().await?;
    let kind: Option<(String, bool)> = sqlx::query_as(
        "SELECT latest_event_kind, is_done FROM facade_task WHERE id = $1 FOR UPDATE",
    )
    .bind(task)
    .fetch_optional(&mut *tx)
    .await?;
    if !matches!(&kind, Some((kind, false)) if kind == "HELD") {
        return Err(BackendError::Refused(
            "The task is no longer held: someone decided it first.".into(),
        ));
    }
    let runner = runner_of(&mut tx, task).await?;
    let message = if approved { "Approved." } else { "Denied." };
    let decided_event = NewEvent {
        message: Some(message.into()),
        value: Some(decided.clone()),
        ..NewEvent::default()
    };
    let mut events = vec![insert_event(&mut *tx, task, "DECIDED", &decided_event).await?];
    let mut upstream = decided.clone();
    upstream["held"] = json!(task.to_string());
    for &watcher in watchers {
        let event = NewEvent {
            value: Some(upstream.clone()),
            ..decided_event.clone()
        };
        events.push(insert_event(&mut *tx, watcher, "DECIDED", &event).await?);
    }

    let mut saved = vec![task];
    if approved {
        // From here on it is an ordinary dispatched task: the pickup watchdog's clock starts now.
        for target in dict_of(task, runner) {
            update_task_kind(&mut tx, target, "QUEUED", false).await?;
        }
        sqlx::query(
            "UPDATE facade_task SET dispatched_at = $2, dispatch_attempts = 1 WHERE id = $1",
        )
        .bind(runner)
        .bind(Utc::now())
        .execute(&mut *tx)
        .await?;
        events.push(
            insert_event(
                &mut *tx,
                task,
                "QUEUED",
                &NewEvent::default().with_message(Some("Approved: handed to its agent.".into())),
            )
            .await?,
        );
        if runner != task {
            saved.push(runner);
        }
    } else {
        let denied = NewEvent {
            message: Some("Denied: it never ran.".into()),
            value: Some(json!({"answer": answer, "user": decided["user"]})),
            ..NewEvent::default()
        };
        for target in dict_of(task, runner) {
            update_task_kind(&mut tx, target, "DENIED", true).await?;
            if target != task {
                saved.push(target);
            }
        }
        events.push(insert_event(&mut *tx, task, "DENIED", &denied).await?);
    }
    tx.commit().await?;
    for target in saved {
        signals::task_saved(ctx, target, false).await;
    }
    for event in events {
        signals::task_event_created(ctx, event).await;
    }

    if approved {
        let agent: i64 = sqlx::query_scalar("SELECT agent_id FROM facade_task WHERE id = $1")
            .bind(runner)
            .fetch_one(&ctx.db)
            .await?;
        match build_redispatch_assign(ctx, runner).await? {
            Some(mut assign) => {
                assign.message = answer;
                crate::backend::dispatch(ctx, runner, agent, assign).await;
            }
            None => {
                transitions::finalize_terminal(
                    ctx,
                    runner,
                    "CRITICAL",
                    "Approved, but the Assign could not be built (no caller identity, or the provenance policy refused).",
                    None,
                    transitions::Guard {
                        only_if: None,
                        extra: None,
                        skip_locked: false,
                    },
                )
                .await?;
            }
        }
    }
    Ok(())
}

/// The held task and, for a wrapper, its lower child: both move together.
fn dict_of(task: i64, runner: i64) -> Vec<i64> {
    if runner == task {
        vec![task]
    } else {
        vec![task, runner]
    }
}

/// Settle a held child that is cancelled or interrupted before anyone decided: no agent has
/// it, so nobody confirms. Its lower child, when it is a higher-order wrapper, goes with it.
pub async fn settle_held_lower(
    tx: &mut Transaction<'_, Postgres>,
    task: i64,
    outcome: &str,
) -> Result<Option<i64>, sqlx::Error> {
    let runner = runner_of(tx, task).await?;
    if runner == task {
        return Ok(None);
    }
    update_task_kind(tx, runner, outcome, true).await?;
    Ok(Some(runner))
}

/// End the children of a task that ended while they were still held: nobody decided, no agent
/// ever had them, so they end `CANCELLED` with it and their caller is told. Called whenever a
/// task ends (`transitions::claim`), so a held child never outlives its parent.
pub async fn settle_held_children(ctx: &Context, parent: i64) -> Result<(), sqlx::Error> {
    let mut tx = ctx.db.begin().await?;
    let held: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM facade_task
          WHERE parent_id = $1 AND latest_event_kind = 'HELD' AND is_done = false
            AND dispatch_attempts = 0 AND picked_up_at IS NULL
          ORDER BY id FOR UPDATE SKIP LOCKED",
    )
    .bind(parent)
    .fetch_all(&mut *tx)
    .await?;
    if held.is_empty() {
        return Ok(());
    }
    let mut saved = vec![];
    let mut events = vec![];
    for task in held {
        update_task_kind(&mut tx, task, "CANCELLED", true).await?;
        events.push(
            insert_event(
                &mut *tx,
                task,
                "CANCELLED",
                &NewEvent::default().with_message(Some(
                    "Its parent ended while it was held — never dispatched.".into(),
                )),
            )
            .await?,
        );
        saved.push(task);
        if let Some(lower) = settle_held_lower(&mut tx, task, "CANCELLED").await? {
            saved.push(lower);
        }
    }
    tx.commit().await?;
    for task in saved {
        signals::task_saved(ctx, task, false).await;
    }
    for event in events {
        signals::task_event_created(ctx, event).await;
    }
    Ok(())
}

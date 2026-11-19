//! What an executing agent tells us about its work, and the fence around it
//! (`facade/persist/reports.py`).
//!
//! The agent's socket is authenticated, but the task id inside a frame is not: [`agent_task`]
//! resolves every reported task with an `agent_id` predicate, or any agent could finish any task
//! by naming its id. It is also the one place that knows the agent picked a task up, because the
//! stream events (log / yield / progress) never move `latest_event_kind`.

use chrono::Utc;
use sqlx::PgConnection;

use crate::context::Context;
use crate::messages::{AgentFrame, FromAgent, LogLevel};
use crate::persist::positions::{is_numbered, position_stamp};
use crate::persist::transitions::{self, Claim, NewEvent, TaskRow, TASK_ROW};
use crate::persist::{run_after, After, InTxResult, PersistResult};
use crate::signals;

/// Whether an Assign for this task is still worth delivering (`is_task_open`): only a task that
/// verifiably is finalized closes the fence; an id we cannot resolve is delivered.
pub async fn is_task_open(ctx: &Context, task: &str) -> bool {
    let Ok(task) = task.parse::<i64>() else {
        return true;
    };
    !matches!(
        sqlx::query_scalar::<_, bool>("SELECT is_done FROM facade_task WHERE id = $1")
            .bind(task)
            .fetch_optional(&ctx.db)
            .await,
        Ok(Some(true))
    )
}

/// The task, if it is this agent's; the first report stamps `picked_up_at` (`_agent_task`).
/// `None` for an unknown task or another agent's: the frame is dropped, the transport stays.
pub async fn agent_task(ctx: &Context, agent: i64, task: &str) -> PersistResult<Option<TaskRow>> {
    let mut conn = ctx.db.acquire().await?;
    Ok(agent_task_in(&mut conn, agent, task).await?)
}

/// [`agent_task`] on a connection of the caller's (inside its transaction).
pub async fn agent_task_in(
    conn: &mut PgConnection,
    agent: i64,
    task: &str,
) -> Result<Option<TaskRow>, sqlx::Error> {
    let Ok(id) = task.parse::<i64>() else {
        tracing::warn!(
            agent,
            task,
            "reported on a task that is not assigned to it; dropping"
        );
        return Ok(None);
    };
    let row: Option<TaskRow> = sqlx::query_as(&format!(
        "SELECT {TASK_ROW} FROM facade_task WHERE id = $1 AND agent_id = $2"
    ))
    .bind(id)
    .bind(agent)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(mut row) = row else {
        tracing::warn!(
            agent,
            task,
            "reported on a task that is not assigned to it; dropping"
        );
        return Ok(None);
    };
    if row.picked_up_at.is_none() && !row.is_done {
        let now = Utc::now();
        sqlx::query(
            "UPDATE facade_task SET picked_up_at = $2 WHERE id = $1 AND picked_up_at IS NULL",
        )
        .bind(id)
        .bind(now)
        .execute(&mut *conn)
        .await?;
        crate::trace::measures::picked_up(row.pickup_clock());
        row.picked_up_at = Some(now);
    }
    Ok(Some(row))
}

async fn from_earlier_session(
    ctx: &Context,
    agent: i64,
    journal_session: &str,
) -> PersistResult<bool> {
    let active: Option<String> =
        sqlx::query_scalar("SELECT active_session_id FROM facade_agent WHERE id = $1")
            .bind(agent)
            .fetch_optional(&ctx.db)
            .await?
            .flatten();
    Ok(active.is_some_and(|active| active != journal_session))
}

/// Keep an outcome reported after the task was LOST, next to it (`_record_late_report_sync`).
/// LOST is final; a resend of the same late report is recorded once (keyed on its position).
async fn record_late_report(
    ctx: &Context,
    task: i64,
    kind: &str,
    event: &NewEvent,
) -> PersistResult<()> {
    if let Some(pos) = event.agent_pos {
        let seen: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM facade_taskevent WHERE task_id = $1 AND kind = 'LATE_REPORT' AND agent_pos = $2)",
        )
        .bind(task)
        .bind(pos)
        .fetch_one(&ctx.db)
        .await?;
        if seen {
            return Ok(());
        }
    }
    let note = format!("The agent reported {kind} after the task was lost; it stays LOST.");
    let late = NewEvent {
        message: Some(match &event.message {
            Some(message) if !message.is_empty() => format!("{message} ({note})"),
            _ => note,
        }),
        value: Some(serde_json::json!({"kind": kind})),
        ..event.clone()
    };
    let id = transitions::insert_event(&ctx.db, task, "LATE_REPORT", &late).await?;
    signals::task_event_created(ctx, id).await;
    Ok(())
}

/// Replace a done task's server-written terminal with the agent's (`_override_server_outcome_sync`).
async fn override_server_outcome(
    ctx: &Context,
    task: i64,
    kind: &str,
    event: &NewEvent,
) -> PersistResult<bool> {
    let mut tx = ctx.db.begin().await?;
    let done: Option<bool> = sqlx::query_scalar(
        "SELECT is_done FROM facade_task WHERE id = $1 FOR UPDATE OF facade_task",
    )
    .bind(task)
    .fetch_optional(&mut *tx)
    .await?;
    if done != Some(true) {
        return Ok(false);
    }
    let last: Option<(String, Option<i64>)> = sqlx::query_as(
        "SELECT kind, agent_pos FROM facade_taskevent WHERE task_id = $1 AND kind = ANY($2) ORDER BY id DESC LIMIT 1",
    )
    .bind(task)
    .bind(transitions::TERMINAL_KINDS)
    .fetch_optional(&mut *tx)
    .await?;
    let Some((last_kind, None)) = last else {
        return Ok(false); // the agent already reported this task's outcome: that one stands
    };
    tracing::info!(
        task,
        "the agent's {kind} (from its previous session) replaces the server's {last_kind}"
    );
    sqlx::query(
        "UPDATE facade_task SET latest_event_kind = $2, finished_at = now(), revision = revision + 1, updated_at = now() WHERE id = $1",
    )
    .bind(task)
    .bind(kind)
    .execute(&mut *tx)
    .await?;
    let note =
        format!("Reported by the agent's previous session; replaces the server's {last_kind}.");
    let replaced = NewEvent {
        message: Some(match &event.message {
            Some(message) if !message.is_empty() => format!("{message} ({note})"),
            _ => note,
        }),
        ..event.clone()
    };
    let id = transitions::insert_event(&mut *tx, task, kind, &replaced).await?;
    tx.commit().await?;
    signals::task_saved(ctx, task, false).await;
    signals::task_event_created(ctx, id).await;
    Ok(true)
}

/// Persist an agent-reported terminal, exactly once however often it is reported
/// (`_finalize_from_agent`). The agent's outcome wins over one the server wrote itself when the
/// report comes from an earlier session; after LOST, it is a LATE_REPORT.
async fn finalize_from_agent(
    ctx: &Context,
    agent: i64,
    frame: &AgentFrame,
    task: &str,
    kind: &str,
    message: Option<String>,
) -> PersistResult<()> {
    let Some(row) = agent_task(ctx, agent, task).await? else {
        return Ok(());
    };
    let mut event = NewEvent::stamped(position_stamp(frame)).with_message(message.clone());
    // A FAILED report's code (`STATE_PRECONDITION`) is the event's value: `{"code": ...}`.
    if let FromAgent::Failed {
        code: Some(code), ..
    } = &frame.message
    {
        event.value = Some(serde_json::json!({ "code": code }));
    }
    if row.is_done {
        if row.latest_event_kind == "LOST" {
            return record_late_report(ctx, row.id, kind, &event).await;
        }
        let journal_session = if is_numbered(frame) {
            frame.journal_session.as_deref()
        } else {
            None
        };
        let Some(journal_session) = journal_session else {
            return Ok(()); // a resent terminal report
        };
        if !from_earlier_session(ctx, agent, journal_session).await? {
            return Ok(());
        }
        if !override_server_outcome(ctx, row.id, kind, &event).await? {
            return Ok(());
        }
    } else if !transitions::claim(
        ctx,
        row.id,
        Claim {
            mark_done: true,
            event: Some(event),
            ..Claim::to(kind)
        },
    )
    .await?
    {
        return Ok(()); // lost the race to another report or a sweep: theirs is the outcome
    }
    transitions::unfold_to_higher_order(
        ctx,
        row.id,
        kind,
        None,
        message.as_deref(),
        Some(row.is_higher_order_child),
    )
    .await?;
    Ok(())
}

/// A non-terminal lifecycle confirmation: started / paused / resumed (`_on_nonterminal_confirm`).
async fn nonterminal_confirm(
    ctx: &Context,
    agent: i64,
    task: &str,
    kind: &str,
    event: NewEvent,
) -> PersistResult<()> {
    let Some(row) = agent_task(ctx, agent, task).await? else {
        return Ok(());
    };
    if row.is_done {
        return Ok(());
    }
    transitions::claim(
        ctx,
        row.id,
        Claim {
            event: Some(event),
            ..Claim::to(kind)
        },
    )
    .await?;
    Ok(())
}

/// Append a stream event (log / yield / progress / effect) to a task this agent owns
/// (`_record_event`). It does not move `latest_event_kind`. Nothing is recorded on a done task,
/// except a result after LOST, kept as a LATE_REPORT. Returns whether it was recorded.
async fn record_event(
    ctx: &Context,
    agent: i64,
    task: &str,
    kind: &str,
    event: NewEvent,
) -> PersistResult<bool> {
    let mut tx = ctx.db.begin().await?;
    let mut after = vec![];
    let recorded = record_event_in(&mut tx, &mut after, agent, task, kind, event).await?;
    tx.commit().await?;
    run_after(ctx, after).await;
    Ok(recorded.is_some())
}

/// [`record_event`] inside a transaction of the caller's: the task's row when the event was
/// recorded. What to tell whoever follows is left in `after`, for once that is committed.
async fn record_event_in(
    conn: &mut PgConnection,
    after: &mut Vec<After>,
    agent: i64,
    task: &str,
    kind: &str,
    event: NewEvent,
) -> Result<Option<TaskRow>, sqlx::Error> {
    let Some(row) = agent_task_in(&mut *conn, agent, task).await? else {
        return Ok(None);
    };
    if row.is_done {
        if row.latest_event_kind == "LOST" && kind == "YIELD" {
            let late = NewEvent {
                message: Some("A result the agent reported after the task was lost.".into()),
                ..event
            };
            let id = transitions::insert_event(&mut *conn, row.id, "LATE_REPORT", &late).await?;
            after.push(After::Event(id));
            return Ok(None);
        }
        tracing::debug!(
            task,
            kind,
            "dropping an event for a task that is already done"
        );
        return Ok(None);
    }
    let id = transitions::insert_event(&mut *conn, row.id, kind, &event).await?;
    after.push(if kind == "PROGRESS" {
        // Recorded always; fanned out at most once per interval per task.
        After::Progress {
            task: row.id,
            event: id,
        }
    } else {
        After::Event(id)
    });
    Ok(Some(row))
}

/// Whether a frame is one of the reports a running task streams (`LOG`, `PROGRESS`, `YIELD`,
/// `EFFECT`): what [`on_stream_report`] projects inside a transaction of its caller's.
pub fn is_stream_report<D>(message: &FromAgent<D>) -> bool {
    matches!(
        message,
        FromAgent::Log { .. }
            | FromAgent::Progress { .. }
            | FromAgent::Yield { .. }
            | FromAgent::Effect { .. }
    )
}

/// Project one of a running task's stream reports inside a transaction of the caller's, so it
/// is recorded with the position that counts it (`message_router`). What to tell whoever
/// follows is left in `after`.
pub async fn on_stream_report(
    conn: &mut PgConnection,
    after: &mut Vec<After>,
    agent: i64,
    frame: &AgentFrame,
) -> InTxResult<()> {
    let stamp = position_stamp(frame);
    match &frame.message {
        FromAgent::Log {
            task,
            message,
            level,
        } => {
            let event = NewEvent {
                message: Some(message.clone()),
                level: Some(level_name(*level)),
                ..NewEvent::stamped(stamp)
            };
            record_event_in(conn, after, agent, task, "LOG", event).await?;
        }
        FromAgent::Progress {
            task,
            progress,
            message,
        } => {
            let event = NewEvent {
                progress: progress.map(i64::from),
                message: message.clone(),
                ..NewEvent::stamped(stamp)
            };
            record_event_in(conn, after, agent, task, "PROGRESS", event).await?;
        }
        FromAgent::Yield { task, returns } => {
            let event = NewEvent {
                returns: Some(returns.clone()),
                ..NewEvent::stamped(stamp)
            };
            let recorded = record_event_in(&mut *conn, after, agent, task, "YIELD", event).await?;
            // A higher-order child's yield is its wrapper's too, written with it.
            if let Some(row) = recorded.filter(|row| row.is_higher_order_child) {
                if let Some((wrapper, event)) =
                    transitions::unfold_yield_in(conn, row.id, returns).await?
                {
                    after.push(After::TaskSaved(wrapper));
                    after.extend(event.map(After::Event));
                }
            }
        }
        FromAgent::Effect {
            task,
            effect,
            value,
            key,
        } => {
            // The value already recorded under a key stands.
            if let (Some(key), Ok(task_id)) = (key.as_deref(), task.parse::<i64>()) {
                let recorded: bool = sqlx::query_scalar(
                    "SELECT EXISTS (SELECT 1 FROM facade_taskevent WHERE task_id = $1 AND kind = 'EFFECT' AND key = $2)",
                )
                .bind(task_id)
                .bind(key)
                .fetch_one(&mut *conn)
                .await?;
                if recorded {
                    return Ok(());
                }
            }
            let event = NewEvent {
                effect: serde_json::to_value(effect)
                    .ok()
                    .and_then(|v| v.as_str().map(str::to_owned)),
                value: Some(value.clone()),
                key: key.clone(),
                ..NewEvent::stamped(stamp)
            };
            record_event_in(conn, after, agent, task, "EFFECT", event).await?;
        }
        _ => {}
    }
    Ok(())
}

fn level_name(level: LogLevel) -> String {
    serde_json::to_value(level)
        .ok()
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_else(|| "INFO".into())
}

/// Route one of the agent's own reports to its handler (`on_agent_*`). Returns whether it was
/// one; the router acks terminals and lifecycle confirmations.
pub async fn on_report(ctx: &Context, agent: i64, frame: &AgentFrame) -> PersistResult<bool> {
    let stamp = position_stamp(frame);
    match &frame.message {
        FromAgent::Started { task } => {
            nonterminal_confirm(ctx, agent, task, "STARTED", NewEvent::stamped(stamp)).await?
        }
        FromAgent::Paused {
            task,
            message,
            details,
        } => {
            // A task that paused itself (task.hold) says why, and what a person deciding may want to know.
            let event = NewEvent {
                message: message.clone().filter(|m| !m.is_empty()),
                value: details
                    .clone()
                    .filter(|d| !d.is_null() && d.as_object().is_none_or(|o| !o.is_empty())),
                ..NewEvent::stamped(stamp)
            };
            nonterminal_confirm(ctx, agent, task, "PAUSED", event).await?
        }
        FromAgent::Resumed { task } => {
            nonterminal_confirm(ctx, agent, task, "RESUMED", NewEvent::stamped(stamp)).await?
        }
        FromAgent::Held {
            task,
            message,
            prompt,
            details,
            blok,
        } => {
            crate::holds::on_self_hold(
                ctx,
                agent,
                task,
                message.as_deref(),
                prompt.as_ref(),
                details.as_ref(),
                blok.as_ref(),
                stamp,
            )
            .await?
        }
        FromAgent::Inform {
            task,
            message,
            blok,
        } => {
            // Shown while the task carries on: the blok, then the event naming it.
            let Some(row) = agent_task(ctx, agent, task).await? else {
                return Ok(true);
            };
            if row.is_done {
                return Ok(true);
            }
            let Some(materialized) =
                crate::task_bloks::shown(ctx, agent, row.id, crate::task_bloks::INFORM, blok)
                    .await?
            else {
                return Ok(true);
            };
            let event = NewEvent {
                message: message.clone().filter(|m| !m.is_empty()),
                value: Some(serde_json::json!({"blok": materialized.to_string()})),
                ..NewEvent::stamped(stamp)
            };
            record_event(ctx, agent, task, "INFORM", event).await?;
        }
        FromAgent::Completed { task } => {
            finalize_from_agent(ctx, agent, frame, task, "COMPLETED", None).await?
        }
        FromAgent::Cancelled { task } => {
            finalize_from_agent(ctx, agent, frame, task, "CANCELLED", None).await?
        }
        FromAgent::Interrupted { task } => {
            finalize_from_agent(ctx, agent, frame, task, "INTERRUPTED", None).await?
        }
        FromAgent::Failed { task, error, .. } => {
            finalize_from_agent(ctx, agent, frame, task, "FAILED", Some(error.clone())).await?
        }
        FromAgent::Critical { task, error } => {
            finalize_from_agent(ctx, agent, frame, task, "CRITICAL", Some(error.clone())).await?
        }
        message if is_stream_report(message) => {
            let mut tx = ctx.db.begin().await?;
            let mut after = vec![];
            on_stream_report(&mut tx, &mut after, agent, frame).await?;
            tx.commit().await?;
            run_after(ctx, after).await;
        }
        _ => return Ok(false),
    }
    Ok(true)
}

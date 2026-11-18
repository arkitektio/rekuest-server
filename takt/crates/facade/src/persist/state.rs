//! An agent's reported state: updates, snapshots, sessions and lock reports
//! (`facade/persist/state.py`).
//!
//! Plain persistence, separate from the task machinery: none of it touches a task, a claim or a
//! lease. Lock rows are a report of what the agent's own in-process lock is doing, not a grant.
//!
//! Every frame here is projected inside a transaction of its caller's ([`on_state_in`]): a
//! numbered one with the position that counts it (`message_router`), so a snapshot of several
//! states is stored whole or not at all and nothing is stored twice.

use serde_json::{Map, Value};
use sqlx::PgConnection;

use crate::context::Context;
use crate::messages::{is_probe_task, AgentFrame, FromAgent};
use crate::persist::positions::{position_stamp, session_row, Stamp};
use crate::persist::{run_after, After, InTxError, InTxResult, PersistResult};

async fn state_id(conn: &mut PgConnection, agent: i64, interface: &str) -> InTxResult<i64> {
    sqlx::query_scalar("SELECT id FROM facade_state WHERE agent_id = $1 AND interface = $2")
        .bind(agent)
        .bind(interface)
        .fetch_optional(&mut *conn)
        .await?
        .ok_or_else(|| {
            InTxError::Refused(format!("State matching query does not exist: {interface}"))
        })
}

/// One update of a state (`STATE_UPDATE`, or the one operation of a `STATE_PATCH`): one row,
/// which takes the state to `global_rev`. The changing task is linked only when it is one of
/// this agent's tasks; a resend of a recorded revision is dropped.
#[allow(clippy::too_many_arguments)]
async fn on_agent_state_update(
    conn: &mut PgConnection,
    after: &mut Vec<After>,
    agent: i64,
    stamp: Stamp,
    session_id: &str,
    global_rev: u64,
    state_name: &str,
    ops: &Value,
    task_id: Option<&str>,
    lifecycle: Option<&str>,
    qualifiers: Option<&[String]>,
) -> InTxResult<()> {
    if ops.as_array().is_none_or(Vec::is_empty) {
        return Err(InTxError::Refused(format!(
            "An update of {state_name} without operations changes nothing."
        )));
    }
    let state = state_id(&mut *conn, agent, state_name).await?;
    let session = session_row(&mut *conn, agent, session_id).await?;
    // `Patch.task` is a real foreign key: a probe's update is kept without the probe's link,
    // and so is one naming a task that is not this agent's.
    let mut task = task_id
        .filter(|t| !is_probe_task(t))
        .and_then(|t| t.parse::<i64>().ok());
    if let Some(id) = task {
        let owned: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM facade_task WHERE id = $1 AND agent_id = $2)",
        )
        .bind(id)
        .bind(agent)
        .fetch_one(&mut *conn)
        .await?;
        if !owned {
            task = None;
        }
    }
    // One row per (session, global_rev, state): a revision already recorded is left as it is.
    let inserted: Option<i64> = sqlx::query_scalar(
        "INSERT INTO facade_patch
            (state_id, agent_id, session_id, interface, ops, task_id,
             global_rev, agent_pos, agent_ts, step, lifecycle, qualifiers)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
         ON CONFLICT (session_id, global_rev, state_id) DO NOTHING
         RETURNING id",
    )
    .bind(state)
    .bind(agent)
    .bind(session)
    .bind(state_name)
    .bind(sqlx::types::Json(ops))
    .bind(task)
    .bind(global_rev as i32)
    .bind(stamp.agent_pos)
    .bind(stamp.agent_ts)
    .bind(stamp.step)
    // What the agent says of the change beside its value: an empty one says nothing.
    .bind(lifecycle.filter(|l| !l.is_empty()))
    .bind(qualifiers.filter(|q| !q.is_empty()).map(sqlx::types::Json))
    .fetch_optional(&mut *conn)
    .await?;
    match inserted {
        Some(id) => after.push(After::Patch(id)),
        None => tracing::info!(
            agent,
            state_name,
            global_rev,
            session_id,
            "dropping a duplicate update"
        ),
    }
    Ok(())
}

/// Snapshots of the agent's states at a revision (`on_agent_state_snapshot`, and
/// `on_agent_session_init` with revision 0). Every state is looked up before any is stored: a
/// snapshot naming a state the agent does not have is refused whole.
async fn on_agent_snapshots(
    conn: &mut PgConnection,
    agent: i64,
    session_id: &str,
    global_rev: u64,
    snapshots: &Map<String, Value>,
) -> InTxResult<()> {
    let mut states = Vec::with_capacity(snapshots.len());
    for (state_name, snapshot) in snapshots {
        states.push((state_id(&mut *conn, agent, state_name).await?, snapshot));
    }
    let session = session_row(&mut *conn, agent, session_id).await?;
    for (state, snapshot) in states {
        sqlx::query(
            "INSERT INTO facade_snapshot (session_id, state_id, agent_id, value, global_rev)
             VALUES ($1, $2, $3, $4, $5)",
        )
        .bind(session)
        .bind(state)
        .bind(agent)
        .bind(sqlx::types::Json(snapshot))
        .bind(global_rev as i32)
        .execute(&mut *conn)
        .await?;
    }
    Ok(())
}

/// Record that `task` holds lock `key` on this agent (`on_agent_lock`). An unknown task is
/// ignored: a stray lock must not tear down the transport.
async fn lock_in(
    conn: &mut PgConnection,
    after: &mut Vec<After>,
    agent: i64,
    key: &str,
    task: &str,
) -> InTxResult<()> {
    let Ok(task_id) = task.parse::<i64>() else {
        return Err(InTxError::Refused(format!(
            "Field 'id' expected a number but got {task:?}"
        )));
    };
    let exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM facade_task WHERE id = $1)")
            .bind(task_id)
            .fetch_one(&mut *conn)
            .await?;
    if !exists {
        tracing::warn!(
            agent,
            key,
            task,
            "lock requested by an unknown task: ignored"
        );
        return Ok(());
    }
    sqlx::query(
        "INSERT INTO facade_lock (agent_id, key, hold_by_id) VALUES ($1, $2, $3)
         ON CONFLICT (agent_id, key) DO UPDATE SET hold_by_id = EXCLUDED.hold_by_id, updated_at = now()",
    )
    .bind(agent)
    .bind(key)
    .bind(task_id)
    .execute(&mut *conn)
    .await?;
    // The agent's feed carries its locks: who holds what is visible the moment it changes.
    after.push(After::AgentSaved(agent));
    Ok(())
}

/// Release: clear the holder, a no-op if the lock is absent or already free (`on_agent_unlock`).
async fn unlock_in(
    conn: &mut PgConnection,
    after: &mut Vec<After>,
    agent: i64,
    key: &str,
) -> InTxResult<()> {
    let released = sqlx::query("UPDATE facade_lock SET hold_by_id = NULL, updated_at = now() WHERE agent_id = $1 AND key = $2 AND hold_by_id IS NOT NULL")
        .bind(agent)
        .bind(key)
        .execute(&mut *conn)
        .await?
        .rows_affected();
    if released > 0 {
        after.push(After::AgentSaved(agent));
    }
    Ok(())
}

/// Whether a frame is one of the agent's state or lock reports: what [`on_state_in`] projects.
pub fn is_state_report<D>(message: &FromAgent<D>) -> bool {
    matches!(
        message,
        FromAgent::StateUpdate { .. }
            | FromAgent::StatePatch { .. }
            | FromAgent::StateSnapshot { .. }
            | FromAgent::SessionInit { .. }
            | FromAgent::Lock { .. }
            | FromAgent::Unlock { .. }
    )
}

/// Project one of the agent's state or lock reports inside a transaction of the caller's.
/// What to tell whoever follows is left in `after`, for once that is committed.
pub async fn on_state_in(
    conn: &mut PgConnection,
    after: &mut Vec<After>,
    agent: i64,
    frame: &AgentFrame,
) -> InTxResult<()> {
    match &frame.message {
        FromAgent::StateUpdate {
            state,
            global_rev,
            ops,
            task,
            lifecycle,
            qualifiers,
        } => {
            // The session an update belongs to is the frame's own.
            let Some(session_id) = frame.journal_session.as_deref() else {
                return Err(InTxError::Refused(format!(
                    "An update of {state} that names no journal session belongs to none."
                )));
            };
            let ops = serde_json::to_value(ops).unwrap_or(Value::Null);
            on_agent_state_update(
                conn,
                after,
                agent,
                position_stamp(frame),
                session_id,
                *global_rev,
                state,
                &ops,
                task.as_deref(),
                lifecycle.as_deref(),
                qualifiers.as_deref(),
            )
            .await
        }
        // An agent from before updates: its one operation is an update of its own.
        FromAgent::StatePatch {
            session_id,
            global_rev,
            state_name,
            op,
            path,
            value,
            task_id,
            lifecycle,
            qualifiers,
            ..
        } => {
            let ops = serde_json::json!([{"op": op, "path": path, "value": value}]);
            on_agent_state_update(
                conn,
                after,
                agent,
                position_stamp(frame),
                session_id,
                *global_rev,
                state_name,
                &ops,
                task_id.as_deref(),
                lifecycle.as_deref(),
                qualifiers.as_deref(),
            )
            .await
        }
        FromAgent::StateSnapshot {
            session_id,
            global_rev,
            snapshots,
        } => on_agent_snapshots(conn, agent, session_id, *global_rev, snapshots).await,
        FromAgent::SessionInit { session_id, states } => {
            on_agent_snapshots(conn, agent, session_id, 0, states).await
        }
        FromAgent::Lock { key, task } => lock_in(conn, after, agent, key, task).await,
        FromAgent::Unlock { key, .. } => unlock_in(conn, after, agent, key).await,
        _ => Ok(()),
    }
}

/// Route one of the agent's state or lock reports, in a transaction of its own (a frame without
/// a position). Returns whether it was one.
pub async fn on_state(ctx: &Context, agent: i64, frame: &AgentFrame) -> PersistResult<bool> {
    if !is_state_report(&frame.message) {
        return Ok(false);
    }
    let mut tx = ctx.db.begin().await?;
    let mut after = vec![];
    on_state_in(&mut tx, &mut after, agent, frame).await?;
    tx.commit().await?;
    run_after(ctx, after).await;
    Ok(true)
}

/// Record a lock report in a transaction of its own (`on_agent_lock`).
pub async fn on_agent_lock(ctx: &Context, agent: i64, key: &str, task: &str) -> PersistResult<()> {
    let mut tx = ctx.db.begin().await?;
    let mut after = vec![];
    lock_in(&mut tx, &mut after, agent, key, task).await?;
    tx.commit().await?;
    run_after(ctx, after).await;
    Ok(())
}

/// Record a release in a transaction of its own (`on_agent_unlock`).
pub async fn on_agent_unlock(ctx: &Context, agent: i64, key: &str) -> PersistResult<()> {
    let mut tx = ctx.db.begin().await?;
    let mut after = vec![];
    unlock_in(&mut tx, &mut after, agent, key).await?;
    tx.commit().await?;
    run_after(ctx, after).await;
    Ok(())
}

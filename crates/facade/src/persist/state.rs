//! An agent's reported state: patches, snapshots, sessions and lock reports
//! (`facade/persist/state.py`).
//!
//! Plain persistence, separate from the task machinery: none of it touches a task, a claim or a
//! lease. Lock rows are a report of what the agent's own in-process lock is doing, not a grant.

use serde_json::{Map, Value};

use crate::context::Context;
use crate::messages::{is_probe_task, AgentFrame};
use crate::persist::positions::{get_or_create_session, position_stamp, Stamp};
use crate::persist::{PersistError, PersistResult};
use crate::signals;

async fn state_id(ctx: &Context, agent: i64, interface: &str) -> PersistResult<i64> {
    sqlx::query_scalar("SELECT id FROM facade_state WHERE agent_id = $1 AND interface = $2")
        .bind(agent)
        .bind(interface)
        .fetch_optional(&ctx.db)
        .await?
        .ok_or_else(|| {
            PersistError::Refused(format!("State matching query does not exist: {interface}"))
        })
}

/// One RFC 6902 operation on a state (`on_agent_state_patch`). The changing task is linked
/// only when it is one of this agent's tasks; a resend of a recorded revision is dropped.
#[allow(clippy::too_many_arguments)]
pub async fn on_agent_state_patch(
    ctx: &Context,
    agent: i64,
    stamp: Stamp,
    session_id: &str,
    global_rev: u64,
    state_name: &str,
    op: &str,
    path: &str,
    value: &Value,
    old_value: &Value,
    task_id: Option<&str>,
) -> PersistResult<()> {
    let state = state_id(ctx, agent, state_name).await?;
    let session = get_or_create_session(&ctx.db, agent, session_id).await?;
    let mut task = task_id
        .filter(|t| !is_probe_task(t))
        .and_then(|t| t.parse::<i64>().ok());
    if let Some(id) = task {
        let owned: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM facade_task WHERE id = $1 AND agent_id = $2)",
        )
        .bind(id)
        .bind(agent)
        .fetch_one(&ctx.db)
        .await?;
        if !owned {
            task = None;
        }
    }
    let inserted: Result<i64, sqlx::Error> = sqlx::query_scalar(
        "INSERT INTO facade_patch
            (state_id, agent_id, session_id, interface, op, path, value, old_value, task_id, global_rev,
             timestamp, agent_pos, agent_ts, step)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now(), $11, $12, $13)
         RETURNING id",
    )
    .bind(state)
    .bind(agent)
    .bind(session)
    .bind(state_name)
    .bind(op)
    .bind(path)
    .bind(sqlx::types::Json(value))
    .bind((!old_value.is_null()).then_some(sqlx::types::Json(old_value)))
    .bind(task)
    .bind(global_rev as i32)
    .bind(stamp.agent_pos)
    .bind(stamp.agent_ts)
    .bind(stamp.step)
    .fetch_one(&ctx.db)
    .await;
    match inserted {
        Ok(id) => {
            signals::patch_created(ctx, id).await;
            Ok(())
        }
        Err(sqlx::Error::Database(e))
            if e.constraint() == Some("patch_unique_rev_per_session_state") =>
        {
            // One patch per (session, global_rev, state): this revision is already recorded.
            tracing::info!(
                agent,
                state_name,
                global_rev,
                session_id,
                "dropping a duplicate patch"
            );
            Ok(())
        }
        Err(e) => Err(e.into()),
    }
}

/// Snapshots of the agent's states at a revision (`on_agent_state_snapshot`, and
/// `on_agent_session_init` with revision 0).
pub async fn on_agent_snapshots(
    ctx: &Context,
    agent: i64,
    session_id: &str,
    global_rev: u64,
    snapshots: &Map<String, Value>,
) -> PersistResult<()> {
    let session = get_or_create_session(&ctx.db, agent, session_id).await?;
    for (state_name, snapshot) in snapshots {
        let state = state_id(ctx, agent, state_name).await?;
        sqlx::query(
            "INSERT INTO facade_snapshot (session_id, state_id, agent_id, value, global_rev, timestamp)
             VALUES ($1, $2, $3, $4, $5, now())",
        )
        .bind(session)
        .bind(state)
        .bind(agent)
        .bind(sqlx::types::Json(snapshot))
        .bind(global_rev as i32)
        .execute(&ctx.db)
        .await?;
    }
    Ok(())
}

/// Record that `task` holds lock `key` on this agent (`on_agent_lock`). An unknown task is
/// ignored: a stray lock must not tear down the transport.
pub async fn on_agent_lock(ctx: &Context, agent: i64, key: &str, task: &str) -> PersistResult<()> {
    let Ok(task_id) = task.parse::<i64>() else {
        return Err(PersistError::Refused(format!(
            "Field 'id' expected a number but got {task:?}"
        )));
    };
    let exists: bool =
        sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM facade_task WHERE id = $1)")
            .bind(task_id)
            .fetch_one(&ctx.db)
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
        "INSERT INTO facade_lock (agent_id, key, hold_by_id, created_at, updated_at) VALUES ($1, $2, $3, now(), now())
         ON CONFLICT (agent_id, key) DO UPDATE SET hold_by_id = EXCLUDED.hold_by_id, updated_at = now()",
    )
    .bind(agent)
    .bind(key)
    .bind(task_id)
    .execute(&ctx.db)
    .await?;
    Ok(())
}

/// Release: clear the holder, a no-op if the lock is absent or already free (`on_agent_unlock`).
pub async fn on_agent_unlock(ctx: &Context, agent: i64, key: &str) -> PersistResult<()> {
    sqlx::query("UPDATE facade_lock SET hold_by_id = NULL WHERE agent_id = $1 AND key = $2")
        .bind(agent)
        .bind(key)
        .execute(&ctx.db)
        .await?;
    Ok(())
}

/// Route one of the agent's state or lock reports. Returns whether it was one.
pub async fn on_state(ctx: &Context, agent: i64, frame: &AgentFrame) -> PersistResult<bool> {
    use crate::messages::FromAgent;
    match &frame.message {
        FromAgent::StatePatch {
            session_id,
            global_rev,
            state_name,
            op,
            path,
            value,
            old_value,
            task_id,
            ..
        } => {
            on_agent_state_patch(
                ctx,
                agent,
                position_stamp(frame),
                session_id,
                *global_rev,
                state_name,
                op,
                path,
                value,
                old_value,
                task_id.as_deref(),
            )
            .await?
        }
        FromAgent::StateSnapshot {
            session_id,
            global_rev,
            snapshots,
        } => on_agent_snapshots(ctx, agent, session_id, *global_rev, snapshots).await?,
        FromAgent::SessionInit { session_id, states } => {
            on_agent_snapshots(ctx, agent, session_id, 0, states).await?
        }
        FromAgent::Lock { key, task } => on_agent_lock(ctx, agent, key, task).await?,
        FromAgent::Unlock { key, .. } => on_agent_unlock(ctx, agent, key).await?,
        _ => return Ok(false),
    }
    Ok(true)
}

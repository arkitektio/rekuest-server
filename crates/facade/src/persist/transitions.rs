//! The task transition primitive: the kernel everything else builds on
//! (`facade/persist/transitions.py`).
//!
//! One place decides "is this task still open, and may I move it?", and it decides it under a
//! row lock, writing the `TaskEvent` in the same transaction. Sweeps are re-entrant and run in
//! every backend, and an agent retries an unacked terminal report over whatever connection it
//! has next: reading the flag and then writing it would let two of them both finish a task.

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};
use sqlx::{PgPool, Postgres, Transaction};

use crate::context::Context;
use crate::signals;

/// The kinds that end a task (`_TERMINAL_KINDS`).
pub const TERMINAL_KINDS: &[&str] = &[
    "COMPLETED",
    "CANCELLED",
    "INTERRUPTED",
    "FAILED",
    "CRITICAL",
    "LOST",
];

pub fn is_terminal(kind: &str) -> bool {
    TERMINAL_KINDS.contains(&kind)
}

/// What a claim reads of the task under its lock.
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct TaskRow {
    pub id: i64,
    pub agent_id: i64,
    pub is_done: bool,
    pub latest_event_kind: String,
    pub is_higher_order_child: bool,
    pub parent_id: Option<i64>,
    pub implementation_id: Option<i64>,
    pub picked_up_at: Option<DateTime<Utc>>,
    pub dispatched_at: Option<DateTime<Utc>>,
    pub created_at: DateTime<Utc>,
}

pub const TASK_ROW: &str =
    "id, agent_id, is_done, latest_event_kind, is_higher_order_child, parent_id, \
     implementation_id, picked_up_at, dispatched_at, created_at";

/// The fields of a `TaskEvent` besides its task and kind; all optional, as on the model.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct NewEvent {
    pub message: Option<String>,
    pub returns: Option<Map<String, Value>>,
    pub progress: Option<i64>,
    pub level: Option<String>,
    pub agent_pos: Option<i64>,
    pub agent_ts: Option<DateTime<Utc>>,
    pub step: Option<i64>,
    pub effect: Option<String>,
    pub key: Option<String>,
    pub value: Option<Value>,
    pub delegated_to: Option<i64>,
}

impl NewEvent {
    pub fn stamped(stamp: crate::persist::positions::Stamp) -> Self {
        Self {
            agent_pos: stamp.agent_pos,
            agent_ts: stamp.agent_ts,
            step: stamp.step,
            ..Self::default()
        }
    }

    pub fn with_message(mut self, message: Option<String>) -> Self {
        self.message = message;
        self
    }
}

/// Insert a `TaskEvent` (`TaskEvent.objects.create`); its id.
pub async fn insert_event(
    executor: impl sqlx::PgExecutor<'_>,
    task: i64,
    kind: &str,
    event: &NewEvent,
) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar(
        "INSERT INTO facade_taskevent
            (task_id, kind, created_at, message, returns, progress, level, agent_pos, agent_ts, step,
             effect, key, value, delegated_to_id)
         VALUES ($1, $2, now(), $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
         RETURNING id",
    )
    .bind(task)
    .bind(kind)
    .bind(&event.message)
    .bind(event.returns.as_ref().map(|r| sqlx::types::Json(Value::Object(r.clone()))))
    .bind(event.progress.map(|p| p as i32))
    .bind(&event.level)
    .bind(event.agent_pos)
    .bind(event.agent_ts)
    .bind(event.step)
    .bind(&event.effect)
    .bind(&event.key)
    .bind(event.value.as_ref().map(sqlx::types::Json))
    .bind(event.delegated_to)
    .fetch_one(executor)
    .await
}

/// Move a task's `latest_event_kind` (and, `done`, finish it), bumping `revision` and
/// `updated_at` in the database, as every `Task.save()` does.
pub async fn update_task_kind(
    tx: &mut Transaction<'_, Postgres>,
    task: i64,
    kind: &str,
    done: bool,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE facade_task
            SET latest_event_kind = $2,
                is_done = CASE WHEN $3 THEN true ELSE is_done END,
                finished_at = CASE WHEN $3 THEN now() ELSE finished_at END,
                revision = revision + 1, updated_at = now()
          WHERE id = $1",
    )
    .bind(task)
    .bind(kind)
    .bind(done)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

/// A transition to take under the lock (`_claim_task_transition_sync`'s arguments).
pub struct Claim<'a> {
    pub to_kind: &'a str,
    pub mark_done: bool,
    /// Lose the claim when the task already reads this kind.
    pub skip_if_kind: Option<&'a str>,
    /// Lose the claim unless this holds for the locked row.
    pub only_if: Option<&'a (dyn Fn(&TaskRow) -> bool + Sync)>,
    /// The event to write in the same transaction; `None` writes none.
    pub event: Option<NewEvent>,
    /// Step over a row another backend holds instead of waiting for it (sweeps).
    pub skip_locked: bool,
}

impl<'a> Claim<'a> {
    pub fn to(kind: &'a str) -> Self {
        Self {
            to_kind: kind,
            mark_done: false,
            skip_if_kind: None,
            only_if: None,
            event: Some(NewEvent::default()),
            skip_locked: false,
        }
    }
}

/// Take a task transition under a row lock. Returns whether we won (`_claim`). Losing (already
/// done, already `skip_if_kind`, `only_if` no longer holds, or the row is gone) means somebody
/// else handled it. On a win, the task and its event are fanned out after the commit.
pub async fn claim(ctx: &Context, task: i64, claim: Claim<'_>) -> Result<bool, sqlx::Error> {
    let mut tx = ctx.db.begin().await?;
    let lock = if claim.skip_locked {
        "FOR UPDATE SKIP LOCKED"
    } else {
        "FOR UPDATE"
    };
    let row: Option<TaskRow> = sqlx::query_as(&format!(
        "SELECT {TASK_ROW} FROM facade_task WHERE id = $1 {lock}"
    ))
    .bind(task)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(row) = row else {
        return Ok(false);
    };
    if row.is_done
        || claim
            .skip_if_kind
            .is_some_and(|kind| row.latest_event_kind == kind)
    {
        return Ok(false);
    }
    if claim.only_if.is_some_and(|only_if| !only_if(&row)) {
        return Ok(false);
    }
    update_task_kind(&mut tx, task, claim.to_kind, claim.mark_done).await?;
    let event = match &claim.event {
        Some(event) => Some(insert_event(&mut *tx, task, claim.to_kind, event).await?),
        None => None,
    };
    tx.commit().await?;
    signals::task_saved(ctx, task, false).await;
    if let Some(event) = event {
        signals::task_event_created(ctx, event).await;
    }
    Ok(true)
}

/// The wrapper's returns for a child's (`higher_order.project_returns`): `return_map` is
/// `{higher_key: lower_key}`; empty means identity.
pub fn project_returns(
    config: &Value,
    lower: Option<&Map<String, Value>>,
) -> Option<Map<String, Value>> {
    let lower = lower?;
    let map = config
        .get("return_map")
        .and_then(Value::as_object)
        .filter(|m| !m.is_empty());
    Some(match map {
        None => lower.clone(),
        Some(map) => map
            .iter()
            .map(|(higher, lower_key)| {
                let value = lower_key
                    .as_str()
                    .and_then(|k| lower.get(k))
                    .cloned()
                    .unwrap_or(Value::Null);
                (higher.clone(), value)
            })
            .collect(),
    })
}

/// If `child` is the child of a higher-order wrapper, re-emit a mapped event on the wrapper
/// (`_unfold_to_higher_order`), through the claim like every other transition.
pub async fn unfold_to_higher_order(
    ctx: &Context,
    child: i64,
    kind: &str,
    returns: Option<&Map<String, Value>>,
    message: Option<&str>,
    is_higher_order_child: Option<bool>,
) -> Result<(), sqlx::Error> {
    let flagged = match is_higher_order_child {
        Some(flag) => flag,
        None => sqlx::query_scalar::<_, bool>(
            "SELECT is_higher_order_child FROM facade_task WHERE id = $1",
        )
        .bind(child)
        .fetch_optional(&ctx.db)
        .await?
        .unwrap_or(false),
    };
    if !flagged {
        return Ok(());
    }
    let parent: Option<(i64, Option<i64>, Option<sqlx::types::Json<Value>>)> = sqlx::query_as(
        "SELECT p.id, i.higher_order_for_id, i.higher_order_config
           FROM facade_task c JOIN facade_task p ON p.id = c.parent_id
           LEFT JOIN facade_implementation i ON i.id = p.implementation_id
          WHERE c.id = $1",
    )
    .bind(child)
    .fetch_optional(&ctx.db)
    .await?;
    let Some((parent, Some(_), config)) = parent else {
        return Ok(()); // not a higher-order child
    };
    let config = config.map(|c| c.0).unwrap_or(Value::Null);
    let mut event = NewEvent {
        delegated_to: Some(child),
        message: message.map(str::to_owned),
        ..NewEvent::default()
    };
    if kind == "YIELD" {
        event.returns = project_returns(&config, returns);
    }
    claim(
        ctx,
        parent,
        Claim {
            mark_done: is_terminal(kind),
            event: Some(event),
            ..Claim::to(kind)
        },
    )
    .await?;
    Ok(())
}

/// Finalize a task the SERVER decided is over, and project it onto any wrapper
/// (`_finalize_terminal`). Returns whether we won.
pub async fn finalize_terminal(
    ctx: &Context,
    task: i64,
    kind: &str,
    message: &str,
    value: Option<Value>,
    only_if: Option<&(dyn Fn(&TaskRow) -> bool + Sync)>,
    skip_locked: bool,
) -> Result<bool, sqlx::Error> {
    let won = claim(
        ctx,
        task,
        Claim {
            mark_done: true,
            only_if,
            skip_locked,
            event: Some(NewEvent {
                message: Some(message.to_owned()),
                value,
                ..NewEvent::default()
            }),
            ..Claim::to(kind)
        },
    )
    .await?;
    if won {
        unfold_to_higher_order(ctx, task, kind, None, Some(message), None).await?;
    }
    Ok(won)
}

/// What is known about a lost task, for whoever decides next (`_lost_details_sync`): whether it
/// was ever picked up, its last progress, and what running it again would do.
pub async fn lost_details(
    db: &PgPool,
    task: i64,
    started: bool,
    reason: &str,
) -> Result<Value, sqlx::Error> {
    let last_progress: Option<i32> = sqlx::query_scalar(
        "SELECT progress FROM facade_taskevent
          WHERE task_id = $1 AND kind = 'PROGRESS' AND progress IS NOT NULL ORDER BY id DESC LIMIT 1",
    )
    .bind(task)
    .fetch_optional(db)
    .await?;
    let effects: Option<String> = sqlx::query_scalar(
        "SELECT i.effects FROM facade_task t JOIN facade_implementation i ON i.id = t.implementation_id WHERE t.id = $1",
    )
    .bind(task)
    .fetch_optional(db)
    .await?;
    // A workflow sent again to be resumed is "not picked up" again, but it did start once.
    let started = started
        || sqlx::query_scalar::<_, bool>("SELECT EXISTS (SELECT 1 FROM facade_taskevent WHERE task_id = $1 AND kind = 'STARTED')")
            .bind(task)
            .fetch_one(db)
            .await?;
    Ok(serde_json::json!({
        "started": started,
        "last_progress": last_progress,
        "effects": effects.unwrap_or_else(|| "UNKNOWN".into()),
        "reason": reason,
    }))
}

/// End a task LOST: its agent is gone, and so is any way of knowing how it ended
/// (`_finalize_lost`). The event keeps what is known; LOST is final.
pub async fn finalize_lost(
    ctx: &Context,
    task: i64,
    reason: &str,
    started: bool,
    only_if: Option<&(dyn Fn(&TaskRow) -> bool + Sync)>,
    skip_locked: bool,
) -> Result<bool, sqlx::Error> {
    let details = lost_details(&ctx.db, task, started, reason).await?;
    finalize_terminal(
        ctx,
        task,
        "LOST",
        reason,
        Some(details),
        only_if,
        skip_locked,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn returns_project_through_the_return_map() {
        let lower: Map<String, Value> =
            serde_json::from_value(serde_json::json!({"a": 1, "b": 2})).unwrap();
        assert_eq!(
            project_returns(&Value::Null, Some(&lower)),
            Some(lower.clone())
        );
        let mapped = project_returns(
            &serde_json::json!({"return_map": {"x": "b", "y": "missing"}}),
            Some(&lower),
        )
        .unwrap();
        assert_eq!(
            Value::Object(mapped),
            serde_json::json!({"x": 2, "y": null})
        );
        assert_eq!(project_returns(&Value::Null, None), None);
    }
}

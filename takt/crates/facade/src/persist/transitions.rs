//! The task transition primitive: the kernel everything else builds on
//! (`facade/persist/transitions.py`).
//!
//! One place decides "is this task still open, and may I move it?", and it decides it under a
//! row lock, writing the `TaskEvent` in the same transaction. Sweeps are re-entrant and run in
//! every backend, and an agent retries an unacked terminal report over whatever connection it
//! has next: reading the flag and then writing it would let two of them both finish a task.

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};
use sqlx::{PgConnection, PgPool};

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
    "DENIED",
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
    pub latest_instruct_kind: String,
    pub not_before: Option<DateTime<Utc>>,
    pub dispatch_attempts: i16,
    pub interrupt_at: Option<DateTime<Utc>>,
}

impl TaskRow {
    /// A delayed task nobody has handed over yet (`_is_waiting`): waiting, not undelivered.
    pub fn is_waiting(&self) -> bool {
        self.not_before.is_some() && self.dispatch_attempts == 0
    }

    /// When its pickup clock started: its last dispatch, or its creation if it never left.
    pub fn pickup_clock(&self) -> DateTime<Utc> {
        self.dispatched_at.unwrap_or(self.created_at)
    }
}

pub const TASK_ROW: &str =
    "id, agent_id, is_done, latest_event_kind, is_higher_order_child, parent_id, \
     implementation_id, picked_up_at, dispatched_at, created_at, latest_instruct_kind, \
     not_before, dispatch_attempts, interrupt_at";

/// The fields of a `TaskEvent` besides its task and kind; all optional, as on the model.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct NewEvent {
    pub message: Option<String>,
    pub returns: Option<Map<String, Value>>,
    pub progress: Option<i64>,
    pub level: Option<String>,
    pub agent_pos: Option<i64>,
    pub agent_session: Option<String>,
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
            agent_session: stamp.agent_session,
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
///
/// The same statement keeps the task's latest report (`latest_progress`, `latest_message`,
/// `latest_returns`), so a client following a task reads three columns instead of its whole
/// event history. Every event is written here, so nothing else has to keep them. They summarise
/// the events, so `revision` does not move.
///
/// A report is one event: the event of a report sent again (same task, journal session and
/// position) is the one already there, and its id is what comes back. Nothing is written then.
///
/// And the same statement keeps the event for the agent that asked for the task, when a socket
/// agent's running task did: a row in its commands that names the event
/// (`consumers::agent_queue`). An event and its way to whoever waits for it are one write, so
/// no caller is left waiting for an end that was recorded. `PROGRESS` is the exception: its
/// fan-out is thinned (`progress_gate`), and what is thinned is decided after the commit
/// (`signals::task_event_created`). A `LOG` is only kept for an agent that listens.
///
/// Every event is numbered within its task (`task_seq`, 1, 2, 3 …) in commit order: the task
/// row is updated (its `event_seq` taken, its latest report kept) **before** the event is
/// inserted, so the row's lock is held from the moment the number is taken until commit, and
/// the next writer of the task waits and takes the number after. That is what makes "every
/// event after `task_seq` n" a cursor that misses none (a caller session's, `session/`), where
/// "after id n" could skip one that committed late. A report sent again is found first and
/// takes no number.
pub async fn insert_event(
    executor: impl sqlx::PgExecutor<'_>,
    task: i64,
    kind: &str,
    event: &NewEvent,
) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar(
        "WITH existing AS (
            SELECT id FROM facade_taskevent
             WHERE task_id = $1 AND agent_session = $14 AND agent_pos = $7
             LIMIT 1
         ), numbered AS (
            UPDATE facade_task t
               SET event_seq = t.event_seq + 1,
                   latest_progress = CASE WHEN $2 = 'PROGRESS' AND $5::int IS NOT NULL
                                          THEN $5::int ELSE t.latest_progress END,
                   latest_message = COALESCE($3, t.latest_message),
                   latest_returns = CASE WHEN $2 = 'YIELD' THEN $4::jsonb ELSE t.latest_returns END
             WHERE t.id = $1 AND NOT EXISTS (SELECT 1 FROM existing)
             RETURNING t.event_seq
         ), event AS (
            INSERT INTO facade_taskevent
                (task_id, kind, message, returns, progress, level, agent_pos, agent_ts, step, effect,
                 key, value, delegated_to_id, agent_session, task_seq)
            SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, numbered.event_seq
              FROM numbered
             ON CONFLICT (task_id, agent_session, agent_pos)
                WHERE agent_session IS NOT NULL AND agent_pos IS NOT NULL DO NOTHING
             RETURNING id, task_id, kind, message, returns, progress
         ), mirrored AS (
            INSERT INTO facade_command (agent_id, kind, task, event)
            SELECT pa.id, 'EVENT', event.task_id, event.id
              FROM event
              JOIN facade_task t ON t.id = event.task_id
              JOIN facade_task p ON p.id = t.parent_id AND p.is_done = false
              JOIN facade_caller c ON c.id = t.caller_id
              JOIN facade_agent pa ON pa.id = p.agent_id AND pa.kind <> 'WEBHOOK'
                   AND pa.client_id = c.client_id AND pa.user_id = c.user_id
                   AND pa.organization_id = c.organization_id
             WHERE event.kind = ANY($15) AND event.kind <> 'PROGRESS'
               AND (event.kind <> 'LOG' OR pa.connected)
         )
         SELECT id FROM event
         UNION ALL
         SELECT id FROM existing
          LIMIT 1",
    )
    .bind(task)
    .bind(kind)
    .bind(&event.message)
    .bind(
        event
            .returns
            .as_ref()
            .map(|r| sqlx::types::Json(Value::Object(r.clone()))),
    )
    .bind(event.progress.map(|p| p as i32))
    .bind(&event.level)
    .bind(event.agent_pos)
    .bind(event.agent_ts)
    .bind(event.step)
    .bind(&event.effect)
    .bind(&event.key)
    .bind(event.value.as_ref().map(sqlx::types::Json))
    .bind(event.delegated_to)
    .bind(&event.agent_session)
    .bind(crate::caller_events::MIRRORED_KINDS)
    .fetch_one(executor)
    .await
}

/// Number the events written without a `task_seq` (by a takt older than this one, between the
/// migration and this start), after their task's counter, in id order; moves each counter on.
/// Run once at start, before anything is served: a caller session's replay reads by
/// `task_seq`, and must never pass over a row that has none. How many were numbered.
pub async fn number_unnumbered_events(db: &sqlx::PgPool) -> Result<u64, sqlx::Error> {
    let done = sqlx::query(
        "WITH pending AS (
            SELECT e.id, e.task_id, row_number() OVER (PARTITION BY e.task_id ORDER BY e.id) AS rn
              FROM facade_taskevent e
             WHERE e.task_seq IS NULL
         ), bumped AS (
            UPDATE facade_task t
               SET event_seq = t.event_seq + c.n
              FROM (SELECT task_id, count(*) AS n FROM pending GROUP BY task_id) c
             WHERE t.id = c.task_id
            RETURNING t.id, t.event_seq - c.n AS base
         )
         UPDATE facade_taskevent e
            SET task_seq = b.base + p.rn
           FROM pending p JOIN bumped b ON b.id = p.task_id
          WHERE e.id = p.id",
    )
    .execute(db)
    .await?;
    Ok(done.rows_affected())
}

/// Move a task's `latest_event_kind` (and, `done`, finish it), bumping `revision` and
/// `updated_at` in the database, as every `Task.save()` does.
pub async fn update_task_kind(
    conn: &mut PgConnection,
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
    .execute(&mut *conn)
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
    /// More columns to set with the transition (`extra`), as a SQL `SET` list; `$2` in it is
    /// the application clock's now, the clock every deadline is measured by.
    pub extra: Option<&'a str>,
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
            extra: None,
        }
    }
}

/// Take a task transition under a row lock. Returns whether we won (`_claim`). Losing (already
/// done, already `skip_if_kind`, `only_if` no longer holds, or the row is gone) means somebody
/// else handled it. On a win, the task and its event are fanned out after the commit.
pub async fn claim(ctx: &Context, task: i64, claim: Claim<'_>) -> Result<bool, sqlx::Error> {
    let mut tx = ctx.db.begin().await?;
    let Some(event) = claim_in(&mut tx, task, &claim).await? else {
        return Ok(false);
    };
    tx.commit().await?;
    signals::task_saved(ctx, task, false).await;
    if let Some(event) = event {
        signals::task_event_created(ctx, event).await;
    }
    if claim.mark_done {
        crate::holds::settle_held_children(ctx, task).await?;
    }
    Ok(true)
}

/// [`claim`] inside a transaction of the caller's: `None` when the claim was lost, else the
/// event it wrote (if it writes one). Whoever commits fans the task and that event out.
pub async fn claim_in(
    conn: &mut PgConnection,
    task: i64,
    claim: &Claim<'_>,
) -> Result<Option<Option<i64>>, sqlx::Error> {
    let lock = if claim.skip_locked {
        "FOR UPDATE SKIP LOCKED"
    } else {
        "FOR UPDATE"
    };
    let row: Option<TaskRow> = sqlx::query_as(&format!(
        "SELECT {TASK_ROW} FROM facade_task WHERE id = $1 {lock}"
    ))
    .bind(task)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(row) = row else {
        return Ok(None);
    };
    if row.is_done
        || claim
            .skip_if_kind
            .is_some_and(|kind| row.latest_event_kind == kind)
    {
        return Ok(None);
    }
    if claim.only_if.is_some_and(|only_if| !only_if(&row)) {
        return Ok(None);
    }
    update_task_kind(&mut *conn, task, claim.to_kind, claim.mark_done).await?;
    if let Some(extra) = claim.extra {
        sqlx::query(&format!("UPDATE facade_task SET {extra} WHERE id = $1"))
            .bind(task)
            .bind(Utc::now())
            .execute(&mut *conn)
            .await?;
    }
    if claim.mark_done {
        crate::trace::measures::ended(row.created_at, claim.to_kind);
    }
    Ok(Some(match &claim.event {
        Some(event) => Some(insert_event(&mut *conn, task, claim.to_kind, event).await?),
        None => None,
    }))
}

/// The wrapper's returns for a child's (`higher_order.project_returns`).
///
/// `returns_key` is the way back of `args_key`: the lower implementation returns the
/// wrapper's values packed in one dict port, and that dict is what is mapped (a missing or
/// non-dict port is no values). `return_map` is then `{higher_key: lower_key}`; empty means
/// identity.
pub fn project_returns(
    config: &Value,
    lower: Option<&Map<String, Value>>,
) -> Option<Map<String, Value>> {
    let lower = lower?;
    let unpacked;
    let lower = match config.get("returns_key").and_then(Value::as_str) {
        Some(key) => {
            unpacked = lower
                .get(key)
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            &unpacked
        }
        None => lower,
    };
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

/// A higher-order child yielded: re-emit the mapped yield on its wrapper, inside the transaction
/// that recorded the child's ([`unfold_to_higher_order`] for a `YIELD`, without a commit of its
/// own). The wrapper and the event to fan out once that transaction commits, if there is one.
pub async fn unfold_yield_in(
    conn: &mut PgConnection,
    child: i64,
    returns: &Map<String, Value>,
) -> Result<Option<(i64, Option<i64>)>, sqlx::Error> {
    let parent: Option<(i64, Option<i64>, Option<sqlx::types::Json<Value>>)> = sqlx::query_as(
        "SELECT p.id, i.higher_order_for_id, i.higher_order_config
           FROM facade_task c JOIN facade_task p ON p.id = c.parent_id
           LEFT JOIN facade_implementation i ON i.id = p.implementation_id
          WHERE c.id = $1",
    )
    .bind(child)
    .fetch_optional(&mut *conn)
    .await?;
    let Some((parent, Some(_), config)) = parent else {
        return Ok(None); // not a higher-order child
    };
    let config = config.map(|c| c.0).unwrap_or(Value::Null);
    let event = NewEvent {
        delegated_to: Some(child),
        returns: project_returns(&config, Some(returns)),
        ..NewEvent::default()
    };
    let claimed = claim_in(
        conn,
        parent,
        &Claim {
            event: Some(event),
            ..Claim::to("YIELD")
        },
    )
    .await?;
    Ok(claimed.map(|event| (parent, event)))
}

/// How [`finalize_terminal`] may lose its claim, and what else it sets (its keyword arguments).
#[derive(Default)]
pub struct Guard<'a> {
    /// Lose the claim unless this holds for the locked row.
    pub only_if: Option<&'a (dyn Fn(&TaskRow) -> bool + Sync)>,
    /// More columns to set, as in [`Claim::extra`].
    pub extra: Option<&'a str>,
    /// Step over a row another backend holds (sweeps).
    pub skip_locked: bool,
}

/// Finalize a task the SERVER decided is over, and project it onto any wrapper
/// (`_finalize_terminal`). Returns whether we won.
pub async fn finalize_terminal(
    ctx: &Context,
    task: i64,
    kind: &str,
    message: &str,
    value: Option<Value>,
    guard: Guard<'_>,
) -> Result<bool, sqlx::Error> {
    let won = claim(
        ctx,
        task,
        Claim {
            mark_done: true,
            only_if: guard.only_if,
            extra: guard.extra,
            skip_locked: guard.skip_locked,
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
        Guard {
            only_if,
            extra: None,
            skip_locked,
        },
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

    #[test]
    fn a_returns_key_unpacks_the_dict_the_lower_returns() {
        let lower: Map<String, Value> = serde_json::from_value(
            serde_json::json!({"returns": {"return0": "img-mask", "return1": 7}}),
        )
        .unwrap();
        let unpacked =
            project_returns(&serde_json::json!({"returns_key": "returns"}), Some(&lower)).unwrap();
        assert_eq!(
            Value::Object(unpacked),
            serde_json::json!({"return0": "img-mask", "return1": 7})
        );
        let mapped = project_returns(
            &serde_json::json!({"returns_key": "returns", "return_map": {"mask": "return0"}}),
            Some(&lower),
        )
        .unwrap();
        assert_eq!(
            Value::Object(mapped),
            serde_json::json!({"mask": "img-mask"})
        );
        let missing =
            project_returns(&serde_json::json!({"returns_key": "other"}), Some(&lower)).unwrap();
        assert!(missing.is_empty());
    }
}

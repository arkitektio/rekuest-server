//! Matching signals to triggers and firing them.
//!
//! The reaper's `triggers` sweep claims unprocessed signals under `SKIP LOCKED` (any number of
//! replicas may run it) and assigns every matching trigger's action.
//!
//! A trigger matches a signal when kind, structure identifier and organization agree and the
//! signal's descriptors satisfy BOTH the trigger's own conditions and the target port's own
//! `requires` (its `compiled_jsonpath`): a trigger can never feed an action an object the
//! action has declared it cannot take. Both are checked in one query with the same
//! `jsonb_path_match` the action search uses; a path that errors (a type mismatch against the
//! object) reads as NULL, which is not a match.
//!
//! A run caused by a task is that task's child (`parent` = the causing task), so it joins the
//! causing tree, and its provenance token names the tree's human at the root. Each trigger and
//! signal pair runs at most once: the run's reference is `trigger:<id>:<signal>`, unique per
//! caller. `trigger_depth` bounds chains of triggers feeding each other.

use serde_json::{json, Map, Value};
use sqlx::types::Json;

use crate::backend::{self, AssignInput, AssignOrigin};
use crate::caller_context::CallerContext;
use crate::context::Context;

#[derive(sqlx::FromRow)]
struct Signal {
    id: i64,
    kind: String,
    identifier: String,
    object: String,
    descriptors: Json<Value>,
    organization_id: i64,
    causing_task_id: Option<i64>,
    causing_depth: Option<i16>,
}

#[derive(sqlx::FromRow)]
struct Trigger {
    id: i64,
    action_id: i64,
    agent_id: Option<i64>,
    interface: Option<String>,
    port: String,
    args: Json<Value>,
    consecutive_failures: i32,
    last_error: Option<String>,
    user_id: i64,
    client_id: i64,
    organization_id: i64,
}

/// The enabled triggers of the signal's organization this signal fires (`matching_triggers`).
const MATCHING: &str = "
SELECT t.id, t.action_id, t.agent_id, t.interface, t.port, t.args, t.consecutive_failures, t.last_error,
       c.user_id, c.client_id, c.organization_id
  FROM facade_trigger t
  JOIN facade_caller c ON c.id = t.caller_id
  LEFT JOIN facade_argport p ON p.action_id = t.action_id AND p.parent_id IS NULL AND p.key = t.port
 WHERE t.enabled AND t.kind = $1 AND t.identifier = $2 AND c.organization_id = $3
   AND (t.compiled_jsonpath IS NULL OR jsonb_path_match($4::jsonb, t.compiled_jsonpath::jsonpath, '{}'::jsonb, true) IS TRUE)
   AND (p.compiled_jsonpath IS NULL OR jsonb_path_match($4::jsonb, p.compiled_jsonpath::jsonpath, '{}'::jsonb, true) IS TRUE)
 ORDER BY t.id";

impl Trigger {
    fn assign_input(&self, signal: &Signal) -> AssignInput {
        let mut args = match &self.args.0 {
            Value::Object(args) => args.clone(),
            _ => Map::new(),
        };
        args.insert(
            self.port.clone(),
            json!({"__identifier": signal.identifier, "object": signal.object}),
        );
        let mut input = AssignInput {
            args,
            reference: Some(format!("trigger:{}:{}", self.id, signal.id)),
            parent: signal.causing_task_id.map(|task| task.to_string()),
            ..AssignInput::default()
        };
        match self.agent_id {
            Some(agent) => {
                input.agent = Some(agent.to_string());
                input.interface = self.interface.clone();
            }
            None => input.action = Some(self.action_id.to_string()),
        }
        input
    }
}

/// Note how a firing went (`_record`): a failure counts and is kept, a success clears both.
async fn record(
    conn: &mut sqlx::PgConnection,
    trigger: &Trigger,
    error: Option<String>,
) -> Result<(), sqlx::Error> {
    match error {
        None if trigger.consecutive_failures == 0 && trigger.last_error.is_none() => Ok(()),
        None => sqlx::query(
            "UPDATE facade_trigger SET consecutive_failures = 0, last_error = NULL, updated_at = now() WHERE id = $1",
        )
        .bind(trigger.id)
        .execute(conn)
        .await
        .map(|_| ()),
        Some(error) => {
            tracing::warn!("Trigger {} did not fire: {error}", trigger.id);
            sqlx::query(
                "UPDATE facade_trigger SET consecutive_failures = consecutive_failures + 1, last_error = $2,
                        updated_at = now() WHERE id = $1",
            )
            .bind(trigger.id)
            .bind(error)
            .execute(conn)
            .await
            .map(|_| ())
        }
    }
}

/// Match and fire one unprocessed signal (`fire_one`); the number of runs created.
pub async fn fire_one(ctx: &Context, signal_id: i64) -> Result<usize, sqlx::Error> {
    let mut tx = ctx.db.begin().await?;
    let signal: Option<Signal> = sqlx::query_as(
        "SELECT s.id, s.kind, s.identifier, s.object, s.descriptors, s.organization_id, s.causing_task_id,
                t.trigger_depth AS causing_depth
           FROM facade_signal s LEFT JOIN facade_task t ON t.id = s.causing_task_id
          WHERE s.id = $1 AND s.processed_at IS NULL FOR NO KEY UPDATE OF s SKIP LOCKED",
    )
    .bind(signal_id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(signal) = signal else {
        return Ok(0);
    };
    let depth = signal.causing_depth.map_or(1, |depth| depth + 1);
    let limit = ctx.settings.trigger_max_depth;
    let triggers: Vec<Trigger> = sqlx::query_as(MATCHING)
        .bind(&signal.kind)
        .bind(&signal.identifier)
        .bind(signal.organization_id)
        .bind(&signal.descriptors)
        .fetch_all(&mut *tx)
        .await?;
    let mut created = 0;
    for trigger in &triggers {
        if depth > limit {
            record(
                &mut tx,
                trigger,
                Some(format!(
                    "Not fired for signal {}: {depth} triggers deep (limit {limit}) — a trigger loop?",
                    signal.id
                )),
            )
            .await?;
            continue;
        }
        let fired = async {
            let principal = CallerContext::load(
                &ctx.db,
                trigger.user_id,
                trigger.client_id,
                Some(trigger.organization_id),
                vec![],
            )
            .await
            .map_err(|e| e.to_string())?;
            backend::assign_with_status(
                ctx,
                &principal,
                &trigger.assign_input(&signal),
                AssignOrigin {
                    signal: Some(signal.id),
                    trigger: Some(trigger.id),
                    trigger_depth: depth,
                    ..AssignOrigin::default()
                },
            )
            .await
            .map_err(|e| e.to_string())
        }
        .await;
        match fired {
            Ok(assigned) => {
                created += usize::from(assigned.created);
                record(&mut tx, trigger, None).await?;
            }
            // The target is broken, not the signal: record, move on.
            Err(error) => {
                record(
                    &mut tx,
                    trigger,
                    Some(format!("Could not run for signal {}: {error}", signal.id)),
                )
                .await?;
            }
        }
    }
    sqlx::query("UPDATE facade_signal SET processed_at = now() WHERE id = $1")
        .bind(signal.id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(created)
}

/// One pass over the unprocessed signals, oldest first (`fire_triggers`): the runs created.
pub async fn fire_triggers(ctx: &Context, limit: i64) -> Result<usize, sqlx::Error> {
    let pending: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM facade_signal WHERE processed_at IS NULL ORDER BY received_at LIMIT $1",
    )
    .bind(limit)
    .fetch_all(&ctx.db)
    .await?;
    let mut created = 0;
    for signal in pending {
        created += fire_one(ctx, signal).await?;
    }
    Ok(created)
}

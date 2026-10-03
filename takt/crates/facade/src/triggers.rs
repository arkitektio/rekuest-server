//! Matching signals to triggers and firing them, and the log of what became of each.
//!
//! The reaper's `triggers` sweep claims unprocessed signals under `SKIP LOCKED` (any number of
//! replicas may run it). For a claimed signal it looks at every enabled trigger of the signal's
//! organization that listens for its kind and structure, and writes one `facade_firing` row for
//! each: `FIRED` with the run, `REJECTED` with why the trigger did not apply, or `FAILED` with
//! why the run could not be created. A signal nobody listens for gets no row at all, which is
//! how "matched nothing" reads.
//!
//! A trigger applies when the signal's descriptors satisfy BOTH the trigger's own conditions
//! and the target port's own `requires` (its `compiled_jsonpath`): a trigger can never feed an
//! action an object the action has declared it cannot take. Both are tested with the same
//! `jsonb_path_match` the action search uses; a path that errors (a type mismatch against the
//! object) reads as NULL, which is not a match. Then its policies: it has not ended (`ends_at`,
//! `max_runs`), the chain of triggers is not too deep, and — with `debounce_seconds` — it has
//! not already fired for this object within the window.
//!
//! Replicas claim different signals, so two signals for one object, or two firings near
//! `max_runs`, could both pass a check made on a snapshot. Each trigger row is therefore locked
//! before its policies are checked and its counters written.
//!
//! A run caused by a task is that task's child (`parent` = the causing task), so it joins the
//! causing tree, and its provenance token names the tree's human at the root. Each trigger and
//! signal pair runs at most once: the run's reference is `trigger:<id>:<signal>`, unique per
//! caller. A replay ([`replay`]) is a firing of its own, asked for by hand.

use chrono::{DateTime, Utc};
use serde_json::{json, Map, Value};
use sqlx::types::Json;

use crate::backend::{self, AssignInput, AssignOrigin, BackendError, BackendResult};
use crate::caller_context::CallerContext;
use crate::context::Context;
use crate::signals;

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
    ends_at: Option<DateTime<Utc>>,
    max_runs: Option<i32>,
    run_count: i32,
    debounce_seconds: Option<i32>,
    user_id: i64,
    client_id: i64,
    organization_id: i64,
    /// The signal's descriptors satisfy the trigger's own conditions.
    conditions_met: bool,
    /// ... and what the target port requires of its object.
    requires_met: bool,
}

const COLUMNS: &str = "t.id, t.action_id, t.agent_id, t.interface, t.port, t.args, t.ends_at, t.max_runs, t.run_count,
       t.debounce_seconds, c.user_id, c.client_id, c.organization_id";

/// Every enabled trigger of the signal's organization that listens for its kind and structure,
/// with whether the signal satisfies it. Locked: see the module docs.
fn listening() -> String {
    format!(
        "SELECT {COLUMNS},
       (t.compiled_jsonpath IS NULL OR jsonb_path_match($4::jsonb, t.compiled_jsonpath::jsonpath, '{{}}'::jsonb, true) IS TRUE) AS conditions_met,
       (p.compiled_jsonpath IS NULL OR jsonb_path_match($4::jsonb, p.compiled_jsonpath::jsonpath, '{{}}'::jsonb, true) IS TRUE) AS requires_met
  FROM facade_trigger t
  JOIN facade_caller c ON c.id = t.caller_id
  LEFT JOIN facade_argport p ON p.action_id = t.action_id AND p.parent_id IS NULL AND p.key = t.port
 WHERE t.enabled AND t.kind = $1 AND t.identifier = $2 AND c.organization_id = $3
 ORDER BY t.id
   FOR NO KEY UPDATE OF t"
    )
}

impl Trigger {
    fn assign_input(&self, signal: &Signal, reference: String) -> AssignInput {
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
            reference: Some(reference),
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

    /// Whether it stopped by itself: its end passed, or it created its last allowed run.
    fn ended(&self) -> bool {
        self.ends_at.is_some_and(|end| end <= Utc::now())
            || self.max_runs.is_some_and(|max| self.run_count >= max)
    }

    /// Create the run for `signal` as the trigger's owner.
    async fn assign(
        &self,
        ctx: &Context,
        signal: &Signal,
        reference: String,
        depth: i16,
    ) -> Result<backend::Assigned, String> {
        let principal = CallerContext::load(
            &ctx.db,
            self.user_id,
            self.client_id,
            Some(self.organization_id),
            vec![],
        )
        .await
        .map_err(|e| e.to_string())?;
        backend::assign_with_status(
            ctx,
            &principal,
            &self.assign_input(signal, reference),
            AssignOrigin {
                signal: Some(signal.id),
                trigger: Some(self.id),
                trigger_depth: depth,
                ..AssignOrigin::default()
            },
        )
        .await
        .map_err(|e| e.to_string())
    }
}

/// What became of one trigger for one signal.
enum Outcome {
    Fired(i64),
    /// The trigger did not apply: nothing is wrong with it.
    Rejected(String),
    /// The trigger applied and its run could not be created: the trigger's bookkeeping says so.
    Failed(String),
}

impl Outcome {
    fn columns(&self) -> (&'static str, Option<&str>, Option<i64>) {
        match self {
            Outcome::Fired(task) => ("FIRED", None, Some(*task)),
            Outcome::Rejected(reason) => ("REJECTED", Some(reason), None),
            Outcome::Failed(reason) => ("FAILED", Some(reason), None),
        }
    }
}

/// Write the firing row, and the trigger's bookkeeping for it. True when the trigger row changed.
///
/// The row is written once per trigger and signal (`ON CONFLICT DO NOTHING`): a crash between a
/// run's creation and the signal's `processed_at` has the signal fired again, and the second
/// pass must not count the same run twice. A replay is always a row of its own.
async fn log(
    conn: &mut sqlx::PgConnection,
    signal: &Signal,
    trigger: &Trigger,
    outcome: &Outcome,
    replay: bool,
) -> Result<bool, sqlx::Error> {
    let (name, reason, task) = outcome.columns();
    let reason = match (replay, reason) {
        (true, None) => Some("Replayed by hand".to_owned()),
        (true, Some(reason)) => Some(format!("Replayed by hand: {reason}")),
        (false, reason) => reason.map(str::to_owned),
    };
    let written = sqlx::query(
        "INSERT INTO facade_firing (signal_id, trigger_id, outcome, reason, task_id, replay)
         VALUES ($1, $2, $3, $4, $5, $6)
         ON CONFLICT (signal_id, trigger_id) WHERE NOT replay DO NOTHING",
    )
    .bind(signal.id)
    .bind(trigger.id)
    .bind(name)
    .bind(&reason)
    .bind(task)
    .bind(replay)
    .execute(&mut *conn)
    .await?
    .rows_affected();
    if written == 0 {
        return Ok(false);
    }
    match outcome {
        Outcome::Rejected(_) => Ok(false),
        Outcome::Fired(_) => {
            sqlx::query(
                "UPDATE facade_trigger SET consecutive_failures = 0, last_error = NULL, last_error_at = NULL,
                        run_count = run_count + 1, last_fired_at = now(), updated_at = now() WHERE id = $1",
            )
            .bind(trigger.id)
            .execute(conn)
            .await?;
            Ok(true)
        }
        Outcome::Failed(error) => {
            tracing::warn!("Trigger {} did not fire: {error}", trigger.id);
            sqlx::query(
                "UPDATE facade_trigger SET consecutive_failures = consecutive_failures + 1, last_error = $2,
                        last_error_at = now(), updated_at = now() WHERE id = $1",
            )
            .bind(trigger.id)
            .bind(format!("Could not run for signal {}: {error}", signal.id))
            .execute(conn)
            .await?;
            Ok(true)
        }
    }
}

/// Whether `trigger` already fired for this object within its debounce window.
async fn debounced(
    conn: &mut sqlx::PgConnection,
    trigger: &Trigger,
    signal: &Signal,
) -> Result<bool, sqlx::Error> {
    let Some(window) = trigger.debounce_seconds else {
        return Ok(false);
    };
    sqlx::query_scalar(
        "SELECT EXISTS (
            SELECT 1 FROM facade_firing f JOIN facade_signal s ON s.id = f.signal_id
             WHERE f.trigger_id = $1 AND f.outcome = 'FIRED' AND s.object = $2 AND s.id <> $3
               AND f.created_at > now() - make_interval(secs => $4))",
    )
    .bind(trigger.id)
    .bind(&signal.object)
    .bind(signal.id)
    .bind(f64::from(window))
    .fetch_one(conn)
    .await
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
    let triggers: Vec<Trigger> = sqlx::query_as(&listening())
        .bind(&signal.kind)
        .bind(&signal.identifier)
        .bind(signal.organization_id)
        .bind(&signal.descriptors)
        .fetch_all(&mut *tx)
        .await?;
    let mut created = 0;
    let mut changed = Vec::new();
    for trigger in &triggers {
        let outcome = if !trigger.conditions_met {
            Outcome::Rejected("The signal does not satisfy the trigger's conditions".into())
        } else if !trigger.requires_met {
            Outcome::Rejected(format!(
                "The signal does not satisfy what the action's port {:?} requires",
                trigger.port
            ))
        } else if trigger.ended() {
            Outcome::Rejected("The trigger has ended".into())
        } else if depth > limit {
            // Not the signal's fault and not quite the trigger's: worth a line on the trigger.
            Outcome::Failed(format!(
                "{depth} triggers deep (limit {limit}) — a trigger loop?"
            ))
        } else if debounced(&mut tx, trigger, &signal).await? {
            Outcome::Rejected(format!(
                "Debounced: it already fired for this object within {} s",
                trigger.debounce_seconds.unwrap_or_default()
            ))
        } else {
            let reference = format!("trigger:{}:{}", trigger.id, signal.id);
            match trigger.assign(ctx, &signal, reference, depth).await {
                Ok(assigned) => {
                    created += usize::from(assigned.created);
                    Outcome::Fired(assigned.task)
                }
                // The target is broken, not the signal: record, move on.
                Err(error) => Outcome::Failed(error),
            }
        };
        if log(&mut tx, &signal, trigger, &outcome, false).await? {
            changed.push(trigger.id);
        }
    }
    sqlx::query("UPDATE facade_signal SET processed_at = now() WHERE id = $1")
        .bind(signal.id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    signals::signal_changed(ctx, signal.id, signal.organization_id, false).await;
    for trigger in changed {
        signals::rule_changed(ctx, signals::Rule::Trigger(trigger), signal.organization_id).await;
    }
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

/// Fire `trigger` on a stored `signal` by hand; the firing row's id (`trigger/fire`).
///
/// A replay asks "what would this rule do with that object", so it does not ask whether the
/// signal satisfies the trigger, nor its policies, nor whether the signal was processed: the
/// run is created. It is logged as a firing of its own (`replay`), counts as a run, and — being
/// asked for by a person — is a root run of the trigger's owner, not a child of the signal's cause.
pub async fn replay(
    ctx: &Context,
    organization: i64,
    trigger: i64,
    signal_id: i64,
) -> BackendResult<i64> {
    let mut tx = ctx.db.begin().await?;
    let signal: Option<Signal> = sqlx::query_as(
        "SELECT s.id, s.kind, s.identifier, s.object, s.descriptors, s.organization_id,
                NULL::bigint AS causing_task_id, NULL::smallint AS causing_depth
           FROM facade_signal s WHERE s.id = $1 AND s.organization_id = $2",
    )
    .bind(signal_id)
    .bind(organization)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(signal) = signal else {
        return Err(BackendError::Refused(format!(
            "No signal {signal_id} in this organization"
        )));
    };
    let found: Option<Trigger> = sqlx::query_as(&format!(
        "SELECT {COLUMNS}, true AS conditions_met, true AS requires_met
           FROM facade_trigger t JOIN facade_caller c ON c.id = t.caller_id
          WHERE t.id = $1 AND c.organization_id = $2 FOR NO KEY UPDATE OF t"
    ))
    .bind(trigger)
    .bind(organization)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(found) = found else {
        return Err(BackendError::Refused(format!(
            "No trigger {trigger} in this organization"
        )));
    };
    let reference = format!(
        "trigger:{}:{}:replay:{}",
        found.id,
        signal.id,
        uuid::Uuid::new_v4().simple()
    );
    let outcome = match found.assign(ctx, &signal, reference, 1).await {
        Ok(assigned) => Outcome::Fired(assigned.task),
        Err(error) => Outcome::Failed(error),
    };
    log(&mut tx, &signal, &found, &outcome, true).await?;
    let firing: i64 = sqlx::query_scalar(
        "SELECT id FROM facade_firing WHERE signal_id = $1 AND trigger_id = $2 AND replay ORDER BY id DESC LIMIT 1",
    )
    .bind(signal.id)
    .bind(found.id)
    .fetch_one(&mut *tx)
    .await?;
    tx.commit().await?;
    signals::signal_changed(ctx, signal.id, signal.organization_id, false).await;
    signals::rule_changed(
        ctx,
        signals::Rule::Trigger(found.id),
        signal.organization_id,
    )
    .await;
    Ok(firing)
}

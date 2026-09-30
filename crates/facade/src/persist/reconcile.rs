//! The sweeps: every deadline the server enforces, acted on from any backend
//! (`facade/persist/reconcile.py`).
//!
//! None of these is a timer. Each starts at a database column (`Agent.last_seen`,
//! `Task.dispatched_at`, `Task.interrupt_at`, `Task.not_before`), so a backend can be killed
//! mid-window without losing a pending decision, and any number of backends may sweep at once.
//! [`crate::reaper`] is what drives them.
//!
//! Every transition goes through the row-locked claim in [`transitions`], and candidate scans
//! lock with `SKIP LOCKED`, so a backend steps over a row another one is already handling
//! rather than queueing behind it. Cutoffs are taken from the application clock, the clock
//! `last_seen` and `dispatched_at` are written with (see [`crate::clock`]).

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};
use sqlx::types::Json;

use crate::consumers::agent_queue;
use crate::context::Context;
use crate::liveness::agent_is_live;
use crate::messages::{Assign, Journal, RecordedEffect, ToAgent};
use crate::persist::leases::{self, IN_FLIGHT};
use crate::persist::transitions::{
    self, insert_event, update_task_kind, Claim, Guard, NewEvent, TaskRow, TASK_ROW,
};
use crate::signals;

/// The pickup watchdog's budget: the original dispatch plus ONE redelivery. Failed handoffs
/// (redis down, a webhook that is not delivered) count, or a broken transport never fails.
pub const MAX_DISPATCH_ATTEMPTS: i16 = 2;

/// How often a workflow is sent again after its agent died before it ends LOST: an agent that
/// dies every time (a crashing driver, an input that kills it) must not resume it forever.
pub const MAX_RESUMES: i32 = 3;

/// A delayed task nobody has handed over yet (`WAITING_Q`), on alias `t`: waiting, not
/// undelivered. Every deadline that falls back to `created_at` for a never-dispatched row steps
/// over it; [`dispatch_due_tasks`] owns it until its first dispatch.
pub const WAITING: &str = "(t.not_before IS NOT NULL AND t.dispatch_attempts = 0)";

/// A virtual higher-order wrapper, on alias `t`: never dispatched, its fate is its child's.
const HIGHER_ORDER: &str = "EXISTS (SELECT 1 FROM facade_implementation hi
                                     WHERE hi.id = t.implementation_id
                                       AND hi.higher_order_for_id IS NOT NULL)";

/// A genuinely live agent on alias `a` (`live_agent_q`), `$n` being the stale cutoff. Written
/// null-safe, so its negation keeps an agent that never reported a heartbeat.
fn live_agent(cutoff_param: &str) -> String {
    format!("(a.connected AND a.last_seen IS NOT NULL AND a.last_seen > {cutoff_param})")
}

/// The oldest heartbeat that still counts as live, by the application clock.
fn stale_cutoff(ctx: &Context, now: DateTime<Utc>) -> DateTime<Utc> {
    now - chrono::Duration::from_std(ctx.settings.agent_stale_after)
        .unwrap_or(chrono::Duration::MAX)
}

fn ago(now: DateTime<Utc>, window: std::time::Duration) -> DateTime<Utc> {
    now - chrono::Duration::from_std(window).unwrap_or(chrono::Duration::MAX)
}

/// The provenance token of an Assign the server sends again (`mint_token_for_task`).
///
/// **The provenance seam.** The Python server mints a fresh token for every redelivered or
/// resumed Assign from the task's caller, and a strict provenance policy may refuse (then the
/// Assign "could not be rebuilt"). Provenance is not in this crate: until it is, redelivered
/// Assigns go out without a token, as they do for an implementation with `needs_token=false`.
/// The merge with the provenance port replaces this body; `Err` is the policy's refusal.
pub async fn redispatch_token(_ctx: &Context, _task: i64) -> Result<Option<String>, String> {
    Ok(None)
}

/// Fail the live probes of an agent that is gone (`probe_event_backend.fail_all_for_agent`).
///
/// **The probe seam.** Probes are hover-grade calls kept in the Python server's redis probe
/// store, not in the task tables; this crate does not own that store yet, so there is nothing
/// here to fail. Called at every point the Python server fails them (a revoked lease, a
/// released one), so wiring the store in is this one body. Returns the number failed.
pub async fn fail_probes_for_agent(_ctx: &Context, agent: i64) -> usize {
    tracing::debug!(agent, "no probe store in this server: no probes to fail");
    0
}

/// What the Assign of a task sent again is built from.
#[derive(sqlx::FromRow)]
struct AssignSource {
    args: Option<Json<Map<String, Value>>>,
    reference: String,
    capture: bool,
    step: bool,
    resolution_id: Option<i64>,
    parent_id: Option<i64>,
    root_id: Option<i64>,
    implementation_id: Option<i64>,
    interface: Option<String>,
    execution: Option<String>,
    action_hash: String,
    user_sub: Option<String>,
    org_slug: Option<String>,
}

/// Rebuild the Assign of a task sent again, or `None` (`_build_redispatch_assign_sync`): when it
/// lacks the identity to send it as (no implementation, no caller) or provenance refuses. A
/// workflow's Assign carries its journal (`resume`), which its resumed run replays.
pub async fn build_redispatch_assign(
    ctx: &Context,
    task: i64,
) -> Result<Option<Assign>, sqlx::Error> {
    let source: Option<AssignSource> = sqlx::query_as(
        "SELECT t.args, t.reference, t.capture, t.step, t.resolution_id, t.parent_id, t.root_id,
                t.implementation_id, i.interface, i.execution, a.hash AS action_hash,
                u.sub AS user_sub, o.slug AS org_slug
           FROM facade_task t
           JOIN facade_action a ON a.id = t.action_id
           LEFT JOIN facade_implementation i ON i.id = t.implementation_id
           LEFT JOIN facade_caller c ON c.id = t.caller_id
           LEFT JOIN authentikate_user u ON u.id = c.user_id
           LEFT JOIN authentikate_organization o ON o.id = c.organization_id
          WHERE t.id = $1",
    )
    .bind(task)
    .fetch_optional(&ctx.db)
    .await?;
    let Some(source) = source else {
        return Ok(None);
    };
    let (Some(implementation), Some(interface), Some(user), Some(org)) = (
        source.implementation_id,
        source.interface,
        source.user_sub,
        source.org_slug,
    ) else {
        return Ok(None);
    };
    let token = match redispatch_token(ctx, task).await {
        Ok(token) => token,
        Err(refusal) => {
            tracing::warn!(task, "provenance refused a redispatch token: {refusal}");
            return Ok(None);
        }
    };
    let resume = if source.execution.as_deref() == Some("WORKFLOW") {
        Some(journal(ctx, task).await?)
    } else {
        None
    };
    Ok(Some(Assign {
        id: None,
        interface,
        task: task.to_string(),
        root: source.root_id.map(|id| id.to_string()),
        parent: source.parent_id.map(|id| id.to_string()),
        resolution: source.resolution_id.map(|id| id.to_string()),
        step: source.step.then_some(true),
        probe: false,
        capture: Some(source.capture),
        reference: Some(source.reference),
        args: source.args.map(|args| args.0).unwrap_or_default(),
        message: None,
        user,
        org,
        action: source.action_hash,
        implementation: implementation.to_string(),
        token,
        resume,
    }))
}

/// What a workflow recorded (`_journal_sync`): its effects by key, in the order recorded, and
/// the last step it took, counting the steps its calls were made at.
pub async fn journal(ctx: &Context, task: i64) -> Result<Journal, sqlx::Error> {
    let last_event_step: i64 = sqlx::query_scalar(
        "SELECT COALESCE(MAX(step), 0) FROM facade_taskevent WHERE task_id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await?;
    let last_call_step: i64 = sqlx::query_scalar(
        "SELECT COALESCE(MAX(parent_step), 0) FROM facade_task WHERE parent_id = $1",
    )
    .bind(task)
    .fetch_one(&ctx.db)
    .await?;
    let effects: Vec<(String, Option<String>, Option<Json<Value>>)> = sqlx::query_as(
        "SELECT key, effect, value FROM facade_taskevent
          WHERE task_id = $1 AND kind = 'EFFECT' AND key IS NOT NULL ORDER BY id",
    )
    .bind(task)
    .fetch_all(&ctx.db)
    .await?;
    Ok(Journal {
        last_step: last_event_step.max(last_call_step).max(0) as u64,
        effects: effects
            .into_iter()
            .map(|(key, effect, value)| RecordedEffect {
                key,
                effect: effect.unwrap_or_default(),
                value: value.map(|v| v.0).unwrap_or(Value::Null),
            })
            .collect(),
    })
}

/// Hand a frame to an agent's transport. A websocket agent's goes into its redis queue (which
/// holds it while the agent is away). Webhook delivery is not in this server yet: a webhook
/// agent's frame counts as a failed handoff, which the pickup watchdog retries and then ends.
async fn deliver(ctx: &Context, agent: i64, frame: &ToAgent) -> bool {
    let kind: Result<Option<String>, _> =
        sqlx::query_scalar("SELECT kind FROM facade_agent WHERE id = $1")
            .bind(agent)
            .fetch_optional(&ctx.db)
            .await;
    match kind {
        Ok(Some(kind)) if kind == "WEBSOCKET" => {}
        Ok(Some(kind)) => {
            tracing::error!(
                agent,
                kind,
                "no transport for this agent kind in this server"
            );
            return false;
        }
        Ok(None) => return false,
        Err(e) => {
            tracing::error!(agent, "looking up the agent's transport failed: {e}");
            return false;
        }
    }
    let body = match serde_json::to_string(frame) {
        Ok(body) => body,
        Err(e) => {
            tracing::error!(agent, "serializing a frame failed: {e}");
            return false;
        }
    };
    let mut redis = ctx.redis.clone();
    match agent_queue::push(&mut redis, &ctx.settings, agent, &body, false).await {
        Ok(()) => true,
        Err(e) => {
            tracing::error!(agent, "queueing a frame failed: {e}");
            false
        }
    }
}

/// Hand an Assign to the agent's transport; on failure, record that it never left (`_dispatch`).
///
/// `dispatched_at` means "successfully handed over at". A failed handoff resets it to NULL so
/// the pickup watchdog retries it, and NULL proves the agent cannot have the task.
async fn dispatch(ctx: &Context, task: i64, agent: i64, assign: Assign) -> bool {
    if deliver(ctx, agent, &ToAgent::Assign(Box::new(assign))).await {
        return true;
    }
    tracing::error!(task, agent, "dispatching the task failed");
    if let Err(e) =
        sqlx::query("UPDATE facade_task SET dispatched_at = NULL WHERE id = $1 AND is_done = false")
            .bind(task)
            .execute(&ctx.db)
            .await
    {
        tracing::error!(task, "recording the failed dispatch failed: {e}");
    }
    false
}

/// Fail an agent's in-flight work after a confirmed loss (`reconcile_orphaned_executor_work`).
/// Idempotent. A no-op while the agent is live: a reconnect in the meantime means the work is
/// being reclaimed, not orphaned. Asks [`agent_is_live`], never `connected` alone, so a
/// stuck-connected agent with an expired lease cannot make it a silent no-op.
pub async fn reconcile_orphaned_executor_work(
    ctx: &Context,
    agent: i64,
) -> Result<(), sqlx::Error> {
    let (connected, last_seen): (bool, Option<DateTime<Utc>>) =
        sqlx::query_as("SELECT connected, last_seen FROM facade_agent WHERE id = $1")
            .bind(agent)
            .fetch_one(&ctx.db)
            .await?;
    if agent_is_live(&ctx.settings, connected, last_seen, Utc::now()) {
        return Ok(());
    }
    let in_flight: Vec<i64> = sqlx::query_scalar(&format!(
        "SELECT t.id FROM facade_task t WHERE t.agent_id = $1 AND {IN_FLIGHT} ORDER BY t.id"
    ))
    .bind(agent)
    .fetch_all(&ctx.db)
    .await?;
    fail_and_cascade_inflight(ctx, &in_flight).await
}

/// End orphaned in-flight work LOST (`_fail_and_cascade_inflight`): its agent is gone, and with
/// it any way of knowing how the task ended. A workflow is sent again to resume instead.
///
/// The server re-runs nothing else on its own: whoever called decides, and the LOST event
/// carries what is known for that. Work the agent never picked up is left alone (not orphaned,
/// merely undelivered). Each task is claimed, so concurrent sweeps write one LOST per task.
pub async fn fail_and_cascade_inflight(ctx: &Context, tasks: &[i64]) -> Result<(), sqlx::Error> {
    for &task in tasks {
        let row: Option<(String, Option<DateTime<Utc>>, bool)> = sqlx::query_as(
            "SELECT t.latest_event_kind, t.picked_up_at,
                    COALESCE(i.execution = 'WORKFLOW', false)
               FROM facade_task t LEFT JOIN facade_implementation i ON i.id = t.implementation_id
              WHERE t.id = $1",
        )
        .bind(task)
        .fetch_optional(&ctx.db)
        .await?;
        let Some((kind, picked_up_at, workflow)) = row else {
            continue;
        };
        if kind == "QUEUED" && picked_up_at.is_none() {
            continue; // undelivered, not orphaned
        }
        if workflow && resume_workflow(ctx, task).await? {
            continue;
        }
        transitions::finalize_lost(
            ctx,
            task,
            "Its agent died while it ran; how it ended is unknown.",
            true,
            None,
            false,
        )
        .await?;
    }
    Ok(())
}

/// Send a workflow whose agent died again, with its journal, to be resumed (`_aresume_workflow`).
///
/// Resumed only by the code it ran: replaying the journal onto changed code could take another
/// path, so it ends LOST instead, as it does after [`MAX_RESUMES`]. Returns whether it was
/// handled (resumed, or lost for one of those reasons); `false` leaves it to the plain LOST.
async fn resume_workflow(ctx: &Context, task: i64) -> Result<bool, sqlx::Error> {
    let (pinned, current, resumes, agent): (Option<String>, Option<String>, i32, i64) =
        sqlx::query_as(
            "SELECT t.code_hash, i.code_hash, t.resumes, t.agent_id
               FROM facade_task t LEFT JOIN facade_implementation i ON i.id = t.implementation_id
              WHERE t.id = $1",
        )
        .bind(task)
        .fetch_one(&ctx.db)
        .await?;
    if resumes >= MAX_RESUMES {
        let reason = format!("Resumed {resumes} times, and its agent died each time.");
        transitions::finalize_lost(ctx, task, &reason, true, None, false).await?;
        return Ok(true);
    }
    if pinned != current {
        transitions::finalize_lost(
            ctx,
            task,
            "Its agent died, and the workflow's code changed since this run started, so it cannot be resumed.",
            true,
            None,
            false,
        )
        .await?;
        return Ok(true);
    }
    let Some(assign) = build_redispatch_assign(ctx, task).await? else {
        return Ok(false);
    };
    let won = transitions::claim(
        ctx,
        task,
        Claim {
            skip_if_kind: Some("QUEUED"),
            extra: Some(
                "picked_up_at = NULL, dispatched_at = $2, dispatch_attempts = 1, resumes = resumes + 1",
            ),
            event: Some(NewEvent {
                message: Some(
                    "Its agent died: the workflow is sent again, to resume from what it recorded."
                        .into(),
                ),
                ..NewEvent::default()
            }),
            ..Claim::to("QUEUED")
        },
    )
    .await?;
    if won {
        dispatch(ctx, task, agent, assign).await;
    }
    Ok(true)
}

/// Fail the work of websocket agents that disconnected and stayed gone past the grace
/// (`reconcile_disconnected_agents`). This IS the grace window: a clean disconnect records
/// `connected=false` and `last_seen`, and whichever backend first sees that age past the grace
/// fails the work. Webhook agents have no socket and are not swept here. Returns the count.
pub async fn reconcile_disconnected_agents(ctx: &Context) -> Result<usize, sqlx::Error> {
    let cutoff = ago(Utc::now(), ctx.settings.grace);
    let agents: Vec<i64> = sqlx::query_scalar(&format!(
        "SELECT DISTINCT t.agent_id FROM facade_task t JOIN facade_agent a ON a.id = t.agent_id
          WHERE {IN_FLIGHT} AND a.kind = 'WEBSOCKET' AND a.connected = false
            AND (a.last_seen < $1 OR a.last_seen IS NULL)"
    ))
    .bind(cutoff)
    .fetch_all(&ctx.db)
    .await?;
    for &agent in &agents {
        reconcile_orphaned_executor_work(ctx, agent).await?;
    }
    Ok(agents.len())
}

/// Heal websocket agents whose `connected` is stuck true past the stale window
/// (`reconcile_stale_agents`): a killed worker never runs its disconnect. Revoke the lease, fail
/// the agent's probes, and reconcile its orphaned work. Only the backend whose revoke wins goes
/// on, so N sweeping backends heal each agent once. Returns the number healed.
pub async fn reconcile_stale_agents(ctx: &Context) -> Result<usize, sqlx::Error> {
    let cutoff = stale_cutoff(ctx, Utc::now());
    let stale: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM facade_agent
          WHERE kind = 'WEBSOCKET' AND connected AND (last_seen < $1 OR last_seen IS NULL)",
    )
    .bind(cutoff)
    .fetch_all(&ctx.db)
    .await?;
    let mut healed = 0;
    for agent in stale {
        if !leases::revoke_lease(ctx, agent).await? {
            continue; // another backend's sweep (or a reconnect) got there first
        }
        fail_probes_for_agent(ctx, agent).await; // probes fail fast, no grace
        reconcile_orphaned_executor_work(ctx, agent).await?;
        healed += 1;
    }
    Ok(healed)
}

/// Act on control deadlines (`Task.interrupt_at`) that have passed (`escalate_due_controls`):
///
/// * an unconfirmed **interrupt** is finalized INTERRUPTED: the server stops waiting;
/// * an unconfirmed **cancel** is escalated to an interrupt;
/// * any other instruct (a resume after the cancel) superseded the deadline: it is dropped.
///
/// The claim is a compare-and-set on the deadline itself: whichever backend clears
/// `interrupt_at` owns the escalation, so N sweeping backends interrupt once. Returns the count.
pub async fn escalate_due_controls(ctx: &Context, limit: i64) -> Result<usize, sqlx::Error> {
    let due: Vec<(i64, DateTime<Utc>, String)> = sqlx::query_as(
        "SELECT id, interrupt_at, latest_instruct_kind FROM facade_task
          WHERE is_done = false AND interrupt_at < $1 ORDER BY interrupt_at LIMIT $2",
    )
    .bind(Utc::now())
    .bind(limit)
    .fetch_all(&ctx.db)
    .await?;
    let mut handled = 0;
    for (task, deadline, instruct) in due {
        if instruct == "INTERRUPT" {
            let still_due = move |row: &TaskRow| row.interrupt_at == Some(deadline);
            if transitions::finalize_terminal(
                ctx,
                task,
                "INTERRUPTED",
                "Interrupt was never confirmed by the agent — finalized by the server.",
                None,
                Guard {
                    only_if: Some(&still_due),
                    extra: Some("interrupt_at = NULL"),
                    skip_locked: true,
                },
            )
            .await?
            {
                handled += 1;
            }
            continue;
        }
        let cleared = sqlx::query(
            "UPDATE facade_task SET interrupt_at = NULL
              WHERE id = $1 AND is_done = false AND interrupt_at = $2",
        )
        .bind(task)
        .bind(deadline)
        .execute(&ctx.db)
        .await?
        .rows_affected();
        if cleared == 0 {
            continue; // another backend took it, or the task went terminal meanwhile
        }
        handled += 1;
        if instruct == "CANCEL" {
            if let Err(e) = escalate_to_interrupt(ctx, task).await {
                tracing::error!(task, "escalating to an interrupt failed: {e}");
            }
        }
    }
    Ok(handled)
}

/// A cancel's deadline passed unconfirmed: interrupt the task instead
/// (`_escalate_to_interrupt`, through `controll_backend.interrupt`). A no-op once it is terminal.
///
/// The interrupt request of the control backend, which this crate does not have yet: for the
/// task and every still-open descendant, under its row lock, the INTERRUPT instruct, a re-armed
/// control deadline (so an interrupt nobody confirms is finalized in turn), the INTERRUPTING
/// event and the `TaskInstruct` audit row; then, after the commit, the `Interrupt` frame. A
/// delayed task never handed over is settled INTERRUPTED instead: nobody has it to wind down.
pub async fn escalate_to_interrupt(ctx: &Context, task: i64) -> Result<(), sqlx::Error> {
    let mut targets = vec![task];
    targets.extend(
        sqlx::query_scalar::<_, i64>(
            "SELECT id FROM facade_task WHERE root_id = $1 AND is_done = false ORDER BY id",
        )
        .bind(task)
        .fetch_all(&ctx.db)
        .await?,
    );
    let now = Utc::now();
    let interrupt_at = (!ctx.settings.control_deadline.is_zero()).then(|| {
        now + chrono::Duration::from_std(ctx.settings.control_deadline).unwrap_or_default()
    });

    for target in targets {
        let mut tx = ctx.db.begin().await?;
        let row: Option<TaskRow> = sqlx::query_as(&format!(
            "SELECT {TASK_ROW} FROM facade_task WHERE id = $1 FOR UPDATE"
        ))
        .bind(target)
        .fetch_optional(&mut *tx)
        .await?;
        let Some(row) = row.filter(|row| !row.is_done) else {
            continue;
        };
        let delayed = row.is_waiting() && row.picked_up_at.is_none();
        let (kind, done, message) = if delayed {
            (
                "INTERRUPTED",
                true,
                Some("Settled before it was due — never dispatched.".to_owned()),
            )
        } else {
            ("INTERRUPTING", false, None)
        };
        sqlx::query(
            "UPDATE facade_task SET latest_instruct_kind = 'INTERRUPT', interrupt_at = $2
              WHERE id = $1",
        )
        .bind(target)
        .bind(if delayed { None } else { interrupt_at })
        .execute(&mut *tx)
        .await?;
        update_task_kind(&mut tx, target, kind, done).await?;
        let event = insert_event(
            &mut *tx,
            target,
            kind,
            &NewEvent {
                message,
                ..NewEvent::default()
            },
        )
        .await?;
        sqlx::query(
            "INSERT INTO facade_taskinstruct (task_id, kind, created_at) VALUES ($1, 'INTERRUPT', now())",
        )
        .bind(target)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        signals::task_saved(ctx, target, false).await;
        signals::task_event_created(ctx, event).await;
        if !delayed
            && !deliver(
                ctx,
                row.agent_id,
                &ToAgent::Interrupt {
                    task: target.to_string(),
                },
            )
            .await
        {
            // The INTERRUPTING row is the durable record; an unreachable executor is what the
            // control deadline is for.
            tracing::error!(
                task = target,
                agent = row.agent_id,
                "could not deliver the interrupt"
            );
        }
    }
    Ok(())
}

/// What the pickup watchdog decided for one task (`_decide_unpicked_sync`).
enum Unpicked {
    /// No longer a candidate, or another backend holds it.
    Skip,
    /// Finalized here with this kind.
    Finalized(&'static str),
    /// Stamped for redelivery; the Assign is pushed after the commit.
    Redeliver(Box<Assign>, i64),
}

/// End a locked task in the transaction that holds its lock: the claim's writes, without the
/// claim (it would lock the row again).
async fn finalize_locked(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    task: i64,
    kind: &str,
    message: &str,
    value: Option<Value>,
) -> Result<i64, sqlx::Error> {
    update_task_kind(tx, task, kind, true).await?;
    insert_event(
        &mut **tx,
        task,
        kind,
        &NewEvent {
            message: Some(message.to_owned()),
            value,
            ..NewEvent::default()
        },
    )
    .await
}

/// Decide, under the row lock, what happens to one task nobody picked up.
async fn decide_unpicked(
    ctx: &Context,
    task: i64,
    cutoff: DateTime<Utc>,
) -> Result<Unpicked, sqlx::Error> {
    let mut tx = ctx.db.begin().await?;
    let row: Option<TaskRow> = sqlx::query_as(&format!(
        "SELECT {TASK_ROW} FROM facade_task WHERE id = $1 FOR UPDATE SKIP LOCKED"
    ))
    .bind(task)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(row) = row else {
        return Ok(Unpicked::Skip);
    };
    if row.is_done || row.picked_up_at.is_some() || row.latest_event_kind != "QUEUED" {
        return Ok(Unpicked::Skip);
    }
    if row.is_waiting() {
        return Ok(Unpicked::Skip); // not due yet: dispatch_due_tasks owns it
    }
    if row.pickup_clock() >= cutoff {
        return Ok(Unpicked::Skip); // re-dispatched, or its clock restarted, since the scan
    }

    // A control op already targeted it: the agent never had the task, so there is nothing to
    // wind down. Honour the request instead of redelivering the Assign.
    let finalized = match row.latest_instruct_kind.as_str() {
        "CANCEL" => Some((
            "CANCELLED",
            "Cancelled before any agent picked the task up.",
        )),
        "INTERRUPT" => Some((
            "INTERRUPTED",
            "Interrupted before any agent picked the task up.",
        )),
        _ => None,
    };
    let lost = |reason: &'static str| ("LOST", reason);
    let (outcome, assign) = if let Some(finalized) = finalized {
        (Some(finalized), None)
    } else if row.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS {
        (
            Some(lost(
                "Never picked up: the agent did not report on this task after it was redelivered.",
            )),
            None,
        )
    } else {
        // Never picked up means never STARTED, which an agent reports before anything else:
        // sending it again is safe, whatever the implementation's effects.
        match build_redispatch_assign(ctx, task).await? {
            None => (
                Some(lost(
                    "Never picked up, and the Assign could not be rebuilt for redelivery.",
                )),
                None,
            ),
            Some(assign) => (None, Some(assign)),
        }
    };

    if let Some((kind, message)) = outcome {
        // Nothing ran, so whoever decides may safely send it again.
        let value = if kind == "LOST" {
            Some(transitions::lost_details(&ctx.db, task, false, message).await?)
        } else {
            None
        };
        let event = finalize_locked(&mut tx, task, kind, message, value).await?;
        tx.commit().await?;
        signals::task_saved(ctx, task, false).await;
        signals::task_event_created(ctx, event).await;
        return Ok(Unpicked::Finalized(kind));
    }

    let assign = assign.expect("an Assign to redeliver");
    sqlx::query(
        "UPDATE facade_task SET dispatched_at = $2, dispatch_attempts = dispatch_attempts + 1,
                revision = revision + 1, updated_at = now()
          WHERE id = $1",
    )
    .bind(task)
    .bind(Utc::now())
    .execute(&mut *tx)
    .await?;
    let event = insert_event(
        &mut *tx,
        task,
        "QUEUED",
        &NewEvent {
            message: Some(
                "No report from the agent within the pickup deadline — Assign redelivered.".into(),
            ),
            ..NewEvent::default()
        },
    )
    .await?;
    tx.commit().await?;
    signals::task_saved(ctx, task, false).await;
    signals::task_event_created(ctx, event).await;
    Ok(Unpicked::Redeliver(Box::new(assign), row.agent_id))
}

/// The pickup watchdog: no task waits forever for an agent that looks alive
/// (`reconcile_unpicked_tasks`).
///
/// Every other safety net keys on the agent looking dead. This one covers the rest: the Assign
/// was lost, or the agent dropped it without a word. A dispatched task whose live agent (or
/// webhook endpoint) reported nothing within the pickup deadline is redelivered once; silent
/// again, it ends LOST. Tasks of agents that are not live are left to the disconnect path.
/// Keyed on `picked_up_at` (any report), never on the kind alone. Returns the number acted on.
pub async fn reconcile_unpicked_tasks(ctx: &Context, limit: i64) -> Result<usize, sqlx::Error> {
    if ctx.settings.pickup_deadline.is_zero() {
        return Ok(0);
    }
    let now = Utc::now();
    let cutoff = ago(now, ctx.settings.pickup_deadline);
    let candidates: Vec<i64> = sqlx::query_scalar(&format!(
        "SELECT t.id FROM facade_task t JOIN facade_agent a ON a.id = t.agent_id
          WHERE t.is_done = false AND t.picked_up_at IS NULL AND t.latest_event_kind = 'QUEUED'
            AND (t.dispatched_at < $1 OR (t.dispatched_at IS NULL AND t.created_at < $1))
            AND (a.kind = 'WEBHOOK' OR {live})
            AND NOT {HIGHER_ORDER} AND NOT {WAITING}
          ORDER BY t.created_at LIMIT $3",
        live = live_agent("$2"),
    ))
    .bind(cutoff)
    .bind(stale_cutoff(ctx, now))
    .bind(limit)
    .fetch_all(&ctx.db)
    .await?;
    let mut acted = 0;
    for task in candidates {
        match decide_unpicked(ctx, task, cutoff).await? {
            Unpicked::Skip => continue,
            Unpicked::Redeliver(assign, agent) => {
                dispatch(ctx, task, agent, *assign).await;
            }
            Unpicked::Finalized(kind) => {
                transitions::unfold_to_higher_order(ctx, task, kind, None, None, None).await?;
            }
        }
        acted += 1;
    }
    Ok(acted)
}

/// Claim one due delayed task for its first dispatch, under the row lock (`_claim_due_sync`):
/// `None` to skip, `Some(None)` when it was finalized CRITICAL, `Some(Some(..))` to dispatch
/// after the commit.
async fn claim_due(ctx: &Context, task: i64) -> Result<Option<Option<(Assign, i64)>>, sqlx::Error> {
    let mut tx = ctx.db.begin().await?;
    let row: Option<TaskRow> = sqlx::query_as(&format!(
        "SELECT {TASK_ROW} FROM facade_task WHERE id = $1 FOR UPDATE SKIP LOCKED"
    ))
    .bind(task)
    .fetch_optional(&mut *tx)
    .await?;
    let now = Utc::now();
    let Some(row) = row.filter(|row| {
        !row.is_done && row.is_waiting() && row.not_before.is_some_and(|due| due <= now)
    }) else {
        return Ok(None);
    };
    let Some(assign) = build_redispatch_assign(ctx, task).await? else {
        let event = finalize_locked(
            &mut tx,
            task,
            "CRITICAL",
            "Due, but the Assign could not be built (no caller identity, or the provenance policy refused).",
            None,
        )
        .await?;
        tx.commit().await?;
        signals::task_saved(ctx, task, false).await;
        signals::task_event_created(ctx, event).await;
        return Ok(Some(None));
    };
    // From here on it is an ordinary dispatched task: the pickup watchdog's clock starts now.
    sqlx::query(
        "UPDATE facade_task SET dispatched_at = $2, dispatch_attempts = 1,
                revision = revision + 1, updated_at = now()
          WHERE id = $1",
    )
    .bind(task)
    .bind(now)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    signals::task_saved(ctx, task, false).await;
    Ok(Some(Some((assign, row.agent_id))))
}

/// Hand over delayed tasks whose `not_before` has passed (`dispatch_due_tasks`); a task fires
/// at most one sweep interval late. Cancelled-before-due tasks are already terminal. Returns the
/// number acted on.
pub async fn dispatch_due_tasks(ctx: &Context, limit: i64) -> Result<usize, sqlx::Error> {
    let due: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM facade_task
          WHERE is_done = false AND dispatch_attempts = 0 AND not_before <= $1
          ORDER BY not_before LIMIT $2",
    )
    .bind(Utc::now())
    .bind(limit)
    .fetch_all(&ctx.db)
    .await?;
    let mut acted = 0;
    for task in due {
        match claim_due(ctx, task).await? {
            None => continue,
            Some(None) => {}
            Some(Some((assign, agent))) => {
                dispatch(ctx, task, agent, assign).await;
            }
        }
        acted += 1;
    }
    Ok(acted)
}

/// End work that waited past the disconnected expiry on an agent that never came back
/// (`expire_disconnected_tasks`). Undelivered `QUEUED` tasks of an agent that is not live stay
/// open that long (it may return and pick them up), then end LOST: nothing ran. Zero never
/// expires. Returns the count.
pub async fn expire_disconnected_tasks(ctx: &Context, limit: i64) -> Result<usize, sqlx::Error> {
    if ctx.settings.disconnected_expiry.is_zero() {
        return Ok(0);
    }
    let now = Utc::now();
    let cutoff = ago(now, ctx.settings.disconnected_expiry);
    let undelivered: Vec<i64> = sqlx::query_scalar(&format!(
        "SELECT t.id FROM facade_task t JOIN facade_agent a ON a.id = t.agent_id
          WHERE t.is_done = false AND t.picked_up_at IS NULL AND t.latest_event_kind = 'QUEUED'
            AND a.kind = 'WEBSOCKET'
            AND (t.dispatched_at < $1 OR (t.dispatched_at IS NULL AND t.created_at < $1))
            AND NOT {live} AND NOT {HIGHER_ORDER} AND NOT {WAITING}
          ORDER BY t.id LIMIT $3",
        live = live_agent("$2"),
    ))
    .bind(cutoff)
    .bind(stale_cutoff(ctx, now))
    .bind(limit)
    .fetch_all(&ctx.db)
    .await?;
    let still_undelivered = move |row: &TaskRow| {
        row.picked_up_at.is_none()
            && row.latest_event_kind == "QUEUED"
            && !row.is_waiting()
            && row.pickup_clock() < cutoff
    };
    let mut expired = 0;
    for task in undelivered {
        if transitions::finalize_lost(
            ctx,
            task,
            "The agent never came back to pick this task up.",
            false,
            Some(&still_undelivered),
            true,
        )
        .await?
        {
            expired += 1;
        }
    }
    Ok(expired)
}

//! Schedules: every enabled one that has not ended always has exactly one waiting run.
//!
//! The reaper's `schedules` sweep gives each schedule that is due one its next run, created
//! through the ordinary assign path as a delayed task. It is safe from any number of replicas:
//! each schedule is claimed under a `SKIP LOCKED` row lock, and the run's
//! `schedule:<id>:<slot>` reference is unique per caller, so a lost race finds the winner's row
//! instead of creating a second run.
//!
//! When a schedule is due a run is its `overlap` policy: `SKIP` (the default) waits for the
//! previous run to finish, so runs never overlap; `ALLOW` only waits for it to be handed over,
//! so the next slot is planned while the previous run still executes. "Waiting" means not handed
//! over yet (`dispatch_attempts = 0`) throughout.
//!
//! Which slot comes next is its `catch_up` policy: without it, the first slot after now (slots
//! that passed meanwhile are skipped); with it, the first slot after the one planned last
//! (`last_slot_at`), so slots missed while takt was down or a run was open are run late, one
//! after another. A gap of more than [`MAX_CATCH_UP`] slots is not caught up: the schedule
//! resumes from now.
//!
//! A schedule ends by itself at `ends_at` or once it created `max_runs` runs: nothing is
//! planned from then on, and nothing is written to say so (the columns say it).
//!
//! The rekuest server owns the schedule rows (its GraphQL creates and changes them); what plans,
//! moves and cancels their runs is here, asked through the internal API.

use chrono::{DateTime, Duration, Utc};
use serde_json::{Map, Value};
use sqlx::types::Json;

use crate::backend::{self, AssignInput, AssignOrigin, BackendError, BackendResult, Control};
use crate::caller_context::CallerContext;
use crate::context::Context;
use crate::signals;
use crate::timing::Timing;

/// Creating a run failed (action gone, args no longer fit, no agent): retry no sooner than this.
const REFILL_RETRY_SECONDS: i64 = 60;

/// Slots whose reference already exists (a cancelled or triggered run of that very slot) are
/// skipped; a schedule that keeps colliding past this many is broken, not unlucky.
const MAX_SLOT_SKIPS: usize = 5;

/// How many missed slots a `catch_up` schedule runs late. Past that the gap is not a hiccup,
/// and running hundreds of stale slots in a row helps nobody: it resumes from now.
const MAX_CATCH_UP: usize = 100;

#[derive(sqlx::FromRow)]
struct Schedule {
    id: i64,
    action_id: i64,
    agent_id: Option<i64>,
    interface: Option<String>,
    args: Json<Value>,
    interval_seconds: Option<i32>,
    cron: Option<String>,
    timezone: String,
    created_at: DateTime<Utc>,
    refill_after: Option<DateTime<Utc>>,
    consecutive_failures: i32,
    ends_at: Option<DateTime<Utc>>,
    max_runs: Option<i32>,
    run_count: i32,
    overlap: String,
    catch_up: bool,
    last_slot_at: Option<DateTime<Utc>>,
    user_id: i64,
    client_id: i64,
    organization_id: i64,
}

const COLUMNS: &str = "s.id, s.action_id, s.agent_id, s.interface, s.args, s.interval_seconds, s.cron, s.timezone,
                       s.created_at, s.refill_after, s.consecutive_failures, s.ends_at, s.max_runs, s.run_count,
                       s.overlap, s.catch_up, s.last_slot_at, c.user_id, c.client_id, c.organization_id";

/// A run that keeps a schedule from getting its next one: any open run, or with `ALLOW`
/// overlap only one that is still waiting. `s` is the schedule, `t` the task.
const BLOCKING_RUN: &str =
    "t.schedule_id = s.id AND NOT t.is_done AND (s.overlap <> 'ALLOW' OR t.dispatch_attempts = 0)";

impl Schedule {
    fn timing(&self) -> Timing {
        Timing {
            interval_seconds: self.interval_seconds.map(i64::from),
            cron: self.cron.clone(),
            timezone: self.timezone.clone(),
        }
    }

    /// The identity its runs are assigned as: the schedule's caller, with no roles.
    async fn principal(&self, ctx: &Context) -> Result<CallerContext, sqlx::Error> {
        CallerContext::load(
            &ctx.db,
            self.user_id,
            self.client_id,
            Some(self.organization_id),
            vec![],
        )
        .await
    }

    fn allows_overlap(&self) -> bool {
        self.overlap == "ALLOW"
    }

    /// Whether it stopped by itself: its end passed, or it created its last allowed run.
    fn ended(&self, now: DateTime<Utc>) -> bool {
        self.ends_at.is_some_and(|end| end <= now)
            || self.max_runs.is_some_and(|max| self.run_count >= max)
    }

    /// Where the search for the next slot starts: now, or — catching up — the slot planned last,
    /// unless that is more than [`MAX_CATCH_UP`] slots ago.
    fn search_from(&self, now: DateTime<Utc>) -> Result<DateTime<Utc>, String> {
        let Some(last) = self
            .last_slot_at
            .filter(|last| self.catch_up && *last < now)
        else {
            return Ok(now);
        };
        let timing = self.timing();
        let mut slot = last;
        for _ in 0..MAX_CATCH_UP {
            slot = timing.next_slot(self.created_at, slot)?;
            if slot > now {
                return Ok(last);
            }
        }
        tracing::warn!(
            "Schedule {}: more than {MAX_CATCH_UP} slots were missed since {last}; resuming from now",
            self.id
        );
        Ok(now)
    }

    fn assign_input(&self, reference: String, not_before: Option<DateTime<Utc>>) -> AssignInput {
        let args = match &self.args.0 {
            Value::Object(args) => args.clone(),
            _ => Map::new(),
        };
        let mut input = AssignInput {
            args,
            reference: Some(reference),
            not_before,
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

/// A slot as a run's reference names it: Python's `datetime.isoformat()` of the UTC instant,
/// which is what the runs planned before schedules moved here are named with.
fn slot_reference(schedule: i64, slot: DateTime<Utc>) -> String {
    let stamp = if slot.timestamp_subsec_micros() == 0 {
        slot.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        slot.format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string()
    };
    format!("schedule:{schedule}:{stamp}")
}

async fn has_blocking_run(
    conn: &mut sqlx::PgConnection,
    schedule: i64,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(&format!(
        "SELECT EXISTS (SELECT 1 FROM facade_task t JOIN facade_schedule s ON s.id = t.schedule_id
                         WHERE s.id = $1 AND {BLOCKING_RUN})"
    ))
    .bind(schedule)
    .fetch_one(conn)
    .await
}

/// How many of the schedule's most recent finished runs failed in a row, and why the newest did.
///
/// With `ALLOW` overlap the next run is planned before the previous one finished, so "the
/// previous run" is not known terminal at refill time; the streak is read off the finished runs
/// instead, which gives the same number however often it is asked.
async fn failure_streak(
    conn: &mut sqlx::PgConnection,
    schedule: i64,
) -> Result<(i32, Option<String>), sqlx::Error> {
    let recent: Vec<(i64, String)> = sqlx::query_as(
        "SELECT id, latest_event_kind FROM facade_task WHERE schedule_id = $1 AND is_done
          ORDER BY finished_at DESC NULLS LAST, id DESC LIMIT 50",
    )
    .bind(schedule)
    .fetch_all(&mut *conn)
    .await?;
    let failed = |kind: &str| kind == "FAILED" || kind == "CRITICAL";
    let streak = recent.iter().take_while(|(_, kind)| failed(kind)).count();
    let error = match recent.first() {
        Some((task, kind)) if failed(kind) => Some(run_error(conn, *task, kind).await?),
        _ => None,
    };
    Ok((i32::try_from(streak).unwrap_or(i32::MAX), error))
}

/// Give one schedule its next run, if it is due one (`refill_one`). True when a run was created.
pub async fn refill_one(ctx: &Context, schedule_id: i64) -> Result<bool, sqlx::Error> {
    let now = Utc::now();
    let mut tx = ctx.db.begin().await?;
    let schedule: Option<Schedule> = sqlx::query_as(&format!(
        "SELECT {COLUMNS} FROM facade_schedule s JOIN facade_caller c ON c.id = s.caller_id
          WHERE s.id = $1 AND s.enabled FOR NO KEY UPDATE OF s SKIP LOCKED"
    ))
    .bind(schedule_id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(schedule) = schedule else {
        return Ok(false);
    };
    if schedule.ended(now)
        || has_blocking_run(&mut tx, schedule.id).await?
        || schedule.refill_after.is_some_and(|after| after > now)
    {
        return Ok(false);
    }
    let last: Option<(i64, String)> = sqlx::query_as(
        "SELECT id, latest_event_kind FROM facade_task WHERE schedule_id = $1 ORDER BY created_at DESC LIMIT 1",
    )
    .bind(schedule.id)
    .fetch_optional(&mut *tx)
    .await?;

    let slot = match plan_next(ctx, &schedule, now).await {
        Ok(Some(slot)) => slot,
        // Its next slot lies past its end: it has nothing more to run.
        Ok(None) => return Ok(false),
        Err(error) => {
            // The schedule is fine; its target is not: record, retry later.
            tracing::warn!(
                "Schedule {}: could not create the next run: {error}",
                schedule.id
            );
            sqlx::query(
                "UPDATE facade_schedule SET last_error = $2, last_error_at = now(), refill_after = $3,
                        updated_at = now() WHERE id = $1",
            )
            .bind(schedule.id)
            .bind(format!("Could not create the next run: {error}"))
            .bind(now + Duration::seconds(REFILL_RETRY_SECONDS))
            .execute(&mut *tx)
            .await?;
            tx.commit().await?;
            signals::rule_changed(
                ctx,
                signals::Rule::Schedule(schedule.id),
                schedule.organization_id,
            )
            .await;
            return Ok(false);
        }
    };

    // Counted here, once per finished run: this is the only moment a run is known terminal AND
    // the next one exists, so a retried refill never counts the same run twice.
    let (failures, last_error) = if schedule.allows_overlap() {
        failure_streak(&mut tx, schedule.id).await?
    } else {
        match &last {
            Some((task, kind)) if kind == "FAILED" || kind == "CRITICAL" => (
                schedule.consecutive_failures + 1,
                Some(run_error(&mut tx, *task, kind).await?),
            ),
            Some(_) => (0, None),
            None => (schedule.consecutive_failures, None),
        }
    };
    let judged = last.is_some() || schedule.allows_overlap();
    // The run just planned counts, and its slot is where catch-up continues from.
    sqlx::query(
        "UPDATE facade_schedule
            SET consecutive_failures = CASE WHEN $2 THEN $3 ELSE consecutive_failures END,
                last_error = CASE WHEN $2 THEN $4 ELSE last_error END,
                last_error_at = CASE WHEN NOT $2 THEN last_error_at WHEN $4 IS NULL THEN NULL
                                     WHEN $4 IS DISTINCT FROM last_error THEN now() ELSE last_error_at END,
                refill_after = NULL, run_count = run_count + 1, last_fired_at = now(), last_slot_at = $5,
                updated_at = now()
          WHERE id = $1",
    )
    .bind(schedule.id)
    .bind(judged)
    .bind(failures)
    .bind(last_error)
    .bind(slot)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    signals::rule_changed(
        ctx,
        signals::Rule::Schedule(schedule.id),
        schedule.organization_id,
    )
    .await;
    Ok(true)
}

/// Create the run of the schedule's next free slot, as the schedule's caller; the slot it took,
/// or `None` when that slot lies past the schedule's end.
async fn plan_next(
    ctx: &Context,
    schedule: &Schedule,
    now: DateTime<Utc>,
) -> Result<Option<DateTime<Utc>>, String> {
    let principal = schedule.principal(ctx).await.map_err(|e| e.to_string())?;
    let timing = schedule.timing();
    let mut after = schedule.search_from(now)?;
    for _ in 0..MAX_SLOT_SKIPS {
        let slot = timing.next_slot(schedule.created_at, after)?;
        if schedule.ends_at.is_some_and(|end| slot > end) {
            return Ok(None);
        }
        let assigned = backend::assign_with_status(
            ctx,
            &principal,
            &schedule.assign_input(slot_reference(schedule.id, slot), Some(slot)),
            AssignOrigin {
                schedule: Some(schedule.id),
                ..AssignOrigin::default()
            },
        )
        .await
        .map_err(|e| e.to_string())?;
        if assigned.created {
            return Ok(Some(slot));
        }
        // This slot already has its run (cancelled, or triggered): the next one.
        after = slot;
    }
    Err(format!(
        "{MAX_SLOT_SKIPS} consecutive slots already had a run"
    ))
}

async fn run_error(
    conn: &mut sqlx::PgConnection,
    task: i64,
    kind: &str,
) -> Result<String, sqlx::Error> {
    let message: Option<Option<String>> = sqlx::query_scalar(
        "SELECT message FROM facade_taskevent WHERE task_id = $1 AND kind = $2 ORDER BY id DESC LIMIT 1",
    )
    .bind(task)
    .bind(kind)
    .fetch_optional(conn)
    .await?;
    Ok(match message.flatten().filter(|m| !m.is_empty()) {
        Some(message) => format!("Run {task} ended {kind}: {message}"),
        None => format!("Run {task} ended {kind}"),
    })
}

/// One pass over the schedules that are due a run (`refill_schedules`): how many got one.
pub async fn refill_schedules(ctx: &Context, limit: i64) -> Result<usize, sqlx::Error> {
    let candidates: Vec<i64> = sqlx::query_scalar(&format!(
        "SELECT s.id FROM facade_schedule s
          WHERE s.enabled AND (s.refill_after IS NULL OR s.refill_after <= now())
            AND (s.ends_at IS NULL OR s.ends_at > now())
            AND (s.max_runs IS NULL OR s.run_count < s.max_runs)
            AND NOT EXISTS (SELECT 1 FROM facade_task t WHERE {BLOCKING_RUN})
          ORDER BY s.id LIMIT $1"
    ))
    .bind(limit)
    .fetch_all(&ctx.db)
    .await?;
    let mut planned = 0;
    for schedule in candidates {
        if refill_one(ctx, schedule).await? {
            planned += 1;
        }
    }
    Ok(planned)
}

/// Drop a run that has not been handed over yet, so the next refill plans it again
/// (`cancel_waiting_run`). Used when a schedule is disabled, retimed, retargeted or deleted. A
/// run that is already executing is left alone: it finishes, and the refill after it uses the
/// new settings.
pub async fn cancel_waiting_run(
    ctx: &Context,
    schedule: i64,
    caller: Option<i64>,
) -> BackendResult<()> {
    let waiting: Option<i64> = sqlx::query_scalar(
        "SELECT id FROM facade_task WHERE schedule_id = $1 AND NOT is_done AND dispatch_attempts = 0
          ORDER BY id LIMIT 1",
    )
    .bind(schedule)
    .fetch_optional(&ctx.db)
    .await?;
    if let Some(run) = waiting {
        backend::request_control(ctx, &run.to_string(), Control::Cancel, caller).await?;
    }
    Ok(())
}

/// Plan a schedule now, not on the next tick, so its next run exists when the mutation answers.
/// `replan`: its timing, target or switch changed, so the waiting run is cancelled first and the
/// backoff of the old settings forgotten. True when a run was created.
pub async fn plan(
    ctx: &Context,
    schedule: i64,
    replan: bool,
    caller: Option<i64>,
) -> BackendResult<bool> {
    if replan {
        cancel_waiting_run(ctx, schedule, caller).await?;
    }
    sqlx::query("UPDATE facade_schedule SET refill_after = NULL WHERE id = $1")
        .bind(schedule)
        .execute(&ctx.db)
        .await?;
    Ok(refill_one(ctx, schedule).await?)
}

/// Run now (`trigger`). The waiting run is moved to now; with no open run, a one-off run is
/// created.
///
/// The moved run *replaces* its slot: the refill after it plans the slot following that one.
/// Without overlap, a run that is already executing is not doubled: the refusal is the overlap
/// guarantee. With `ALLOW`, an executing run is no obstacle.
pub async fn trigger(ctx: &Context, schedule_id: i64) -> BackendResult<i64> {
    let mut tx = ctx.db.begin().await?;
    let schedule: Option<Schedule> = sqlx::query_as(&format!(
        "SELECT {COLUMNS} FROM facade_schedule s JOIN facade_caller c ON c.id = s.caller_id
          WHERE s.id = $1 FOR NO KEY UPDATE OF s"
    ))
    .bind(schedule_id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(schedule) = schedule else {
        return Err(BackendError::Refused(format!("No Schedule {schedule_id}")));
    };
    // The waiting run first: with overlap, executing runs may be open beside it.
    let open: Option<(i64, i16)> = sqlx::query_as(
        "SELECT id, dispatch_attempts FROM facade_task WHERE schedule_id = $1 AND NOT is_done
          ORDER BY dispatch_attempts, id LIMIT 1 FOR UPDATE",
    )
    .bind(schedule.id)
    .fetch_optional(&mut *tx)
    .await?;
    let open = open.filter(|(_, attempts)| *attempts == 0 || !schedule.allows_overlap());
    match open {
        Some((_, attempts)) if attempts != 0 => Err(BackendError::Refused(
            "A run of this schedule is already executing".into(),
        )),
        Some((run, _)) => {
            // The waiting run keeps its identity (and its slot's reference): only its time moves.
            sqlx::query(
                "UPDATE facade_task SET not_before = now(), revision = revision + 1, updated_at = now() WHERE id = $1",
            )
            .bind(run)
            .execute(&mut *tx)
            .await?;
            tx.commit().await?;
            signals::task_saved(ctx, run, false).await;
            Ok(run)
        }
        None => {
            let principal = schedule.principal(ctx).await?;
            let reference = format!(
                "schedule:{}:manual:{}",
                schedule.id,
                uuid::Uuid::new_v4().simple()
            );
            let assigned = backend::assign_with_status(
                ctx,
                &principal,
                &schedule.assign_input(reference, None),
                AssignOrigin {
                    schedule: Some(schedule.id),
                    ..AssignOrigin::default()
                },
            )
            .await?;
            sqlx::query(
                "UPDATE facade_schedule SET run_count = run_count + 1, last_fired_at = now(), updated_at = now() WHERE id = $1",
            )
            .bind(schedule.id)
            .execute(&mut *tx)
            .await?;
            tx.commit().await?;
            signals::rule_changed(
                ctx,
                signals::Rule::Schedule(schedule.id),
                schedule.organization_id,
            )
            .await;
            Ok(assigned.task)
        }
    }
}

/// The next `count` slots of a timing after now, as takt reads it (`schedule/upcoming`).
pub fn upcoming(
    timing: &Timing,
    created_at: DateTime<Utc>,
    count: usize,
) -> Result<Vec<DateTime<Utc>>, String> {
    let mut slots = Vec::with_capacity(count);
    let mut after = Utc::now();
    for _ in 0..count {
        after = timing.next_slot(created_at, after)?;
        slots.push(after);
    }
    Ok(slots)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_slot_is_named_as_python_named_it() {
        let whole: DateTime<Utc> = "2026-10-01T02:00:00Z".parse().unwrap();
        let fractional: DateTime<Utc> = "2026-10-01T02:00:00.026490Z".parse().unwrap();
        assert_eq!(
            slot_reference(7, whole),
            "schedule:7:2026-10-01T02:00:00+00:00"
        );
        assert_eq!(
            slot_reference(7, fractional),
            "schedule:7:2026-10-01T02:00:00.026490+00:00"
        );
    }
}

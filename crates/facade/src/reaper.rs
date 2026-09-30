//! The reconciler: every deadline the server enforces fires from here (`facade/reaper.py`).
//!
//! No deadline lives in a process-local timer. Each starts at a database column, and this loop
//! acts on it once it has passed:
//!
//! | sweep                             | deadline starts at | setting               |
//! |-----------------------------------|--------------------|-----------------------|
//! | `reconcile_stale_agents`          | `Agent.last_seen`  | `agent_stale_after`   |
//! | `reconcile_disconnected_agents`   | `Agent.last_seen`  | `grace`               |
//! | `dispatch_due_tasks`              | `Task.not_before`  | (the task's own)      |
//! | `reconcile_unpicked_tasks`        | `Task.dispatched_at` | `pickup_deadline`   |
//! | `escalate_due_controls`           | `Task.interrupt_at`  | `control_deadline`  |
//! | `expire_disconnected_tasks`       | `Task.dispatched_at` | `disconnected_expiry` |
//!
//! The Python reaper runs more sweeps (service agents, schedules, triggers, embeddings,
//! retention); those stay in Python and are not run here.
//!
//! Deliberately: a reaper that starts heals whatever an earlier one left behind on its first
//! tick, which runs at once; a reaper may die at any instant and nothing pending dies with it; any
//! number may run, since every transition is a row-locked claim with one winner. The redis tick
//! token only keeps N reapers from scanning the same rows in the same second.

use std::sync::OnceLock;
use std::time::Duration;

use crate::clock;
use crate::context::Context;
use crate::persist::reconcile;
use crate::redis_keys;

/// How many rows one task sweep takes per tick; the next tick drains the rest.
const SWEEP_LIMIT: i64 = 200;

/// The ordered sweeps (`_sweeps`). Agents before tasks: healing a stuck-connected agent is what
/// makes its work visible to the task sweeps of the same tick.
pub const SWEEPS: [&str; 6] = [
    "stale agents",
    "disconnected agents",
    "due tasks",
    "unpicked tasks",
    "due controls",
    "expired tasks",
];

/// Run one sweep by name; the count it acted on.
pub async fn run_sweep(ctx: &Context, name: &str) -> Result<usize, sqlx::Error> {
    match name {
        "stale agents" => reconcile::reconcile_stale_agents(ctx).await,
        "disconnected agents" => reconcile::reconcile_disconnected_agents(ctx).await,
        "due tasks" => reconcile::dispatch_due_tasks(ctx, SWEEP_LIMIT).await,
        "unpicked tasks" => reconcile::reconcile_unpicked_tasks(ctx, SWEEP_LIMIT).await,
        "due controls" => reconcile::escalate_due_controls(ctx, SWEEP_LIMIT).await,
        "expired tasks" => reconcile::expire_disconnected_tasks(ctx, SWEEP_LIMIT).await,
        other => unreachable!("no sweep named {other:?}"),
    }
}

/// One full pass (`run_sweeps`). Every sweep is isolated: one failing never starves the others.
///
/// A backend whose clock drifted does not sweep at all: every sweep decides whether some other
/// backend's agent is dead by comparing timestamps, so a skewed clock makes a wrong decision,
/// not a late one (see [`clock`]). Skipping is safe: the deadlines are in the database, and a
/// correctly clocked backend acts on them. Returns whether it swept.
pub async fn run_sweeps(ctx: &Context) -> bool {
    let limit = clock::max_skew_seconds(&ctx.settings);
    if let Some(skew) = clock::check_skew(&ctx.db).await {
        if skew.abs() > limit {
            tracing::error!(
                "Clock is {skew:.1}s off the database (limit {limit:.1}s) — skipping the sweeps. Check NTP on this host."
            );
            return false;
        }
    }
    for name in SWEEPS {
        match run_sweep(ctx, name).await {
            Ok(0) => {}
            Ok(acted) => tracing::info!("Reaper: {name} → {acted}"),
            Err(e) => tracing::error!("Reaper sweep {name:?} failed; continuing: {e}"),
        }
    }
    true
}

/// This process, as the tick token's holder.
fn process_id() -> &'static str {
    static ID: OnceLock<String> = OnceLock::new();
    ID.get_or_init(|| uuid::Uuid::new_v4().simple().to_string())
}

/// Whether this backend sweeps this tick (`_take_tick_token`): work de-duplication across
/// backends, nothing more. `SET NX PX`: the first backend to tick holds the token for most of
/// one interval, the others skip; the holder dying costs at most one interval. Any redis
/// problem: sweep anyway.
pub async fn take_tick_token(ctx: &Context) -> bool {
    let px = ((ctx.settings.sweep_interval.as_secs_f64() * 800.0) as u64).max(1);
    let mut redis = ctx.redis.clone();
    let taken: redis::RedisResult<Option<String>> = redis::cmd("SET")
        .arg(redis_keys::key(&ctx.settings, &[&"reaper", &"tick"]))
        .arg(process_id())
        .arg("NX")
        .arg("PX")
        .arg(px)
        .query_async(&mut redis)
        .await;
    match taken {
        Ok(taken) => taken.is_some(),
        Err(e) => {
            tracing::debug!("reaper tick token unavailable; sweeping anyway: {e}");
            true
        }
    }
}

/// Sweep forever, every `sweep_interval` (`run_forever`). The first pass runs almost at once
/// (after a little jitter, so reapers started together do not tick in lockstep): it is what heals
/// everything a previous process left behind.
pub async fn run_forever(ctx: Context) {
    let jitter = uuid::Uuid::new_v4().as_u128() % 500;
    tokio::time::sleep(Duration::from_millis(jitter as u64)).await;
    loop {
        if take_tick_token(&ctx).await {
            run_sweeps(&ctx).await;
        }
        tokio::time::sleep(ctx.settings.sweep_interval).await;
    }
}

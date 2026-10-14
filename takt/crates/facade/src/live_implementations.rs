//! `Action.live_implementations`: how many implementations of an action sit on an agent that can
//! take work now — kept by the database, recomputed here.
//!
//! The server's triggers (migration `facade 0006_action_assignable`) never compute it: whoever
//! changes an agent's `connected`/`kind` or an implementation only appends the affected action
//! ids to `facade_actionlivenessdirty` and notifies [`CHANNEL`], which Postgres delivers when that
//! transaction commits. A writer therefore waits on nobody — a long registration and a lease
//! claim never contend for action rows.
//!
//! [`drain`] runs `facade_refresh_live_implementations`, which takes marks and action rows
//! `SKIP LOCKED`, recomputes in a statement of its own (every change it took a mark for has
//! committed) and puts back the marks of actions someone else holds. It never waits, so it cannot
//! deadlock, and any number of replicas may drain at once: every replica drains on every notice,
//! with no claim in redis. The reaper drains every tick too ([`crate::reaper`]), for notices sent
//! while no replica listened.

use std::time::Duration;

use sqlx::postgres::PgListener;

use crate::context::Context;

/// The channel the triggers notify on.
pub const CHANNEL: &str = "rekuest_action_liveness";

/// Marks one call takes.
const BATCH: i32 = 500;
/// Calls one drain makes at most: a burst beyond `ROUNDS * BATCH` actions finishes on the next
/// notice or tick.
const ROUNDS: usize = 20;

/// Recompute every marked action this replica can take now. Returns how many it recomputed.
pub async fn drain(ctx: &Context) -> Result<usize, sqlx::Error> {
    let mut total = 0;
    for _ in 0..ROUNDS {
        let done: i32 = sqlx::query_scalar("SELECT facade_refresh_live_implementations($1)")
            .bind(BATCH)
            .fetch_one(&ctx.db)
            .await?;
        if done <= 0 {
            break;
        }
        total += done as usize;
    }
    Ok(total)
}

/// Listen until the connection fails, draining on every notice. Notices that arrive while a
/// drain runs are folded into the next one.
async fn listen(ctx: &Context) -> Result<(), sqlx::Error> {
    let mut listener = PgListener::connect_with(&ctx.db).await?;
    listener.listen(CHANNEL).await?;
    // Whatever was marked before this replica listened.
    drain(ctx).await?;
    loop {
        listener.recv().await?;
        while listener.next_buffered().is_some() {}
        if let Err(e) = drain(ctx).await {
            tracing::warn!("Live implementations: drain failed; the reaper retries: {e}");
        }
    }
}

/// Listen forever (`run_forever`), reconnecting when the connection is lost.
pub async fn run_forever(ctx: Context) {
    loop {
        if let Err(e) = listen(&ctx).await {
            tracing::warn!("Live implementations: lost the listener; reconnecting: {e}");
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

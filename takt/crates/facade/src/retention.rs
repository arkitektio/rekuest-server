//! Retention: terminal task trees and processed signals past their horizon are deleted.
//!
//! A terminal ROOT task whose `finished_at` is past the horizon goes with its whole tree, their
//! events, instructs and patches (locks held and signals caused are released). A done root can
//! still have a live descendant (a cancel goes to the mother only), so roots with any not-done
//! member are skipped.
//!
//! Task retention is an operator's opt-in (zero, the default, keeps everything): deleting past
//! runs also removes them from replay. Ephemeral trees (the runs of a schedule with
//! `ephemeral_runs`: services' housekeeping sweeps, thousands a day) have their own, shorter
//! horizon, which is on by default: they are never replay candidates and would pile up forever.
//!
//! One batch per slow reaper tick, so a backlog drains over successive ticks and never in one
//! long transaction. Any number of replicas may run it: a root another one already deleted
//! simply matches nothing.

use std::time::Duration;

use crate::context::Context;
use crate::deletion;

/// How many roots (or signals) one batch deletes.
pub const BATCH: i64 = 500;

/// One retention pass: a batch of signals, of ephemeral trees and of ordinary trees
/// (`sweep_terminal_tasks`). The task roots deleted.
pub async fn sweep(ctx: &Context) -> Result<usize, sqlx::Error> {
    sweep_signals(ctx).await?;
    let mut deleted = sweep_tasks(ctx, ctx.settings.ephemeral_task_retention, true).await?;
    deleted += sweep_tasks(ctx, ctx.settings.task_retention, false).await?;
    Ok(deleted)
}

/// Drop one batch of processed signals past the signal horizon (`_sweep_signals`), with their
/// firing log; the runs they caused keep theirs as null.
pub async fn sweep_signals(ctx: &Context) -> Result<usize, sqlx::Error> {
    let retention = ctx.settings.signal_retention;
    if retention.is_zero() {
        return Ok(0);
    }
    let mut tx = ctx.db.begin().await?;
    let signals: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM facade_signal WHERE processed_at < now() - make_interval(secs => $1)
          LIMIT $2 FOR UPDATE SKIP LOCKED",
    )
    .bind(retention.as_secs_f64())
    .bind(BATCH)
    .fetch_all(&mut *tx)
    .await?;
    if signals.is_empty() {
        return Ok(0);
    }
    sqlx::query("UPDATE facade_task SET signal_id = NULL WHERE signal_id = ANY($1)")
        .bind(&signals)
        .execute(&mut *tx)
        .await?;
    // The firing log lives as long as its signal.
    sqlx::query("DELETE FROM facade_firing WHERE signal_id = ANY($1)")
        .bind(&signals)
        .execute(&mut *tx)
        .await?;
    sqlx::query("DELETE FROM facade_signal WHERE id = ANY($1)")
        .bind(&signals)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(signals.len())
}

/// Delete one batch of terminal root trees older than `retention` (`_sweep`); `ephemeral`
/// restricts it to ephemeral ones. Zero disables it.
async fn sweep_tasks(
    ctx: &Context,
    retention: Duration,
    ephemeral: bool,
) -> Result<usize, sqlx::Error> {
    if retention.is_zero() {
        return Ok(0);
    }
    let mut tx = ctx.db.begin().await?;
    let roots: Vec<i64> = sqlx::query_scalar(
        "SELECT t.id FROM facade_task t
          WHERE t.is_done AND t.root_id IS NULL AND t.finished_at < now() - make_interval(secs => $1)
            AND ($2 = false OR t.ephemeral)
            AND NOT EXISTS (SELECT 1 FROM facade_task d WHERE d.root_id = t.id AND NOT d.is_done)
          LIMIT $3 FOR UPDATE OF t SKIP LOCKED",
    )
    .bind(retention.as_secs_f64())
    .bind(ephemeral)
    .bind(BATCH)
    .fetch_all(&mut *tx)
    .await?;
    if roots.is_empty() {
        return Ok(0);
    }
    deletion::delete_tasks(&mut tx, &roots).await?;
    tx.commit().await?;
    tracing::info!(
        "Retention deleted {} terminal {}task tree(s) past {}s.",
        roots.len(),
        if ephemeral { "ephemeral " } else { "" },
        retention.as_secs()
    );
    Ok(roots.len())
}

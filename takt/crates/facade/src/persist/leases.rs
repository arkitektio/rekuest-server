//! The executor lease: one live connection per agent (`facade/persist/leases.py`).
//!
//! Transitions (claim, release, revoke) take the agent's row lock; renewal (the heartbeat) is a
//! lock-free compare-and-set on `active_connection_id`, whose row count is the answer to "am I
//! still the owner?". A claim writes the new socket's id and a revoke clears it, which is what
//! fences a previous connection: its next renewal matches no row, and it closes.

use chrono::Utc;
use sqlx::PgPool;

use crate::context::Context;
use crate::liveness::{agent_is_live, agent_is_stale};
use crate::registration::clear_drawers;
use crate::settings::Settings;
use crate::signals;

/// Whether a registration comes from the process that held the lease before: both sessions
/// known and equal. That process keeps its in-flight work and its memory drawers.
pub fn same_process(prior: Option<&str>, session: Option<&str>) -> bool {
    matches!((prior, session), (Some(a), Some(b)) if a == b)
}

/// Whether a registration provably comes from a NEW process: both sessions known and different.
/// The previous process's in-flight work is orphaned.
pub fn fresh_process(prior: Option<&str>, session: Option<&str>) -> bool {
    matches!((prior, session), (Some(a), Some(b)) if a != b)
}

/// The outcome of claiming the lease (`LeaseClaim`).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct LeaseClaim {
    pub claimed: bool,
    /// Tasks the agent may still hold: asked about in `INIT` (same process reconnecting).
    pub inquiries: Vec<i64>,
    /// In-flight work a previous process orphaned (a fresh process took over): for
    /// [`crate::persist::reconcile::fail_and_cascade_inflight`], once the lease is ours.
    pub orphaned: Vec<i64>,
    /// A previous connection existed and should be told to stop.
    pub displaced_incumbent: bool,
    pub prior_session: Option<String>,
}

/// Decide the executor singleton and take the lease, atomically (`_claim_lease_sync`).
///
/// Refused only when the incumbent is provably live (connected AND a fresh heartbeat), `force`
/// is not set, and it is not this same process's previous connection. A stale incumbent (its
/// process died without a clean close) is displaced without `force`.
async fn claim_lease(
    db: &PgPool,
    settings: &Settings,
    agent: i64,
    connection_id: &str,
    session_id: Option<&str>,
    force: bool,
) -> Result<LeaseClaim, sqlx::Error> {
    let mut tx = db.begin().await?;
    let (connected, last_seen, prior_session): (
        bool,
        Option<chrono::DateTime<Utc>>,
        Option<String>,
    ) = sqlx::query_as(
        "SELECT connected, last_seen, active_session_id FROM facade_agent WHERE id = $1 FOR UPDATE",
    )
    .bind(agent)
    .fetch_one(&mut *tx)
    .await?;

    if agent_is_live(settings, connected, last_seen, Utc::now())
        && !force
        && !same_process(prior_session.as_deref(), session_id)
    {
        tx.rollback().await?;
        return Ok(LeaseClaim {
            prior_session,
            ..LeaseClaim::default()
        });
    }

    // The connection id is the fencing token: a fresh one per socket, so writing it displaces
    // whoever held the lease before.
    sqlx::query(
        "UPDATE facade_agent
            SET connected = true, last_seen = $2,
                active_connection_id = $3, active_session_id = $4
          WHERE id = $1",
    )
    .bind(agent)
    .bind(Utc::now())
    .bind(connection_id)
    .bind(session_id)
    .execute(&mut *tx)
    .await?;

    // The memory shelf lives in the agent process: unless the same process is reconnecting,
    // whatever it shelved is gone, and so are the references to it.
    if !same_process(prior_session.as_deref(), session_id) {
        clear_drawers(&mut *tx, agent).await?;
    }
    tx.commit().await?;

    Ok(LeaseClaim {
        claimed: true,
        inquiries: vec![],
        orphaned: vec![],
        displaced_incumbent: connected,
        prior_session,
    })
}

/// Open work the agent may hold: not done, not an undelivered `QUEUED` task, not a virtual
/// higher-order wrapper (`_in_flight_q`).
pub const IN_FLIGHT: &str = "t.is_done = false
    AND NOT (t.latest_event_kind = 'QUEUED' AND t.picked_up_at IS NULL)
    AND NOT EXISTS (SELECT 1 FROM facade_implementation i
                     WHERE i.id = t.implementation_id AND i.higher_order_for_id IS NOT NULL)";

/// Claim the lease for a connecting agent (`on_agent_connected`).
///
/// Work it never picked up restarts its pickup clock. A same-session reconnect is asked about
/// its in-flight tasks (`inquiries`); a fresh process's in-flight work is orphaned (`orphaned`),
/// and the caller fails and cascades it. Not through `reconcile_orphaned_executor_work`: the
/// agent is live again, which is exactly what makes that one step aside.
pub async fn on_agent_connected(
    db: &PgPool,
    settings: &Settings,
    agent: i64,
    connection_id: &str,
    session_id: Option<&str>,
    force: bool,
) -> Result<LeaseClaim, sqlx::Error> {
    let mut claim = claim_lease(db, settings, agent, connection_id, session_id, force).await?;
    if !claim.claimed {
        return Ok(claim);
    }

    sqlx::query(
        "UPDATE facade_task SET dispatched_at = now()
          WHERE agent_id = $1 AND is_done = false AND picked_up_at IS NULL
            AND latest_event_kind = 'QUEUED' AND dispatched_at IS NOT NULL",
    )
    .bind(agent)
    .execute(db)
    .await?;

    let in_flight: Vec<i64> = sqlx::query_scalar(&format!(
        "SELECT t.id FROM facade_task t WHERE t.agent_id = $1 AND {IN_FLIGHT} ORDER BY t.id"
    ))
    .bind(agent)
    .fetch_all(db)
    .await?;

    if fresh_process(claim.prior_session.as_deref(), session_id) {
        claim.orphaned = in_flight;
    } else {
        claim.inquiries = in_flight;
    }
    Ok(claim)
}

/// Release the lease on a clean close, only if it is still ours (`_release_lease_sync`). A
/// displaced connection shutting down releases nothing: the new owner is authoritative.
pub async fn release_lease(
    db: &PgPool,
    agent: i64,
    connection_id: &str,
) -> Result<bool, sqlx::Error> {
    let mut tx = db.begin().await?;
    let active: Option<String> = sqlx::query_scalar(
        "SELECT active_connection_id FROM facade_agent WHERE id = $1 FOR UPDATE",
    )
    .bind(agent)
    .fetch_one(&mut *tx)
    .await?;
    if active.as_deref() != Some(connection_id) {
        tx.rollback().await?;
        return Ok(false);
    }
    sqlx::query("UPDATE facade_agent SET connected = false, last_seen = $2 WHERE id = $1")
        .bind(agent)
        .bind(Utc::now())
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(true)
}

/// Revoke one stuck-connected agent's lease under its row lock (`_revoke_lease_sync`). Returns
/// whether we won.
///
/// Re-checks staleness under the lock, so this is also the claim that makes the stale sweep
/// multi-worker safe: the first backend flips `connected`, the others find a row that is no
/// longer stale and back off. A reconnect that landed between the scan and the lock lands here
/// too. Clearing `active_connection_id` fences the wedged connection: if its worker ever
/// resumes, its renewal matches no row and it closes instead of resurrecting the agent, and its
/// disconnect releases nothing. `last_seen` is left alone: it is the true last contact the
/// orphan cutoff depends on.
pub async fn revoke_lease(ctx: &Context, agent: i64) -> Result<bool, sqlx::Error> {
    let mut tx = ctx.db.begin().await?;
    let (connected, last_seen): (bool, Option<chrono::DateTime<Utc>>) =
        sqlx::query_as("SELECT connected, last_seen FROM facade_agent WHERE id = $1 FOR UPDATE")
            .bind(agent)
            .fetch_one(&mut *tx)
            .await?;
    if !agent_is_stale(&ctx.settings, connected, last_seen, Utc::now()) {
        tx.rollback().await?;
        return Ok(false); // healed or reconnected while we waited for the lock
    }
    sqlx::query(
        "UPDATE facade_agent SET connected = false, active_connection_id = NULL WHERE id = $1",
    )
    .bind(agent)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    signals::agent_saved(ctx, agent, false).await;
    Ok(true)
}

/// Renew the lease: the heartbeat's compare-and-set. False when this connection was displaced
/// or revoked, and must close (`renew_agent_lease`).
pub async fn renew_agent_lease(
    db: &PgPool,
    agent: i64,
    connection_id: &str,
) -> Result<bool, sqlx::Error> {
    let rows = sqlx::query(
        "UPDATE facade_agent SET last_seen = $3
          WHERE id = $1 AND active_connection_id = $2 AND connected",
    )
    .bind(agent)
    .bind(connection_id)
    .bind(Utc::now())
    .execute(db)
    .await?
    .rows_affected();
    Ok(rows == 1)
}

/// Whether this connection still holds the agent's lease: asked before every delivery
/// (`holds_lease`).
pub async fn holds_lease(
    db: &PgPool,
    agent: i64,
    connection_id: &str,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM facade_agent
                         WHERE id = $1 AND active_connection_id = $2 AND connected)",
    )
    .bind(agent)
    .bind(connection_id)
    .fetch_one(db)
    .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sessions_tell_the_same_process_from_a_fresh_one() {
        assert!(same_process(Some("s"), Some("s")));
        assert!(!same_process(Some("s"), None) && !same_process(None, None));
        assert!(fresh_process(Some("a"), Some("b")));
        assert!(!fresh_process(None, Some("b")) && !fresh_process(Some("a"), Some("a")));
    }
}

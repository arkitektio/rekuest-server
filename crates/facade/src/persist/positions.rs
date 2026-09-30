//! Session positions: handle every numbered frame once, however often it is sent
//! (`facade/persist/positions.py`).
//!
//! A numbering agent stamps each frame with `pos` (1, 2, 3, … per session) and `journal_session`,
//! keeps it until a `JOURNAL_ACK` covers it, and re-sends the rest in order after a reconnect. The
//! server keeps one watermark per session, `Session.projected_pos`: a frame at or below it is a
//! resend and is skipped. Handling one frame is claim → project → confirm:
//!
//! * **claim**: a conditional UPDATE that moves `claimed_pos` to `pos` only while nothing else is
//!   in flight (`claimed_pos = projected_pos`). Two backends that got the same frame cannot both
//!   win; the loser waits, and a claim older than [`CLAIM_TIMEOUT`] is taken over.
//! * **project**: the router routes the frame.
//! * **confirm**: `projected_pos = pos`. A projection that failed releases the claim instead.
//!
//! A frame beyond `projected_pos + 1` means the agent no longer holds the frames in between: the
//! gap is logged and the watermark moves past it.

use std::time::Duration;

use chrono::{DateTime, Utc};
use sqlx::PgPool;

use crate::messages::{is_probe_task, AgentFrame};

/// How long a claim may stay unconfirmed before another backend takes the frame over.
pub const CLAIM_TIMEOUT: Duration = Duration::from_secs(30);
const WAIT_POLL: Duration = Duration::from_millis(50);

/// What to do with a numbered frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Position {
    /// Claimed: project it, then [`confirm_position`] (or [`release_position`] if it failed).
    Project,
    /// At or below the watermark: already handled. Skip it; ack again.
    Duplicate,
}

/// Whether a frame carries a session position (and is not a probe's).
pub fn is_numbered(frame: &AgentFrame) -> bool {
    frame.pos.is_some()
        && frame.journal_session.is_some()
        && !frame.message.task().is_some_and(is_probe_task)
}

/// The frame's `agent_ts` (epoch seconds) as a timestamp, if it carries one.
pub fn agent_time(frame: &AgentFrame) -> Option<DateTime<Utc>> {
    let ts = frame.agent_ts?;
    if !ts.is_finite() {
        return None;
    }
    DateTime::from_timestamp_micros((ts * 1_000_000.0).round() as i64)
}

/// The `agent_pos` / `agent_ts` / `step` a projected row carries: empty without numbering
/// (`position_stamp`).
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Stamp {
    pub agent_pos: Option<i64>,
    pub agent_ts: Option<DateTime<Utc>>,
    pub step: Option<i64>,
}

pub fn position_stamp(frame: &AgentFrame) -> Stamp {
    if !is_numbered(frame) {
        return Stamp::default();
    }
    Stamp {
        agent_pos: frame.pos.map(|p| p as i64),
        agent_ts: agent_time(frame),
        step: frame.task_step.map(|s| s as i64),
    }
}

/// The session row of `(agent, session_id)`, created if it does not exist.
pub async fn get_or_create_session(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
    session_id: &str,
) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar(
        "INSERT INTO facade_session (agent_id, session_id, created_at, updated_at, projected_pos, claimed_pos)
         VALUES ($1, $2, now(), now(), 0, 0)
         ON CONFLICT (agent_id, session_id) DO UPDATE SET session_id = EXCLUDED.session_id
         RETURNING id",
    )
    .bind(agent)
    .bind(session_id)
    .fetch_one(executor)
    .await
}

/// Claim the frame's position for projection, or report it a duplicate. Waits (polling) while
/// another backend projects the preceding position or this one.
pub async fn claim_position(
    db: &PgPool,
    agent: i64,
    journal_session: &str,
    pos: i64,
) -> Result<Position, sqlx::Error> {
    let session = get_or_create_session(db, agent, journal_session).await?;
    loop {
        let current: Option<(i64, i64, Option<DateTime<Utc>>)> = sqlx::query_as(
            "SELECT projected_pos, claimed_pos, claimed_at FROM facade_session WHERE id = $1",
        )
        .bind(session)
        .fetch_optional(db)
        .await?;
        let Some((projected, claimed, claimed_at)) = current else {
            return Ok(Position::Duplicate); // the agent (and its sessions) was deleted underneath us
        };
        if pos <= projected {
            return Ok(Position::Duplicate);
        }
        let now = Utc::now();
        let stale =
            claimed_at.is_none_or(|at| (now - at).to_std().unwrap_or_default() > CLAIM_TIMEOUT);
        let idle = claimed <= projected;
        if !idle && !stale {
            tokio::time::sleep(WAIT_POLL).await; // another backend is projecting; wait for it
            continue;
        }
        if pos > projected + 1 {
            tracing::warn!(
                agent,
                journal_session,
                "positions {}..{} never arrived (the agent no longer holds them); continuing at {pos}",
                projected + 1,
                pos - 1
            );
        }
        // Conditional on exactly what we read: a concurrent claim or confirm changes one of them.
        let won = sqlx::query(
            "UPDATE facade_session SET projected_pos = $5, claimed_pos = $6, claimed_at = $7
              WHERE id = $1 AND projected_pos = $2 AND claimed_pos = $3 AND claimed_at IS NOT DISTINCT FROM $4",
        )
        .bind(session)
        .bind(projected)
        .bind(claimed)
        .bind(claimed_at)
        .bind(pos - 1)
        .bind(pos)
        .bind(now)
        .execute(db)
        .await?
        .rows_affected();
        if won == 1 {
            return Ok(Position::Project);
        }
    }
}

/// The claimed frame is projected: move the watermark onto it.
pub async fn confirm_position(
    db: &PgPool,
    agent: i64,
    journal_session: &str,
    pos: i64,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE facade_session SET projected_pos = claimed_pos
          WHERE agent_id = $1 AND session_id = $2 AND claimed_pos = $3",
    )
    .bind(agent)
    .bind(journal_session)
    .bind(pos)
    .execute(db)
    .await?;
    Ok(())
}

/// The claimed frame's projection failed: give the claim back, so the resend projects it.
pub async fn release_position(
    db: &PgPool,
    agent: i64,
    journal_session: &str,
    pos: i64,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE facade_session SET claimed_pos = projected_pos, claimed_at = NULL
          WHERE agent_id = $1 AND session_id = $2 AND claimed_pos = $3",
    )
    .bind(agent)
    .bind(journal_session)
    .bind(pos)
    .execute(db)
    .await?;
    Ok(())
}

/// The session's watermark: what a `JOURNAL_ACK` may claim.
pub async fn projected_position(
    db: &PgPool,
    agent: i64,
    journal_session: &str,
) -> Result<i64, sqlx::Error> {
    Ok(sqlx::query_scalar(
        "SELECT projected_pos FROM facade_session WHERE agent_id = $1 AND session_id = $2",
    )
    .bind(agent)
    .bind(journal_session)
    .fetch_optional(db)
    .await?
    .unwrap_or(0))
}

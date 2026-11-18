//! Session positions: handle every numbered frame once, however often it is sent
//! (`facade/persist/positions.py`).
//!
//! A numbering agent stamps each frame with `pos` (1, 2, 3, … per session) and `journal_session`,
//! keeps it until a `JOURNAL_ACK` covers it, and re-sends the rest in order after a reconnect. The
//! server keeps one watermark per session, `Session.projected_pos`: a frame at or below it is a
//! resend and is skipped.
//!
//! **The watermark moves in the transaction that projects the frame** ([`advance`]): one
//! conditional UPDATE, `projected_pos = pos WHERE projected_pos = pos - 1`, first in that
//! transaction. It takes the session's row lock, so two backends that got the same frame cannot
//! both project it: the second waits for the first to commit and then finds the position taken.
//! A projection that fails rolls the watermark back with everything else it wrote, so the resend
//! projects it again; one that commits is never projected twice. There is no claim to keep
//! between the two, and nothing to take over from a backend that died in the middle.
//!
//! A frame beyond `projected_pos + 1` is a gap: it is not taken, and the agent is told which
//! position is expected (`JOURNAL_NACK`). Only the agent's `JOURNAL_SKIP` moves the watermark
//! past positions it no longer holds.

use chrono::{DateTime, Utc};
use sqlx::{PgConnection, PgPool};

use crate::messages::{is_probe_task, AgentFrame};

/// What to do with a numbered frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Position {
    /// The watermark is on this frame now, in the transaction it was asked in: project it there
    /// and commit, or roll back.
    Project,
    /// At or below the watermark: already handled. Skip it; ack again.
    Duplicate,
    /// Ahead of the next position: not taken. The agent is told to send again from `expected`
    /// (`JOURNAL_NACK`); only its own `JOURNAL_SKIP` passes a position over.
    Gap { expected: i64 },
}
/// Whether a frame carries a session position (and is not a probe's).
///
/// A state update a probe caused is numbered like any other: the revisions of a state must
/// have no holes, and neither may the positions, or every frame behind it would arrive ahead
/// of a position that is never counted.
pub fn is_numbered(frame: &AgentFrame) -> bool {
    use crate::messages::FromAgent;
    frame.pos.is_some()
        && frame.journal_session.is_some()
        && (matches!(
            frame.message,
            FromAgent::StateUpdate { .. } | FromAgent::StatePatch { .. }
        ) || !frame.message.task().is_some_and(is_probe_task))
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
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Stamp {
    pub agent_pos: Option<i64>,
    /// The journal session `agent_pos` counts in: together, the report's durable key.
    pub agent_session: Option<String>,
    pub agent_ts: Option<DateTime<Utc>>,
    pub step: Option<i64>,
}

pub fn position_stamp(frame: &AgentFrame) -> Stamp {
    if !is_numbered(frame) {
        return Stamp::default();
    }
    Stamp {
        agent_pos: frame.pos.map(|p| p as i64),
        agent_session: frame.journal_session.clone(),
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
        "INSERT INTO facade_session (agent_id, session_id)
         VALUES ($1, $2)
         ON CONFLICT (agent_id, session_id) DO UPDATE SET session_id = EXCLUDED.session_id
         RETURNING id",
    )
    .bind(agent)
    .bind(session_id)
    .fetch_one(executor)
    .await
}

/// The id of the session row of `(agent, session_id)`, created if it does not exist. A read
/// where the row is there, which it is for every frame but a session's first.
pub async fn session_row(
    conn: &mut PgConnection,
    agent: i64,
    session_id: &str,
) -> Result<i64, sqlx::Error> {
    let known: Option<i64> =
        sqlx::query_scalar("SELECT id FROM facade_session WHERE agent_id = $1 AND session_id = $2")
            .bind(agent)
            .bind(session_id)
            .fetch_optional(&mut *conn)
            .await?;
    match known {
        Some(id) => Ok(id),
        None => get_or_create_session(&mut *conn, agent, session_id).await,
    }
}

/// Move the session's watermark onto `pos`, inside the transaction that will project the frame:
/// [`Position::Project`] when it moved (the row is locked until that transaction ends), else
/// why not. Waits while another backend's transaction holds the row.
pub async fn advance(
    conn: &mut PgConnection,
    session: i64,
    pos: i64,
) -> Result<Position, sqlx::Error> {
    let moved = sqlx::query(
        "UPDATE facade_session SET projected_pos = $2, updated_at = now()
          WHERE id = $1 AND projected_pos = $2 - 1",
    )
    .bind(session)
    .bind(pos)
    .execute(&mut *conn)
    .await?
    .rows_affected();
    if moved == 1 {
        return Ok(Position::Project);
    }
    let projected: Option<i64> =
        sqlx::query_scalar("SELECT projected_pos FROM facade_session WHERE id = $1")
            .bind(session)
            .fetch_optional(&mut *conn)
            .await?;
    Ok(match projected {
        // The agent (and its sessions) was deleted underneath us.
        None => Position::Duplicate,
        Some(projected) if pos <= projected => Position::Duplicate,
        Some(projected) => Position::Gap {
            expected: projected + 1,
        },
    })
}

/// The agent no longer holds `from..=to` (`JOURNAL_SKIP`): move the watermark past them, when
/// they cover the next position (what of them is already projected stays so); whether it did.
/// A skip that starts beyond the next position would pass over frames nobody declared gone.
pub async fn skip_positions(
    conn: &mut PgConnection,
    agent: i64,
    journal_session: &str,
    from: i64,
    to: i64,
) -> Result<bool, sqlx::Error> {
    if to < from {
        return Ok(false);
    }
    let session = session_row(&mut *conn, agent, journal_session).await?;
    let moved = sqlx::query(
        "UPDATE facade_session SET projected_pos = $3
          WHERE id = $1 AND projected_pos >= $2 AND projected_pos < $3",
    )
    .bind(session)
    .bind(from - 1)
    .bind(to)
    .execute(&mut *conn)
    .await?
    .rows_affected();
    Ok(moved == 1)
}

/// How far each of these sessions of `agent` is projected (`INIT`'s `positions`); a session the
/// server never saw is at 0.
pub async fn projected_positions(
    db: &PgPool,
    agent: i64,
    sessions: &[String],
) -> Result<std::collections::BTreeMap<String, u64>, sqlx::Error> {
    let known: Vec<(String, i64)> = sqlx::query_as(
        "SELECT session_id, projected_pos FROM facade_session
          WHERE agent_id = $1 AND session_id = ANY($2)",
    )
    .bind(agent)
    .bind(sessions)
    .fetch_all(db)
    .await?;
    let mut positions: std::collections::BTreeMap<String, u64> = sessions
        .iter()
        .map(|session| (session.clone(), 0))
        .collect();
    for (session, projected) in known {
        positions.insert(session, projected.max(0) as u64);
    }
    Ok(positions)
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

//! The dead man switch (docs/session-protocol.md §7): a task armed with one is cancelled when
//! the window holding it has been gone for longer than its seconds.
//!
//! Absence is read from Postgres (`CallerSession.last_seen`, written by a connected session on
//! a beat), never from redis, so a redis restart cancels nothing. And only absence while takt
//! was serving counts: the reaper writes a beat of its own each tick (`SweepBeat`), and when it
//! finds its previous one too old (a deploy, an outage), it first treats every armed session as
//! just seen. A held task (waiting for a person) is not cancelled: it is not running.

use crate::backend::{self, Control};
use crate::context::Context;
use crate::persist::transitions::{self, NewEvent};

const BEAT: &str = "reaper";

/// Forgive the time takt was not serving, then cancel what is due. How many were cancelled.
pub async fn sweep(ctx: &Context, limit: i64) -> Result<usize, sqlx::Error> {
    let gap: Option<f64> = sqlx::query_scalar(
        "WITH previous AS (SELECT at FROM facade_sweepbeat WHERE name = $1),
              written AS (
                INSERT INTO facade_sweepbeat (name, at) VALUES ($1, now())
                ON CONFLICT (name) DO UPDATE SET at = now()
              )
         SELECT EXTRACT(EPOCH FROM now() - at)::float8 FROM previous",
    )
    .bind(BEAT)
    .fetch_optional(&ctx.db)
    .await?;
    let outage = ctx.settings.sweep_interval.as_secs_f64() * 3.0;
    if gap.is_none_or(|gap| gap > outage) {
        // takt was not serving for a while (or never swept before): nobody was watching for
        // the windows either, so their absence starts now.
        sqlx::query(
            "UPDATE facade_callersession s SET last_seen = now()
               FROM facade_task t
              WHERE t.deadman_session_id = s.id AND t.deadman_seconds IS NOT NULL AND NOT t.is_done",
        )
        .execute(&ctx.db)
        .await?;
    }

    let due: Vec<(i64, i32, String)> = sqlx::query_as(
        "SELECT t.id, t.deadman_seconds, s.id
           FROM facade_task t
           JOIN facade_callersession s ON s.id = t.deadman_session_id
          WHERE t.deadman_seconds IS NOT NULL AND NOT t.is_done
            AND t.latest_event_kind <> 'HELD'
            AND s.last_seen < now() - make_interval(secs => t.deadman_seconds)
          LIMIT $1",
    )
    .bind(limit)
    .fetch_all(&ctx.db)
    .await?;

    let mut cancelled = 0;
    for (task, seconds, session) in due {
        // Claimed once: disarmed in the same statement, so a second reaper finds nothing.
        let claimed = sqlx::query(
            "UPDATE facade_task SET deadman_seconds = NULL, deadman_session_id = NULL
              WHERE id = $1 AND deadman_session_id = $2 AND NOT is_done",
        )
        .bind(task)
        .bind(&session)
        .execute(&ctx.db)
        .await?;
        if claimed.rows_affected() == 0 {
            continue;
        }
        // Cancelled first, said after: a task that ended meanwhile gets no word of a cancel.
        match backend::request_control(ctx, &task.to_string(), Control::Cancel, None).await {
            Ok(_) => cancelled += 1,
            Err(e) => {
                tracing::info!(task, "the dead man cancel was not taken: {e}");
                continue;
            }
        }
        let why = NewEvent {
            level: Some("INFO".into()),
            ..NewEvent::default()
        }
        .with_message(Some(format!(
            "Cancelled: the window it was tied to (dead man switch) was gone for over {seconds} s."
        )));
        match transitions::insert_event(&ctx.db, task, "LOG", &why).await {
            Ok(event) => crate::signals::task_event_created(ctx, event).await,
            Err(e) => tracing::warn!(task, "recording the dead man cancel failed: {e}"),
        }
    }
    Ok(cancelled)
}

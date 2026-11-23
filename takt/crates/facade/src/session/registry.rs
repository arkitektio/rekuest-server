//! Where a caller session is kept between sockets (docs/session-protocol.md §2, §5.4).
//!
//! Redis holds what a window may take back within `session_grace`: what it watched and how far
//! it had each topic as of the newest `pos` it reported, the answers it may not have received,
//! and the probes it owns. Any replica can resume it. Postgres holds the one thing that must
//! outlive redis: the session's row (`CallerSession`), whose `last_seen` the dead man switch
//! reads, so a redis restart cancels nothing.

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use redis::AsyncCommands;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::context::Context;
use crate::redis_keys;
use crate::session::frames::Answer;
use crate::session::topics::Cursor;

/// A session as redis keeps it.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Saved {
    pub caller: i64,
    /// The connection serving it; a grace watcher only ends a session its own connection left.
    pub connection: String,
    /// When its socket went; `None` while one serves it.
    pub left_at: Option<DateTime<Utc>>,
    /// The `pos` its next push gets.
    pub next_pos: u64,
    /// The newest `pos` the window said it received.
    pub acked_pos: u64,
    /// Every watched topic, with its cursor as of `acked_pos` (`None`: its snapshot was not
    /// acknowledged yet, so a resume sends it again).
    pub watches: BTreeMap<String, Option<Cursor>>,
    /// The answers pushed after `acked_pos`, with their `pos`.
    pub answers: Vec<(u64, Answer)>,
    /// The probes it started: they end with it.
    pub probes: Vec<String>,
}

fn key(ctx: &Context, session: &str) -> String {
    redis_keys::key(&ctx.settings, &[&"session", &session])
}

pub async fn load(ctx: &Context, session: &str) -> Option<Saved> {
    let mut redis = ctx.redis.clone();
    let text: Option<String> = redis.get(key(ctx, session)).await.ok()?;
    serde_json::from_str(&text?).ok()
}

/// Keep `saved` as long as `saved.connection` still holds the session; whether it does. A
/// connection another one took the session over from (a window back before this one was seen
/// to be gone) learns so here, and must stop.
pub async fn save_held(ctx: &Context, session: &str, saved: &Saved) -> bool {
    let Ok(text) = serde_json::to_string(saved) else {
        return true;
    };
    let seconds = ctx.settings.session.grace.as_secs() + 60;
    let script = redis::Script::new(
        r"local held = redis.call('GET', KEYS[1])
          if held then
            local ok, record = pcall(cjson.decode, held)
            if ok and record.connection ~= ARGV[1] then return 0 end
          end
          redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
          return 1",
    );
    let mut redis = ctx.redis.clone();
    match script
        .key(key(ctx, session))
        .arg(&saved.connection)
        .arg(text)
        .arg(seconds)
        .invoke_async::<i64>(&mut redis)
        .await
    {
        Ok(held) => held == 1,
        Err(e) => {
            tracing::warn!(session, "keeping the session failed: {e}");
            true
        }
    }
}

/// Keep `saved` for a grace (and a little more, so a grace watcher still finds it), whoever
/// held the session before: what a connection that opens or takes over a session writes first.
pub async fn save(ctx: &Context, session: &str, saved: &Saved) {
    let Ok(text) = serde_json::to_string(saved) else {
        return;
    };
    let seconds = ctx.settings.session.grace.as_secs() + 60;
    let mut redis = ctx.redis.clone();
    if let Err(e) = redis
        .set_ex::<_, _, ()>(key(ctx, session), text, seconds)
        .await
    {
        tracing::warn!(session, "keeping the session failed: {e}");
    }
}

pub async fn forget(ctx: &Context, session: &str) {
    let mut redis = ctx.redis.clone();
    let _: Result<(), _> = redis.del(key(ctx, session)).await;
}

/// The session's row: made for a new session, marked live again for a resumed one.
pub async fn open_row(
    ctx: &Context,
    session: &str,
    caller: i64,
    client_info: &Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO facade_callersession (id, caller_id, client_info, created_at, last_seen)
         VALUES ($1, $2, $3, now(), now())
         ON CONFLICT (id) DO UPDATE SET last_seen = now(), ended_at = NULL",
    )
    .bind(session)
    .bind(caller)
    .bind(sqlx::types::Json(client_info))
    .execute(&ctx.db)
    .await?;
    Ok(())
}

/// The window was heard from: what the dead man switch reads.
pub async fn beat(ctx: &Context, session: &str) {
    if let Err(e) = sqlx::query("UPDATE facade_callersession SET last_seen = now() WHERE id = $1")
        .bind(session)
        .execute(&ctx.db)
        .await
    {
        tracing::warn!(session, "writing the session's last_seen failed: {e}");
    }
}

/// The session is over (its grace ran out).
pub async fn end_row(ctx: &Context, session: &str) {
    if let Err(e) = sqlx::query(
        "UPDATE facade_callersession SET ended_at = now() WHERE id = $1 AND ended_at IS NULL",
    )
    .bind(session)
    .execute(&ctx.db)
    .await
    {
        tracing::warn!(session, "ending the session's row failed: {e}");
    }
}

/// A window that came back after its session's grace (it still names the old one): the runs
/// that old session held by their dead man switch move to the new one, so a switch longer than
/// the grace does not fire on a window that returned. Only the same caller's. How many moved.
pub async fn hand_over_deadman(
    ctx: &Context,
    old: &str,
    new: &str,
    caller: i64,
) -> Result<u64, sqlx::Error> {
    let moved = sqlx::query(
        "UPDATE facade_task t SET deadman_session_id = $2
           FROM facade_callersession s
          WHERE t.deadman_session_id = $1 AND s.id = $1 AND s.caller_id = $3 AND NOT t.is_done",
    )
    .bind(old)
    .bind(new)
    .bind(caller)
    .execute(&ctx.db)
    .await?;
    Ok(moved.rows_affected())
}

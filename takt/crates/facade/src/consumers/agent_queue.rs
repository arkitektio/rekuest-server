//! What the server has for a socket agent: its commands, kept until the agent acknowledges them.
//!
//! **Commands live in the database** (`facade_command`), beside what they are about: a command
//! is written in the transaction that makes it true (a child's event with the event, the answer
//! to a request with the request's position), so there is no moment in which the task says one
//! thing and the queue another, and nothing to make up for when they disagree.
//!
//! - A row is written without a position. The connection holding the agent's lease **numbers**
//!   what is waiting ([`number`]) and sends it at that position; `facade_commandcursor` keeps the
//!   last position given out. Numbering at the sender, not at the writer, is what makes positions
//!   safe to count by: a row that commits late is numbered late, never below a position the
//!   agent already handled.
//! - Nothing is removed by sending. A `COMMAND_ACK {through}` deletes what it covers and records
//!   the pickup of the assigns among it, in one statement ([`ack`]). A connection made anew starts
//!   after the `commands_seen` its `REGISTER` named. At-least-once on the wire, exactly-once for
//!   the agent, which skips a position it has handled.
//! - The mirror of a task event is a row that names the event (`kind = 'EVENT'`): the frame is
//!   built from the event when it is sent, so the event is stored once.
//!
//! **Redis carries what is worth nothing later**, per agent:
//!
//! - `{prefix}:agent:{id}:priority`: a probe's frames, a question (`INQUIRE`), an order to a
//!   connection (`BOUNCE`, `KICK`). No position, sent before any command, in the order they were
//!   pushed, forgotten once sent (at most once).
//! - `{prefix}:agent:{id}:wake`: one entry, written anew whenever a command was committed. A
//!   connection waits on both streams; a wake that got lost costs one [`READ_BLOCK_MILLIS`].

use redis::aio::{ConnectionManager, MultiplexedConnection};
use redis::streams::{StreamReadOptions, StreamReadReply};
use redis::AsyncCommands;
use serde_json::Value;
use sqlx::PgPool;

use crate::caller_events::{build_execution_event, EventLike};
use crate::context::Context;
use crate::redis_keys;
use crate::settings::Settings;

/// How long a connection waits for a wake before it looks for commands anyway.
pub const READ_BLOCK_MILLIS: usize = 5000;
/// How many commands one read returns at most.
pub const READ_COUNT: i64 = 100;
/// The field of a stream entry holding the serialized frame.
const FRAME: &str = "f";
/// The kind of a row that mirrors a task event.
pub const EVENT: &str = "EVENT";

pub fn priority_key(settings: &Settings, agent: i64) -> String {
    redis_keys::key(settings, &[&"agent", &agent, &"priority"])
}

pub fn wake_key(settings: &Settings, agent: i64) -> String {
    redis_keys::key(settings, &[&"agent", &agent, &"wake"])
}

/// Every redis key an agent has here.
pub fn keys(settings: &Settings, agent: i64) -> [String; 2] {
    [priority_key(settings, agent), wake_key(settings, agent)]
}

/// A command to keep for an agent: its frame as it is sent (without a position), and what it
/// is about, said once here so nobody has to read it out of the frame again.
#[derive(Debug, Clone, PartialEq)]
pub struct NewCommand {
    pub kind: String,
    pub task: Option<i64>,
    pub frame: String,
}

impl NewCommand {
    /// Read `kind` and `task` off a serialized frame.
    pub fn of(frame: String) -> Self {
        let raw: Value = serde_json::from_str(&frame).unwrap_or(Value::Null);
        let kind = raw
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        let task = match raw.get("task") {
            Some(Value::String(task)) => task.parse().ok(),
            Some(Value::Number(task)) => task.as_i64(),
            _ => None,
        };
        Self { kind, task, frame }
    }
}

/// Keep a command for an agent. Give it the transaction that makes the command true where
/// there is one; [`wake`] the agent's connection once that is committed.
pub async fn push(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
    command: &NewCommand,
) -> Result<(), sqlx::Error> {
    sqlx::query("INSERT INTO facade_command (agent_id, kind, task, frame) VALUES ($1, $2, $3, $4)")
        .bind(agent)
        .bind(&command.kind)
        .bind(command.task)
        .bind(&command.frame)
        .execute(executor)
        .await?;
    Ok(())
}

/// Keep the mirror of a task event for the agent that asked for the task.
pub async fn push_event(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
    task: i64,
    event: i64,
) -> Result<(), sqlx::Error> {
    sqlx::query("INSERT INTO facade_command (agent_id, kind, task, event) VALUES ($1, $2, $3, $4)")
        .bind(agent)
        .bind(EVENT)
        .bind(task)
        .bind(event)
        .execute(executor)
        .await?;
    Ok(())
}

/// Keep every event a task has so far for an agent, in order: for a caller that found a child
/// again, or that asks about one that ended without it hearing. An event already waiting for
/// that agent is not kept twice.
pub async fn push_events_of(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
    task: i64,
) -> Result<u64, sqlx::Error> {
    Ok(sqlx::query(
        "INSERT INTO facade_command (agent_id, kind, task, event)
         SELECT $1, 'EVENT', e.task_id, e.id
           FROM facade_taskevent e
          WHERE e.task_id = $2 AND e.kind = ANY($3)
            AND NOT EXISTS (SELECT 1 FROM facade_command c
                             WHERE c.task = e.task_id AND c.kind = 'EVENT'
                               AND c.agent_id = $1 AND c.event = e.id)
          ORDER BY e.id",
    )
    .bind(agent)
    .bind(task)
    .bind(crate::caller_events::MIRRORED_KINDS)
    .execute(executor)
    .await?
    .rows_affected())
}

/// Tell the agent's connection that a command was committed. Best effort: a connection looks
/// by itself every [`READ_BLOCK_MILLIS`].
pub async fn wake(ctx: &Context, agent: i64) {
    let mut redis = ctx.redis.clone();
    let woken: redis::RedisResult<String> = redis::cmd("XADD")
        .arg(wake_key(&ctx.settings, agent))
        .arg("MAXLEN")
        .arg(1)
        .arg("*")
        .arg("w")
        .arg(1)
        .query_async(&mut redis)
        .await;
    if let Err(e) = woken {
        tracing::debug!(agent, "waking the agent's connection failed: {e}");
    }
}

/// Queue a frame on the lane that jumps the backlog and keeps nothing: a probe's frame, a
/// question, an order to a connection.
pub async fn push_priority(
    redis: &mut ConnectionManager,
    settings: &Settings,
    agent: i64,
    frame: &str,
) -> redis::RedisResult<()> {
    redis
        .xadd(priority_key(settings, agent), "*", &[(FRAME, frame)])
        .await
}

/// The agent has handled every command through `seen` (its `REGISTER`): numbering goes on after
/// it, whatever this database remembers, and what it has handled is acknowledged.
///
/// A database that counted less than the agent (it was restored from an earlier state) holds
/// commands at positions the agent would take for handled: they lose their position and are
/// numbered anew, behind what the agent has seen.
pub async fn resume(db: &PgPool, agent: i64, seen: u64) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO facade_commandcursor (agent_id, numbered) VALUES ($1, 0)
         ON CONFLICT (agent_id) DO NOTHING",
    )
    .bind(agent)
    .execute(db)
    .await?;
    let renumbered = sqlx::query(
        "WITH behind AS (
            UPDATE facade_commandcursor SET numbered = $2
             WHERE agent_id = $1 AND numbered < $2 RETURNING 1
         )
         UPDATE facade_command SET pos = NULL
          WHERE agent_id = $1 AND pos IS NOT NULL AND EXISTS (SELECT 1 FROM behind)",
    )
    .bind(agent)
    .bind(seen as i64)
    .execute(db)
    .await?
    .rows_affected();
    if renumbered > 0 {
        tracing::warn!(
            agent,
            seen,
            renumbered,
            "the agent has seen more commands than this database numbered: what waits is numbered anew"
        );
    }
    if seen > 0 {
        ack(db, agent, seen).await?;
    }
    Ok(())
}

/// The order waiting commands are numbered in: the order they were written, with one
/// exception. The answer to a request for a child (`ASSIGN_RESPONSE`) is written after the
/// child's first events, which exist as soon as the child does: it takes the place of the first
/// of them that still waits, so whoever asked hears which task it is before what the task did.
const WRITTEN: &str = "CASE WHEN c.kind = 'ASSIGN_RESPONSE'
              THEN COALESCE((SELECT min(e.id) FROM facade_command e
                              WHERE e.agent_id = c.agent_id AND e.pos IS NULL AND e.kind = 'EVENT'
                                AND e.task = c.task AND e.id < c.id), c.id)
              ELSE c.id END, (c.kind <> 'ASSIGN_RESPONSE'), c.id";

/// Give every waiting command of an agent its position, in the order they were written (see
/// [`WRITTEN`]). Only the connection holding the lease numbers. Returns how many were numbered.
///
/// A row another connection numbered meanwhile keeps the position it has (`pos IS NULL` is
/// checked again on the row as it is then); the positions counted for it stay unused.
pub async fn number(db: &PgPool, agent: i64) -> Result<u64, sqlx::Error> {
    Ok(sqlx::query(&format!(
        "WITH pending AS (
            SELECT c.id, row_number() OVER (ORDER BY {WRITTEN}) AS n
              FROM facade_command c WHERE c.agent_id = $1 AND c.pos IS NULL
         ), bumped AS (
            UPDATE facade_commandcursor
               SET numbered = numbered + (SELECT count(*) FROM pending)
             WHERE agent_id = $1 AND EXISTS (SELECT 1 FROM pending)
            RETURNING numbered
         )
         UPDATE facade_command c
            SET pos = bumped.numbered - (SELECT count(*) FROM pending) + pending.n
           FROM pending, bumped
          WHERE c.id = pending.id AND c.pos IS NULL",
    ))
    .bind(agent)
    .execute(db)
    .await?
    .rows_affected())
}

/// A numbered command, as a connection reads it.
#[derive(Debug, Clone, PartialEq)]
pub struct Stored {
    pub pos: u64,
    /// Whether it mirrors an event of a task the agent assigned.
    pub is_event: bool,
    /// The frame to send, without its position. `None` when there is nothing to send at this
    /// position: an `ASSIGN` whose task has ended meanwhile, an event that is gone or that no
    /// caller is told of. The position is passed over, and acknowledged with the rest.
    pub frame: Option<String>,
}

#[derive(sqlx::FromRow)]
struct StoredRow {
    pos: i64,
    kind: String,
    frame: Option<String>,
    stale: Option<bool>,
    event_id: Option<i64>,
    event_task: Option<i64>,
    event_kind: Option<String>,
    event_message: Option<String>,
    event_progress: Option<i32>,
    event_returns: Option<Value>,
    event_level: Option<String>,
    event_value: Option<Value>,
}

const STORED: &str = "c.pos, c.kind, c.frame,
       (c.kind = 'ASSIGN' AND t.is_done IS TRUE) AS stale,
       e.id AS event_id, e.task_id AS event_task, e.kind AS event_kind,
       e.message AS event_message, e.progress AS event_progress, e.returns AS event_returns,
       e.level AS event_level, e.value AS event_value
  FROM facade_command c
  LEFT JOIN facade_task t ON t.id = c.task AND c.kind = 'ASSIGN'
  LEFT JOIN facade_taskevent e ON e.id = c.event";

impl StoredRow {
    fn frame(self) -> Option<String> {
        if self.kind != EVENT {
            return self.frame.filter(|_| self.stale != Some(true));
        }
        let mirror = build_execution_event(&EventLike {
            id: self.event_id? as u64,
            task: self.event_task?.to_string(),
            kind: self.event_kind?,
            message: self.event_message,
            progress: self.event_progress.map(i64::from),
            returns: self.event_returns,
            level: self.event_level,
            value: self.event_value,
        })?;
        Some(crate::transport::frame_text(mirror))
    }
}

/// The numbered commands of an agent after position `after`, in order, [`READ_COUNT`] at most.
pub async fn read(db: &PgPool, agent: i64, after: u64) -> Result<Vec<Stored>, sqlx::Error> {
    let rows: Vec<StoredRow> = sqlx::query_as(&format!(
        "SELECT {STORED} WHERE c.agent_id = $1 AND c.pos > $2 ORDER BY c.pos LIMIT $3"
    ))
    .bind(agent)
    .bind(after as i64)
    .bind(READ_COUNT)
    .fetch_all(db)
    .await?;
    Ok(rows
        .into_iter()
        .map(|row| Stored {
            pos: row.pos.max(0) as u64,
            is_event: row.kind == EVENT,
            frame: row.frame(),
        })
        .collect())
}

/// The agent handled every command through `through` (`COMMAND_ACK`): they are forgotten, and
/// the assigns among them are picked up, as one statement. From here on such a task is the
/// agent's, and an assign that is neither picked up nor kept here never was written.
pub async fn ack(db: &PgPool, agent: i64, through: u64) -> Result<(), sqlx::Error> {
    sqlx::query(
        "WITH gone AS (
            DELETE FROM facade_command WHERE agent_id = $1 AND pos <= $2 RETURNING kind, task
         )
         UPDATE facade_task t SET picked_up_at = now()
           FROM gone
          WHERE gone.kind = 'ASSIGN' AND t.id = gone.task AND t.agent_id = $1
            AND t.picked_up_at IS NULL",
    )
    .bind(agent)
    .bind(through as i64)
    .execute(db)
    .await?;
    Ok(())
}

/// Take a task's `ASSIGN` back: the task is about to be assigned anew, and the assign it was
/// first given must not reach a process that never saw it (it would start the task from
/// nothing, ahead of the assign that says how to go on). Returns how many were taken.
pub async fn withdraw_assigns(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
    task: i64,
) -> Result<u64, sqlx::Error> {
    Ok(sqlx::query(
        "DELETE FROM facade_command WHERE agent_id = $1 AND task = $2 AND kind = 'ASSIGN'",
    )
    .bind(agent)
    .bind(task)
    .execute(executor)
    .await?
    .rows_affected())
}

/// A new process took the agent over. What its predecessor was owed and only it could use goes:
/// the answers to its requests, the events of the tasks it assigned, what steered the tasks it
/// had picked up. What is kept is the work nobody has picked up yet, and what steers that.
/// Returns how many commands went.
pub async fn trim_for_new_process(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
) -> Result<u64, sqlx::Error> {
    Ok(sqlx::query(
        "DELETE FROM facade_command c
          WHERE c.agent_id = $1 AND NOT (
                c.kind IN ('ASSIGN', 'CANCEL', 'INTERRUPT', 'PAUSE', 'RESUME', 'DECIDE', 'BLOK_REFUSED')
                AND EXISTS (SELECT 1 FROM facade_task t
                             WHERE t.id = c.task AND t.agent_id = $1
                               AND t.is_done = false AND t.picked_up_at IS NULL))",
    )
    .bind(agent)
    .execute(executor)
    .await?
    .rows_affected())
}

/// Forget everything kept for an agent: it is gone, or no socket will ever read it again.
pub async fn drop_commands(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
) -> Result<u64, sqlx::Error> {
    Ok(
        sqlx::query("DELETE FROM facade_command WHERE agent_id = $1")
            .bind(agent)
            .execute(executor)
            .await?
            .rows_affected(),
    )
}

/// The commands an agent has not acknowledged, in the order it is sent them, as they are sent
/// (without their position). For whoever needs to look: the tests, and a person asking what an
/// agent is owed.
pub async fn pending(db: &PgPool, agent: i64) -> Result<Vec<String>, sqlx::Error> {
    let rows: Vec<StoredRow> = sqlx::query_as(&format!(
        "SELECT COALESCE(c.pos, 0) AS pos, c.kind, c.frame,
                (c.kind = 'ASSIGN' AND t.is_done IS TRUE) AS stale,
                e.id AS event_id, e.task_id AS event_task, e.kind AS event_kind,
                e.message AS event_message, e.progress AS event_progress,
                e.returns AS event_returns, e.level AS event_level, e.value AS event_value
           FROM facade_command c
           LEFT JOIN facade_task t ON t.id = c.task AND c.kind = 'ASSIGN'
           LEFT JOIN facade_taskevent e ON e.id = c.event
          WHERE c.agent_id = $1
          ORDER BY c.pos NULLS LAST, {WRITTEN}"
    ))
    .bind(agent)
    .fetch_all(db)
    .await?;
    Ok(rows.into_iter().filter_map(StoredRow::frame).collect())
}

/// Everything waiting for an agent, as it would be sent: what is on the priority lane, then the
/// commands it has not acknowledged (without positions).
pub async fn waiting(ctx: &Context, agent: i64) -> anyhow::Result<Vec<String>> {
    let mut redis = ctx.redis.clone();
    let urgent: redis::streams::StreamRangeReply =
        redis.xrange_all(priority_key(&ctx.settings, agent)).await?;
    let mut frames: Vec<String> = urgent
        .ids
        .into_iter()
        .filter_map(|entry| entry.get::<String>(FRAME))
        .collect();
    frames.extend(pending(&ctx.db, agent).await?);
    Ok(frames)
}

/// When an entry of the priority stream was pushed, in redis's milliseconds (its id is
/// `{millis}-{n}`).
pub fn pushed_at(id: &str) -> Option<u64> {
    id.split('-').next()?.parse().ok()
}

/// Whether a frame orders a connection to leave (`BOUNCE`, `KICK`): meant for the connection
/// there was when it was given, never for one made later.
pub fn is_connection_order(frame: &str) -> bool {
    serde_json::from_str::<Value>(frame)
        .ok()
        .and_then(|raw| {
            raw.get("type")
                .and_then(Value::as_str)
                .map(|kind| matches!(kind, "BOUNCE" | "KICK"))
        })
        .unwrap_or(false)
}

/// What is urgent for one connection, and its wake, on a redis connection of its own (a read
/// blocks).
pub struct Lane {
    connection: MultiplexedConnection,
    priority: String,
    wake: String,
}

/// A frame off the priority lane, with the id to forget it by.
#[derive(Debug, Clone, PartialEq)]
pub struct Urgent {
    pub id: String,
    pub frame: String,
}

impl Lane {
    pub async fn open(
        client: &redis::Client,
        settings: &Settings,
        agent: i64,
    ) -> redis::RedisResult<Self> {
        let config = redis::AsyncConnectionConfig::new().set_response_timeout(
            std::time::Duration::from_millis(READ_BLOCK_MILLIS as u64 + 5000),
        );
        Ok(Self {
            connection: client
                .get_multiplexed_async_connection_with_config(&config)
                .await?,
            priority: priority_key(settings, agent),
            wake: wake_key(settings, agent),
        })
    }

    /// Redis's clock, in milliseconds: what the ids of the priority stream count in.
    pub async fn now_millis(&mut self) -> redis::RedisResult<u64> {
        let (seconds, micros): (u64, u64) =
            redis::cmd("TIME").query_async(&mut self.connection).await?;
        Ok(seconds * 1000 + micros / 1000)
    }

    /// What is on the priority lane after `urgent_after` (`0` for all), and the id of the wake
    /// if there is one after `woken_after`. With `block`, waits until either is there or
    /// [`READ_BLOCK_MILLIS`] pass.
    pub async fn read(
        &mut self,
        urgent_after: &str,
        woken_after: &str,
        block: bool,
    ) -> redis::RedisResult<(Vec<Urgent>, Option<String>)> {
        let mut options = StreamReadOptions::default().count(READ_COUNT as usize);
        if block {
            options = options.block(READ_BLOCK_MILLIS);
        }
        let reply: Option<StreamReadReply> = self
            .connection
            .xread_options(
                &[&self.priority, &self.wake],
                &[urgent_after, woken_after],
                &options,
            )
            .await?;
        let mut urgent = vec![];
        let mut woken = None;
        for stream in reply.map(|reply| reply.keys).unwrap_or_default() {
            let is_wake = stream.key == self.wake;
            for entry in stream.ids {
                if is_wake {
                    woken = Some(entry.id);
                } else if let Some(frame) = entry.get::<String>(FRAME) {
                    urgent.push(Urgent {
                        id: entry.id,
                        frame,
                    });
                }
            }
        }
        Ok((urgent, woken))
    }

    /// An urgent frame was sent (or is not worth sending): forget it.
    pub async fn forget(&mut self, id: &str) -> redis::RedisResult<()> {
        self.connection.xdel(&self.priority, &[id]).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_command_says_what_it_is_about() {
        let assign = NewCommand::of(r#"{"type":"ASSIGN","task":"7","args":{}}"#.to_owned());
        assert_eq!((assign.kind.as_str(), assign.task), ("ASSIGN", Some(7)));
        let collect = NewCommand::of(r#"{"type":"COLLECT","drawers":[]}"#.to_owned());
        assert_eq!((collect.kind.as_str(), collect.task), ("COLLECT", None));
    }

    #[test]
    fn an_order_to_leave_is_told_from_a_command() {
        assert!(is_connection_order(r#"{"type":"BOUNCE","duration":null}"#));
        assert!(is_connection_order(r#"{"type":"KICK","reason":"gone"}"#));
        assert!(!is_connection_order(r#"{"type":"ASSIGN","task":"1"}"#));
        assert!(!is_connection_order("nonsense"));
    }

    #[test]
    fn a_priority_entry_says_when_it_was_pushed() {
        assert_eq!(pushed_at("1731600000000-3"), Some(1731600000000));
        assert_eq!(pushed_at("nonsense"), None);
    }
}

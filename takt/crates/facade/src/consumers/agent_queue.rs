//! What the server has for a socket agent: its commands, kept until the agent acknowledges them.
//!
//! Redis rather than the channel layer, on purpose: a command pushed while the agent is away
//! persists and survives the reconnect. Two streams per agent:
//!
//! - `{prefix}:agent:{id}:commands`: the agent's **commands**, each at its position `pos` (1, 2,
//!   3, … per agent, no gaps; the entry's id is `{pos}-0`). The connection holding the lease reads
//!   from the position after the last one it sent, stamps `pos` on the frame and sends it. Nothing
//!   is removed by sending: a `COMMAND_ACK {through}` from the agent trims the stream, and a
//!   connection made anew starts after the `commands_seen` its `REGISTER` named. At-least-once on
//!   the wire, exactly-once for the agent, which skips a position it has handled.
//! - `{prefix}:agent:{id}:priority`: a **probe's** frames. A probe is ephemeral and jumps the
//!   backlog: its frames carry no position, are sent before any command, in the order they were
//!   pushed, and are forgotten once sent (at most once).
//!
//! `{prefix}:agent:{id}:command_pos` is the last position given out.

use redis::aio::{ConnectionManager, MultiplexedConnection};
use redis::streams::{StreamReadOptions, StreamReadReply};
use redis::AsyncCommands;

use crate::redis_keys;
use crate::settings::Settings;

/// How long a read blocks before it returns nothing (and the drain loop checks in).
pub const READ_BLOCK_MILLIS: usize = 5000;
/// How many entries one read returns at most.
const READ_COUNT: usize = 100;
/// The field of a stream entry holding the serialized frame.
const FRAME: &str = "f";

pub fn commands_key(settings: &Settings, agent: i64) -> String {
    redis_keys::key(settings, &[&"agent", &agent, &"commands"])
}

pub fn position_key(settings: &Settings, agent: i64) -> String {
    redis_keys::key(settings, &[&"agent", &agent, &"command_pos"])
}

pub fn priority_key(settings: &Settings, agent: i64) -> String {
    redis_keys::key(settings, &[&"agent", &agent, &"priority"])
}

/// Every key an agent has here: what goes when the agent goes.
pub fn keys(settings: &Settings, agent: i64) -> [String; 3] {
    [
        commands_key(settings, agent),
        position_key(settings, agent),
        priority_key(settings, agent),
    ]
}

/// Take the next position and append the frame at it, as one step: positions are given out in
/// the order the frames are appended.
const PUSH: &str = "local pos = redis.call('INCR', KEYS[2]) \
                    redis.call('XADD', KEYS[1], pos .. '-0', ARGV[1], ARGV[2]) \
                    return pos";

/// Never number a command at or below what the agent has seen: it would be skipped as handled.
/// (A redis that lost its data starts counting at 1 while a living agent remembers more.)
const RESUME: &str = "local at = tonumber(redis.call('GET', KEYS[1]) or '0') \
                      local seen = tonumber(ARGV[1]) \
                      if at < seen then redis.call('SET', KEYS[1], seen) return seen end \
                      return at";

/// One thing read off an agent's streams.
#[derive(Debug, Clone, PartialEq)]
pub enum Queued {
    /// A command at its position.
    Command { pos: u64, frame: String },
    /// A probe's frame, with the id to forget it by.
    Probe { id: String, frame: String },
}

/// The position of a command entry's id (`{pos}-0`).
fn position_of(id: &str) -> Option<u64> {
    id.split('-').next()?.parse().ok()
}

/// One connection's view of its agent's streams, on a redis connection of its own (a read
/// blocks).
pub struct AgentQueue {
    connection: MultiplexedConnection,
    commands: String,
    position: String,
    priority: String,
}

impl AgentQueue {
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
            commands: commands_key(settings, agent),
            position: position_key(settings, agent),
            priority: priority_key(settings, agent),
        })
    }

    /// The agent has handled every command through `seen`: numbering goes on after it, whatever
    /// redis remembers. Returns the last position given out.
    pub async fn resume(&mut self, seen: u64) -> redis::RedisResult<u64> {
        redis::Script::new(RESUME)
            .key(&self.position)
            .arg(seen)
            .invoke_async(&mut self.connection)
            .await
    }

    /// What is there after `sent` (commands) and `probes_after` (a probe id, `0` for all), probes
    /// first. Blocks until something is, or `READ_BLOCK_MILLIS` pass.
    pub async fn read(&mut self, sent: u64, probes_after: &str) -> redis::RedisResult<Vec<Queued>> {
        let options = StreamReadOptions::default()
            .count(READ_COUNT)
            .block(READ_BLOCK_MILLIS);
        let reply: Option<StreamReadReply> = self
            .connection
            .xread_options(
                &[&self.priority, &self.commands],
                &[probes_after.to_owned(), format!("{sent}-0")],
                &options,
            )
            .await?;
        let mut probes = vec![];
        let mut commands = vec![];
        for stream in reply.map(|reply| reply.keys).unwrap_or_default() {
            let urgent = stream.key == self.priority;
            for entry in stream.ids {
                let Some(frame) = entry.get::<String>(FRAME) else {
                    continue;
                };
                if urgent {
                    probes.push(Queued::Probe {
                        id: entry.id,
                        frame,
                    });
                } else if let Some(pos) = position_of(&entry.id) {
                    commands.push(Queued::Command { pos, frame });
                }
            }
        }
        probes.append(&mut commands);
        Ok(probes)
    }

    /// A probe's frame was sent (or is not worth sending): forget it.
    pub async fn forget_probe(&mut self, id: &str) -> redis::RedisResult<()> {
        self.connection.xdel(&self.priority, &[id]).await
    }
}

/// Queue a serialized frame for an agent: a command at the next position, or (`priority`) a
/// probe's frame on the lane of its own.
pub async fn push(
    redis: &mut ConnectionManager,
    settings: &Settings,
    agent: i64,
    frame: &str,
    priority: bool,
) -> redis::RedisResult<()> {
    if priority {
        return redis
            .xadd(priority_key(settings, agent), "*", &[(FRAME, frame)])
            .await;
    }
    redis::Script::new(PUSH)
        .key(commands_key(settings, agent))
        .key(position_key(settings, agent))
        .arg(FRAME)
        .arg(frame)
        .invoke_async::<u64>(redis)
        .await
        .map(|_| ())
}

/// The agent handled every command through `through` (`COMMAND_ACK`): forget them. Returns
/// what was forgotten, oldest first, for whoever acts on a command having arrived.
pub async fn ack(
    redis: &mut ConnectionManager,
    settings: &Settings,
    agent: i64,
    through: u64,
) -> redis::RedisResult<Vec<String>> {
    let key = commands_key(settings, agent);
    let covered: redis::streams::StreamRangeReply =
        redis.xrange(&key, "-", format!("{through}-0")).await?;
    redis::cmd("XTRIM")
        .arg(&key)
        .arg("MINID")
        .arg(format!("{}-0", through.saturating_add(1)))
        .query_async::<u64>(redis)
        .await?;
    Ok(covered
        .ids
        .into_iter()
        .filter_map(|entry| entry.get::<String>(FRAME))
        .collect())
}

/// The commands an agent has not acknowledged, oldest first, as they were pushed (without their
/// position). For whoever needs to look: the tests, and a person asking what an agent is owed.
pub async fn pending(
    redis: &mut ConnectionManager,
    settings: &Settings,
    agent: i64,
) -> redis::RedisResult<Vec<String>> {
    let reply: redis::streams::StreamRangeReply =
        redis.xrange_all(commands_key(settings, agent)).await?;
    Ok(reply
        .ids
        .into_iter()
        .filter_map(|entry| entry.get::<String>(FRAME))
        .collect())
}

/// Everything waiting for an agent, as it would be sent: a probe's frames, then the commands it
/// has not acknowledged, each oldest first (without positions).
pub async fn waiting(
    redis: &mut ConnectionManager,
    settings: &Settings,
    agent: i64,
) -> redis::RedisResult<Vec<String>> {
    let probes: redis::streams::StreamRangeReply =
        redis.xrange_all(priority_key(settings, agent)).await?;
    let mut frames: Vec<String> = probes
        .ids
        .into_iter()
        .filter_map(|entry| entry.get::<String>(FRAME))
        .collect();
    frames.extend(pending(redis, settings, agent).await?);
    Ok(frames)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_command_entry_names_its_position() {
        assert_eq!(position_of("7-0"), Some(7));
        assert_eq!(position_of("1731600000000-3"), Some(1731600000000));
        assert_eq!(position_of("nonsense"), None);
    }
}

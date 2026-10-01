//! The per-agent message queue (`facade/consumers/agent_queue.py`).
//!
//! A redis list rather than the channel layer, on purpose: a message pushed while the agent is
//! offline persists and survives the reconnect. Producers `LPUSH` onto `{prefix}:agent:{id}:queue`
//! (priority frames `RPUSH`); the connection holding the lease `BLMOVE`s one frame at a time into
//! `…:processing`, delivers it, then acks it: at-least-once.

use redis::aio::MultiplexedConnection;
use redis::AsyncCommands;

use crate::redis_keys;
use crate::settings::Settings;

/// How long a pop blocks before it returns nothing (and the drain loop checks in).
pub const POP_BLOCK_SECONDS: f64 = 5.0;

pub fn queue_key(settings: &Settings, agent: i64) -> String {
    redis_keys::key(settings, &[&"agent", &agent, &"queue"])
}

pub fn processing_key(settings: &Settings, agent: i64) -> String {
    redis_keys::key(settings, &[&"agent", &agent, &"processing"])
}

/// One connection's view of its agent's queue, on a redis connection of its own.
pub struct AgentQueue {
    connection: MultiplexedConnection,
    queue: String,
    processing: String,
}

impl AgentQueue {
    pub async fn open(
        client: &redis::Client,
        settings: &Settings,
        agent: i64,
    ) -> redis::RedisResult<Self> {
        let config = redis::AsyncConnectionConfig::new()
            .set_response_timeout(std::time::Duration::from_secs_f64(POP_BLOCK_SECONDS + 5.0));
        Ok(Self {
            connection: client
                .get_multiplexed_async_connection_with_config(&config)
                .await?,
            queue: queue_key(settings, agent),
            processing: processing_key(settings, agent),
        })
    }

    /// Block until a frame is queued (or `POP_BLOCK_SECONDS` pass), moving it to in-flight.
    pub async fn pop(&mut self) -> redis::RedisResult<Option<String>> {
        redis::cmd("BLMOVE")
            .arg(&self.queue)
            .arg(&self.processing)
            .arg("RIGHT")
            .arg("LEFT")
            .arg(POP_BLOCK_SECONDS)
            .query_async(&mut self.connection)
            .await
    }

    /// A popped frame was delivered: remove it from in-flight.
    pub async fn ack(&mut self, frame: &str) -> redis::RedisResult<()> {
        self.connection.lrem(&self.processing, 0, frame).await
    }

    /// Return popped-but-never-acked frames to the head of the queue, oldest first. Only the
    /// lease holder does this, before it starts popping. Returns how many.
    pub async fn recover(&mut self) -> redis::RedisResult<u64> {
        let mut recovered = 0;
        loop {
            let moved: Option<String> = redis::cmd("LMOVE")
                .arg(&self.processing)
                .arg(&self.queue)
                .arg("LEFT")
                .arg("RIGHT")
                .query_async(&mut self.connection)
                .await?;
            if moved.is_none() {
                return Ok(recovered);
            }
            recovered += 1;
        }
    }

    /// Hand a popped, undelivered frame back, to be popped next: this connection lost the lease
    /// and the frame belongs to the new holder. Atomic, so a concurrent `recover` never doubles it.
    pub async fn requeue(&mut self, frame: &str) -> redis::RedisResult<()> {
        redis::Script::new(
            "if redis.call('LREM', KEYS[1], 1, ARGV[1]) > 0 then redis.call('RPUSH', KEYS[2], ARGV[1]) return 1 end return 0",
        )
        .key(&self.processing)
        .key(&self.queue)
        .arg(frame)
        .invoke_async::<i64>(&mut self.connection)
        .await
        .map(|_| ())
    }
}

/// Queue a serialized frame for an agent; `priority` jumps the backlog (`push`).
pub async fn push(
    redis: &mut redis::aio::ConnectionManager,
    settings: &Settings,
    agent: i64,
    frame: &str,
    priority: bool,
) -> redis::RedisResult<()> {
    let key = queue_key(settings, agent);
    if priority {
        redis.rpush(key, frame).await
    } else {
        redis.lpush(key, frame).await
    }
}

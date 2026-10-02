//! The channel layer (`channels_redis/core.py`, `RedisChannelLayer`, channels_redis 4.3.0),
//! single host.
//!
//! Wire-compatible with the Python layer, so a Python `group_send` reaches a Rust receiver and
//! the other way round:
//!
//! * a group is the sorted set `{prefix}:group:{group}` of channel names, scored by join time;
//! * a channel's messages live in the sorted set `{prefix}{non_local_name}`, scored by send
//!   time and expired after `expiry` seconds. A process-local channel
//!   (`specific.{client}!{id}`) shares one inbox per process (`{prefix}specific.{client}!`), and
//!   its message names the channel(s) it is for in `__asgi_channel__`;
//! * a receiver moves a popped message to `{inbox}$inflight` until it is dispatched, so a
//!   receiver that dies mid-way leaves it for the next one.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use redis::aio::{ConnectionManager, MultiplexedConnection};
use redis::AsyncCommands;
use serde_json::{Map, Value};
use tokio::sync::mpsc;

use crate::errors::KanteError;
use crate::serializers::MsgPackSerializer;

/// How long one receive blocks before it looks again (`brpop_timeout`).
pub const BRPOP_TIMEOUT: f64 = 5.0;

/// The layer's settings (`RedisChannelLayer.__init__`).
#[derive(Debug, Clone)]
pub struct ChannelLayerConfig {
    pub prefix: String,
    /// Seconds a sent message waits to be received.
    pub expiry: u64,
    /// Seconds a channel stays in a group without re-joining.
    pub group_expiry: u64,
    /// Messages a channel holds before sends to it are dropped.
    pub capacity: u64,
}

impl Default for ChannelLayerConfig {
    fn default() -> Self {
        Self {
            prefix: "asgi".into(),
            expiry: 60,
            group_expiry: 86400,
            capacity: 100,
        }
    }
}

type Inboxes = Arc<Mutex<HashMap<String, mpsc::UnboundedSender<Value>>>>;

/// A channel layer on one redis.
#[derive(Clone)]
pub struct ChannelLayer {
    config: Arc<ChannelLayerConfig>,
    serializer: MsgPackSerializer,
    connection: ConnectionManager,
    client: redis::Client,
    /// This process's part of every process-local channel name.
    client_prefix: String,
    /// Process-local channels with a live receiver, by channel name.
    inboxes: Inboxes,
    receiving: Arc<tokio::sync::OnceCell<()>>,
}

/// The part of a channel name a message is stored under: up to and including the `!` for a
/// process-local channel, the whole name otherwise (`non_local_name`).
pub fn non_local_name(name: &str) -> &str {
    match name.find('!') {
        Some(index) => &name[..=index],
        None => name,
    }
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or_default()
}

const GROUP_SEND_LUA: &str = "
    local over_capacity = 0
    local current_time = ARGV[#ARGV - 1]
    local expiry = ARGV[#ARGV]
    for i=1,#KEYS do
        if redis.call('ZCOUNT', KEYS[i], '-inf', '+inf') < tonumber(ARGV[i + #KEYS]) then
            redis.call('ZADD', KEYS[i], current_time, ARGV[i])
            redis.call('EXPIRE', KEYS[i], expiry)
        else
            over_capacity = over_capacity + 1
        end
    end
    return over_capacity
";

const CLEANUP_LUA: &str = "
    local backed_up = redis.call('ZRANGE', ARGV[2], 0, -1, 'WITHSCORES')
    for i = #backed_up, 1, -2 do
        redis.call('ZADD', ARGV[1], backed_up[i], backed_up[i - 1])
    end
    redis.call('DEL', ARGV[2])
";

impl ChannelLayer {
    pub async fn new(
        client: redis::Client,
        config: ChannelLayerConfig,
    ) -> Result<Self, KanteError> {
        let connection = ConnectionManager::new(client.clone()).await?;
        Ok(Self {
            config: Arc::new(config),
            serializer: MsgPackSerializer::default(),
            connection,
            client,
            client_prefix: uuid::Uuid::new_v4().simple().to_string(),
            inboxes: Arc::default(),
            receiving: Arc::default(),
        })
    }

    pub fn config(&self) -> &ChannelLayerConfig {
        &self.config
    }

    fn group_key(&self, group: &str) -> String {
        format!("{}:group:{group}", self.config.prefix)
    }

    fn channel_key(&self, channel: &str) -> String {
        format!("{}{}", self.config.prefix, non_local_name(channel))
    }

    /// A new process-local channel name (`new_channel`).
    pub fn new_channel(&self, prefix: &str) -> String {
        format!(
            "{prefix}.{}!{}",
            self.client_prefix,
            uuid::Uuid::new_v4().simple()
        )
    }

    /// Add `channel` to `group` (`group_add`).
    pub async fn group_add(&self, group: &str, channel: &str) -> Result<(), KanteError> {
        let mut connection = self.connection.clone();
        let key = self.group_key(group);
        let _: () = connection.zadd(&key, channel, now()).await?;
        let _: () = connection
            .expire(&key, self.config.group_expiry as i64)
            .await?;
        Ok(())
    }

    /// Remove `channel` from `group`, if it is in it (`group_discard`).
    pub async fn group_discard(&self, group: &str, channel: &str) -> Result<(), KanteError> {
        let mut connection = self.connection.clone();
        let _: () = connection.zrem(self.group_key(group), channel).await?;
        Ok(())
    }

    /// Send `message` (a dict with a `type`) to one channel (`send`). Dropped with an error when
    /// the channel is at capacity.
    pub async fn send(&self, channel: &str, message: &Value) -> Result<(), KanteError> {
        let mut message = message.as_object().cloned().ok_or(KanteError::NotADict)?;
        if channel.contains('!') {
            message.insert("__asgi_channel__".into(), Value::String(channel.into()));
        }
        let key = self.channel_key(channel);
        let mut connection = self.connection.clone();
        let cutoff = now() as i64 - self.config.expiry as i64;
        let _: () = connection.zrembyscore(&key, 0, cutoff).await?;
        let count: u64 = connection.zcount(&key, "-inf", "+inf").await?;
        if count >= self.config.capacity {
            return Err(KanteError::ChannelFull(channel.into()));
        }
        let bytes = self.serializer.serialize(&Value::Object(message))?;
        let _: () = connection.zadd(&key, bytes, now()).await?;
        let _: () = connection.expire(&key, self.config.expiry as i64).await?;
        Ok(())
    }

    /// Send `message` to every channel in `group` (`group_send`). A channel at capacity misses
    /// it; that is logged, not an error.
    pub async fn group_send(&self, group: &str, message: &Value) -> Result<(), KanteError> {
        let message = message.as_object().ok_or(KanteError::NotADict)?;
        let mut connection = self.connection.clone();
        let key = self.group_key(group);
        let _: () = connection
            .zrembyscore(&key, 0, now() as i64 - self.config.group_expiry as i64)
            .await?;
        let channels: Vec<String> = connection.zrange(&key, 0, -1).await?;
        if channels.is_empty() {
            return Ok(());
        }

        // One message per redis key, naming every channel of that key it is for.
        let mut keys: Vec<String> = Vec::new();
        let mut per_key: HashMap<String, Vec<String>> = HashMap::new();
        for channel in &channels {
            let channel_key = self.channel_key(channel);
            if !per_key.contains_key(&channel_key) {
                keys.push(channel_key.clone());
            }
            per_key
                .entry(channel_key)
                .or_default()
                .push(channel.clone());
        }

        let cutoff = now() as i64 - self.config.expiry as i64;
        let mut pipe = redis::pipe();
        for channel_key in &keys {
            pipe.zrembyscore(channel_key, 0, cutoff).ignore();
        }
        let _: () = pipe.query_async(&mut connection).await?;

        let lua = redis::Script::new(GROUP_SEND_LUA);
        let mut script = lua.prepare_invoke();
        for channel_key in &keys {
            script.key(channel_key);
        }
        for channel_key in &keys {
            let mut addressed: Map<String, Value> = message.clone();
            addressed.insert(
                "__asgi_channel__".into(),
                Value::Array(
                    per_key[channel_key]
                        .iter()
                        .cloned()
                        .map(Value::String)
                        .collect(),
                ),
            );
            script.arg(self.serializer.serialize(&Value::Object(addressed))?);
        }
        for _ in &keys {
            script.arg(self.config.capacity);
        }
        script.arg(now()).arg(self.config.expiry);
        let over: i64 = script.invoke_async(&mut connection).await?;
        if over > 0 {
            tracing::info!(
                "{over} of {} channels over capacity in group {group}",
                channels.len()
            );
        }
        Ok(())
    }

    /// Pop one message off `channel_key`, keeping it in the in-flight backup until
    /// [`Self::clean_backup`] (`_brpop_with_clean`). `None` after `BRPOP_TIMEOUT`.
    async fn pop_with_clean(
        &self,
        connection: &mut MultiplexedConnection,
        channel_key: &str,
    ) -> Result<Option<Vec<u8>>, KanteError> {
        let backup = format!("{channel_key}$inflight");
        let _: () = redis::Script::new(CLEANUP_LUA)
            .arg(channel_key)
            .arg(&backup)
            .invoke_async(connection)
            .await?;
        let popped: Option<(String, Vec<u8>, f64)> = redis::cmd("BZPOPMIN")
            .arg(channel_key)
            .arg(BRPOP_TIMEOUT)
            .query_async(connection)
            .await?;
        let Some((_, member, score)) = popped else {
            return Ok(None);
        };
        let _: () = connection.zadd(&backup, member.clone(), score).await?;
        Ok(Some(member))
    }

    async fn clean_backup(
        &self,
        connection: &mut MultiplexedConnection,
        channel_key: &str,
    ) -> Result<(), KanteError> {
        let _: () = redis::cmd("ZPOPMIN")
            .arg(format!("{channel_key}$inflight"))
            .query_async(connection)
            .await?;
        Ok(())
    }

    async fn blocking_connection(&self) -> Result<MultiplexedConnection, KanteError> {
        let config = redis::AsyncConnectionConfig::new()
            .set_response_timeout(Duration::from_secs_f64(BRPOP_TIMEOUT + 5.0));
        Ok(self
            .client
            .get_multiplexed_async_connection_with_config(&config)
            .await?)
    }

    /// Receive the next message of a channel that is not process-local: the channel(s) it was
    /// addressed to, and the message without `__asgi_channel__` (`receive_single`).
    pub async fn receive_single(&self, channel: &str) -> Result<(Vec<String>, Value), KanteError> {
        let mut connection = self.blocking_connection().await?;
        let channel_key = format!("{}{channel}", self.config.prefix);
        loop {
            if let Some(content) = self.pop_with_clean(&mut connection, &channel_key).await? {
                self.clean_backup(&mut connection, &channel_key).await?;
                return self.unpack(channel, &content);
            }
        }
    }

    fn unpack(&self, channel: &str, content: &[u8]) -> Result<(Vec<String>, Value), KanteError> {
        let mut message = self.serializer.deserialize(content)?;
        let addressed = message
            .as_object_mut()
            .and_then(|map| map.remove("__asgi_channel__"));
        let channels = match addressed {
            Some(Value::String(one)) => vec![one],
            Some(Value::Array(many)) => many
                .into_iter()
                .filter_map(|c| c.as_str().map(str::to_owned))
                .collect(),
            _ => vec![channel.to_owned()],
        };
        Ok((channels, message))
    }

    /// A new process-local channel and the stream of its messages. The process's shared inbox
    /// is drained by one background task that hands each message to the channel(s) it names.
    /// The channel stops receiving when the returned receiver is dropped.
    pub async fn subscribe(
        &self,
        prefix: &str,
    ) -> Result<(String, mpsc::UnboundedReceiver<Value>), KanteError> {
        let channel = self.new_channel(prefix);
        let (tx, rx) = mpsc::unbounded_channel();
        self.inboxes
            .lock()
            .expect("inboxes lock")
            .insert(channel.clone(), tx);
        let layer = self.clone();
        self.receiving
            .get_or_init(|| async move {
                tokio::spawn(async move { layer.dispatch_inbox().await });
            })
            .await;
        Ok((channel, rx))
    }

    /// Stop delivering to a process-local channel.
    pub fn unsubscribe(&self, channel: &str) {
        self.inboxes.lock().expect("inboxes lock").remove(channel);
    }

    async fn dispatch_inbox(self) {
        let inbox = format!("{}specific.{}!", self.config.prefix, self.client_prefix);
        loop {
            let mut connection = match self.blocking_connection().await {
                Ok(connection) => connection,
                Err(e) => {
                    tracing::error!("channel layer: no connection to receive on: {e}");
                    tokio::time::sleep(Duration::from_secs(1)).await;
                    continue;
                }
            };
            loop {
                let content = match self.pop_with_clean(&mut connection, &inbox).await {
                    Ok(Some(content)) => content,
                    Ok(None) => continue,
                    Err(e) => {
                        tracing::error!("channel layer: receiving failed: {e}");
                        tokio::time::sleep(Duration::from_secs(1)).await;
                        break;
                    }
                };
                match self.unpack(&inbox, &content) {
                    Ok((channels, message)) => {
                        let mut inboxes = self.inboxes.lock().expect("inboxes lock");
                        for channel in channels {
                            if let Some(tx) = inboxes.get(&channel) {
                                if tx.send(message.clone()).is_err() {
                                    inboxes.remove(&channel);
                                }
                            }
                        }
                    }
                    Err(e) => tracing::warn!("channel layer: dropping an unreadable message: {e}"),
                }
                if let Err(e) = self.clean_backup(&mut connection, &inbox).await {
                    tracing::warn!("channel layer: cleaning the in-flight backup failed: {e}");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn process_local_names_share_the_part_up_to_the_bang() {
        assert_eq!(non_local_name("specific.abc!def"), "specific.abc!");
        assert_eq!(non_local_name("plain"), "plain");
    }
}

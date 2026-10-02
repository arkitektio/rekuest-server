//! What the channel layer can fail with.

#[derive(Debug, thiserror::Error)]
pub enum KanteError {
    #[error("redis: {0}")]
    Redis(#[from] redis::RedisError),
    #[error("message is not a dict")]
    NotADict,
    #[error("channel {0} is full")]
    ChannelFull(String),
    #[error("could not serialize a message: {0}")]
    Serialize(String),
    #[error("could not deserialize a message: {0}")]
    Deserialize(String),
}

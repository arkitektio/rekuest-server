//! The message serializer (`channels_redis/serializers.py`, msgpack format).
//!
//! A message is msgpack, preceded by `random_prefix_length` random bytes: messages live in a
//! sorted set, whose members must be unique even when two messages are equal.

use rand::RngCore;
use serde_json::Value;

use crate::errors::KanteError;

#[derive(Debug, Clone, Copy)]
pub struct MsgPackSerializer {
    pub random_prefix_length: usize,
}

impl Default for MsgPackSerializer {
    /// channels_redis's default: a 12-byte prefix.
    fn default() -> Self {
        Self {
            random_prefix_length: 12,
        }
    }
}

impl MsgPackSerializer {
    pub fn serialize(&self, message: &Value) -> Result<Vec<u8>, KanteError> {
        let mut bytes = vec![0u8; self.random_prefix_length];
        rand::rng().fill_bytes(&mut bytes);
        rmp_serde::encode::write_named(&mut bytes, message)
            .map_err(|e| KanteError::Serialize(e.to_string()))?;
        Ok(bytes)
    }

    pub fn deserialize(&self, bytes: &[u8]) -> Result<Value, KanteError> {
        let body = bytes
            .get(self.random_prefix_length..)
            .ok_or_else(|| KanteError::Deserialize("message shorter than its prefix".into()))?;
        rmp_serde::from_slice(body).map_err(|e| KanteError::Deserialize(e.to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_behind_a_random_prefix() {
        let serializer = MsgPackSerializer::default();
        let message = serde_json::json!({"type": "channel.X", "message": {"id": "1", "n": 3, "f": 1.5, "x": null}});
        let a = serializer.serialize(&message).unwrap();
        let b = serializer.serialize(&message).unwrap();
        assert_ne!(a, b, "the prefix makes equal messages distinct members");
        assert_eq!(serializer.deserialize(&a).unwrap(), message);
    }
}

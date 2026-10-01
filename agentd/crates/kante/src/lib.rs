//! A channels_redis-compatible channel layer: the Rust twin of the Python `kante` channel layer
//! the Arkitekt servers run on channels_redis 4.3.0.
//!
//! [`core`] is the layer itself (groups, sends, receives, `channels_redis/core.py`),
//! [`serializers`] its msgpack format, [`channel`] kante's typed broadcasts on top.

pub mod channel;
pub mod core;
pub mod errors;
pub mod serializers;

pub use crate::core::{ChannelLayer, ChannelLayerConfig};
pub use errors::KanteError;

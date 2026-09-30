//! The agent-protocol app: the Rust twin of the Python server's `facade/`.
//!
//! Modules keep their Python names (`facade/codes.py` is [`codes`], `facade/liveness.py` is
//! [`liveness`], …), so a behaviour can be looked up on either side by the same path. Only
//! the agent protocol lives here; GraphQL and subscriptions stay in Python.

pub mod channel_events;
pub mod channels;
pub mod clock;
pub mod codes;
pub mod consumers;
pub mod context;
pub mod liveness;
pub mod message_router;
pub mod messages;
pub mod persist;
pub mod reaper;
pub mod redis_keys;
pub mod registration;
pub mod settings;
pub mod signals;

pub use context::Context;

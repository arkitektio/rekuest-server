//! The agent-protocol app: the Rust twin of the Python server's `facade/`.
//!
//! Modules keep their Python names (`facade/codes.py` is [`codes`], `facade/liveness.py` is
//! [`liveness`], …), so a behaviour can be looked up on either side by the same path. Only
//! the agent protocol lives here; GraphQL and subscriptions stay in Python.

pub mod backend;
pub mod caller_context;
pub mod caller_events;
pub mod channel_events;
pub mod channels;
pub mod codes;
pub mod consumers;
pub mod context;
pub mod guards;
pub mod higher_order;
pub mod liveness;
pub mod message_router;
pub mod messages;
pub mod persist;
pub mod probes;
pub mod provenance;
pub mod redis_keys;
pub mod registration;
pub mod service_trust;
pub mod settings;
pub mod signals;
pub mod transport;

pub use context::Context;

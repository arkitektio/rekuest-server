//! The agent-protocol app: the Rust twin of the Python server's `facade/`.
//!
//! Modules keep their Python names (`facade/codes.py` is [`codes`], `facade/liveness.py` is
//! [`liveness`], …), so a behaviour can be looked up on either side by the same path. Only
//! the agent protocol lives here; GraphQL and subscriptions stay in Python.

pub mod backend;
pub mod caller_context;
pub mod caller_events;
pub mod catalog_validation;
pub mod channel_events;
pub mod channels;
pub mod clock;
pub mod codes;
pub mod consumers;
pub mod context;
pub mod deletion;
pub mod descriptors;
pub mod embeddings;
pub mod guards;
pub mod higher_order;
pub mod hooks;
pub mod http_intake;
pub mod inference;
pub mod liveness;
pub mod message_router;
pub mod messages;
pub mod mutations;
pub mod persist;
pub mod probes;
pub mod protocol;
pub mod provenance;
pub mod reaper;
pub mod redis_keys;
pub mod registration;
pub mod registration_lock;
pub mod removal;
pub mod retention;
pub mod schedule_notices;
pub mod schedules;
pub mod schema;
pub mod service_trust;
pub mod settings;
pub mod signal_intake;
pub mod signals;
pub mod timing;
pub mod transport;
pub mod triggers;
pub mod unique;
pub mod upkeep;

pub use context::Context;

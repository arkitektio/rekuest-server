//! The agent-protocol app: the Rust twin of the Python server's `facade/`.
//!
//! Modules keep their Python names (`facade/codes.py` is [`codes`], `facade/liveness.py` is
//! [`liveness`], …), so a behaviour can be looked up on either side by the same path. Only
//! the agent protocol lives here; GraphQL and subscriptions stay in Python.

pub mod codes;
pub mod liveness;
pub mod redis_keys;
pub mod settings;

//! Caller sessions: a person's app window on takt (`/session`, docs/session-protocol.md).
//!
//! The window watches topics (its tasks, a task's events, an agent's states, …) and calls
//! (assign, cancel, decide, …) over one socket. takt keeps what it watches and how far it got,
//! so a window that comes back within the grace gets exactly what it missed; the window keeps
//! its session id and the newest `pos` it received. Tasks never depend on a window, unless one
//! is armed with a dead man switch ([`deadman`]).

pub mod calls;
mod connection;
pub mod deadman;
pub mod frames;
pub mod registry;
pub mod topics;

pub use connection::serve;

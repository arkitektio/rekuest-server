//! Probes: hover-grade invocations that never touch the database (`facade/probes/`).
//!
//! A probe is the throwaway sibling of a task: same wire protocol (the agent receives a normal
//! `ASSIGN` and reports normal events), but all server state lives in redis under a TTL: no row,
//! no events, no replay, no recovery sweep. The id space separates them: task ids are integers,
//! probe ids `p-<hex>` (`ids.py`), so every router branches on a prefix.

pub mod backend;
pub mod persist;
pub mod store;

pub use crate::messages::{is_probe_task as is_probe_id, PROBE_PREFIX};

/// A fresh probe id (`new_probe_id`).
pub fn new_probe_id() -> String {
    format!("{PROBE_PREFIX}{}", uuid::Uuid::new_v4().simple())
}

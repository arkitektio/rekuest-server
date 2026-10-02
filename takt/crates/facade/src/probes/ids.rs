//! The probe id space (`facade/probes/ids.py`): task ids are integer primary keys, so a `p-`
//! prefixed hex uuid never collides with one. Wherever an id crosses the protocol, a prefix check
//! tells a task from a probe.

pub use crate::messages::{is_probe_task as is_probe_id, PROBE_PREFIX};

/// A fresh probe id (`new_probe_id`).
pub fn new_probe_id() -> String {
    format!("{PROBE_PREFIX}{}", uuid::Uuid::new_v4().simple())
}

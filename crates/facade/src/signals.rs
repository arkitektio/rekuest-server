//! The fan-out that follows a committed write (`facade/signals.py`, `facade/transport.py`).
//!
//! Replaced by the kante branch: these are the call sites' contract, as no-ops here, so the
//! persistence code calls them where the Python server's `post_save` handlers fire.

use crate::context::Context;

/// A task row was written (`task_post_save`).
pub async fn task_saved(_ctx: &Context, _task: i64, _created: bool) {}

/// A task event was created (`task_event_post_save` → `publish_task_event`).
pub async fn task_event_created(_ctx: &Context, _event: i64) {}

/// A patch was created (`patch_post_save`).
pub async fn patch_created(_ctx: &Context, _patch: i64) {}

/// A state row was written (`state_post_save`).
pub async fn state_saved(_ctx: &Context, _state: i64) {}

/// An agent row was written (`agent_post_save`).
pub async fn agent_saved(_ctx: &Context, _agent: i64, _created: bool) {}

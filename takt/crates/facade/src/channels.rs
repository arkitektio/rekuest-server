//! The change-feed channels and their names (`facade/channels.py`): a message's type is
//! `channel.<name>`, which the GraphQL subscriptions listen for.

pub const AGENT_UPDATED: &str = "agent_updated_broadcast";
pub const TASK_EVENT: &str = "TaskEventCreatedEvent";
pub const CHILD_TASK: &str = "child_task_feed";
pub const AGENT_TASK: &str = "agent_task_feed";
pub const NEW_IMPLEMENTATION: &str = "ImplementationEvent";
pub const PATCH: &str = "PatchEvent";
pub const STATE_UPDATE: &str = "StateUpdateEvent";
pub const PROBE_EVENT: &str = "probe_event_broadcast";

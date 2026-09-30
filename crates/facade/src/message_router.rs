//! Where a registered agent's frames go (`facade/message_router.py`).
//!
//! Reports, state, locks, shelving and the agent's requests are routed as their phases land;
//! until then a frame is logged, so nothing is dropped silently.

use crate::consumers::agent_protocol::Sender;
use crate::context::Context;
use crate::messages::AgentMessage;

pub async fn route(_ctx: &Context, agent: i64, message: AgentMessage, _sender: &Sender) {
    let kind = serde_json::to_value(&message)
        .ok()
        .and_then(|v| v.get("type").and_then(|t| t.as_str()).map(str::to_owned))
        .unwrap_or_default();
    tracing::warn!(agent, kind, "no route for this frame yet");
}

//! The one place that knows how to reach an agent (`facade/transport.py`'s `deliver_to_agent`).
//!
//! A WEBSOCKET agent's commands go into its redis queue, which also holds them while it is
//! offline; the connection holding its lease drains it (`consumers::agent_queue`). A WEBHOOK
//! agent is reached by a signed POST, which arrives with the hook agents (Phase 5): until then a
//! command for one is logged and reported undelivered, so a dispatch leaves `dispatched_at`
//! NULL and the pickup watchdog owns the retry.
//!
//! The publishing half of `transport.py` (`publish_task_event`) is `signals::task_event_created`.
//! Routing is read fresh on every delivery, never cached: an agent that flipped WEBSOCKET →
//! WEBHOOK must not have its commands pushed into a list no socket drains any more.

use crate::consumers::agent_queue;
use crate::context::Context;
use crate::messages::{ToAgent, ToAgentFrame};

/// `AgentKind.WEBHOOK`.
pub const WEBHOOK: &str = "WEBHOOK";

#[derive(Debug, thiserror::Error)]
pub enum DeliveryError {
    #[error("agent lookup: {0}")]
    Database(#[from] sqlx::Error),
    #[error("agent queue: {0}")]
    Redis(#[from] redis::RedisError),
}

/// A message as `model_dump_json()` writes it: a fresh `id`, the `type`, the fields.
pub fn frame_text(message: ToAgent) -> String {
    let message = match message {
        // The frame carries the id; an Assign's own must stay unset or it would appear twice.
        ToAgent::Assign(mut assign) => {
            assign.id = None;
            ToAgent::Assign(assign)
        }
        message => message,
    };
    serde_json::to_string(&ToAgentFrame {
        id: Some(uuid::Uuid::new_v4().to_string()),
        message,
    })
    .expect("a ToAgent frame serializes")
}

/// Send one message to `agent` over its transport (`deliver_to_agent`). Whether the transport
/// accepted it; a redis failure is an error. `priority` (probe traffic) jumps the queued backlog.
pub async fn deliver_to_agent(
    ctx: &Context,
    agent: i64,
    message: ToAgent,
    priority: bool,
) -> Result<bool, DeliveryError> {
    let kind: String = sqlx::query_scalar("SELECT kind FROM facade_agent WHERE id = $1")
        .bind(agent)
        .fetch_one(&ctx.db)
        .await?;
    if kind == WEBHOOK {
        tracing::error!(
            agent,
            "agent {agent} is a WEBHOOK agent: agentd does not deliver to hooks yet (Phase 5); the message stays undelivered"
        );
        return Ok(false);
    }
    let mut redis = ctx.redis.clone();
    agent_queue::push(
        &mut redis,
        &ctx.settings,
        agent,
        &frame_text(message),
        priority,
    )
    .await?;
    Ok(true)
}

/// Deliver, never failing (`AgentConsumer.broadcast` at the best-effort call sites): a failure
/// is logged and reported as not delivered.
pub async fn broadcast(ctx: &Context, agent: i64, message: ToAgent, priority: bool) -> bool {
    match deliver_to_agent(ctx, agent, message, priority).await {
        Ok(delivered) => delivered,
        Err(e) => {
            tracing::error!(agent, "delivering to agent {agent} failed: {e}");
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::messages::Assign;
    use serde_json::Value;

    #[test]
    fn an_assign_carries_one_id() {
        let text = frame_text(ToAgent::Assign(Box::new(Assign {
            id: Some("inner".into()),
            interface: "echo".into(),
            task: "1".into(),
            root: None,
            parent: None,
            resolution: None,
            step: None,
            probe: false,
            capture: Some(false),
            reference: None,
            args: Default::default(),
            message: None,
            user: "u".into(),
            org: "o".into(),
            action: "h".into(),
            implementation: "2".into(),
            token: None,
            resume: None,
        })));
        assert_eq!(text.matches("\"id\"").count(), 1);
        let value: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(value["type"], "ASSIGN");
        assert_ne!(value["id"], "inner");
    }
}

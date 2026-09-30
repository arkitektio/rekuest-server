//! The one place that knows how to reach an agent (`facade/transport.py`'s `deliver_to_agent`).
//!
//! A WEBSOCKET agent's commands go into its redis queue, which also holds them while it is
//! offline; the connection holding its lease drains it (`consumers::agent_queue`). A WEBHOOK
//! agent is reached by a signed POST ([`crate::hooks`]); one that fails is reported undelivered,
//! so a dispatch leaves `dispatched_at` NULL and the pickup watchdog owns the retry.
//!
//! The publishing half of `transport.py` (`publish_task_event`) is `signals::task_event_created`.
//! Routing is read fresh on every delivery, never cached: an agent that flipped WEBSOCKET →
//! WEBHOOK must not have its commands pushed into a list no socket drains any more.

use crate::consumers::agent_queue;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use crate::caller_events::{build_execution_event, EventLike};
use crate::context::Context;
use crate::hooks::{self, HookTarget};
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
        // No queue to jump: `priority` means nothing to a hook.
        let target = hook_target(ctx, agent).await?;
        return Ok(hooks::deliver_to_hook(&ctx.settings, &target, &frame_text(message)).await);
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

/// The routing row of a HookAgent, read fresh (`get_agent_for_delivery`).
async fn hook_target(ctx: &Context, agent: i64) -> Result<HookTarget, sqlx::Error> {
    sqlx::query_as(
        "SELECT a.id, a.hook_url, a.hook_url_secret, c.client_id
           FROM facade_agent a JOIN authentikate_client c ON c.id = a.client_id WHERE a.id = $1",
    )
    .bind(agent)
    .fetch_one(&ctx.db)
    .await
}

/// How long "this caller has no HookAgent" (or has this one) is believed (`_WEBHOOK_LOOKUP_TTL`):
/// almost no caller has one, and a stale answer costs a best-effort mirror, never the event.
const WEBHOOK_LOOKUP_TTL: Duration = Duration::from_secs(5);
const WEBHOOK_LOOKUP_MAX: usize = 4096;

type WebhookCache = Mutex<HashMap<i64, (Instant, Option<HookTarget>)>>;

fn webhook_cache() -> &'static WebhookCache {
    static CACHE: OnceLock<WebhookCache> = OnceLock::new();
    CACHE.get_or_init(WebhookCache::default)
}

/// The caller's HookAgent, if it has one (`_get_webhook_agent_for_caller`).
async fn webhook_agent_for_caller(
    ctx: &Context,
    caller: i64,
) -> Result<Option<HookTarget>, sqlx::Error> {
    if let Some((at, target)) = webhook_cache().lock().expect("webhook cache").get(&caller) {
        if at.elapsed() < WEBHOOK_LOOKUP_TTL {
            return Ok(target.clone());
        }
    }
    let target: Option<HookTarget> = sqlx::query_as(
        "SELECT a.id, a.hook_url, a.hook_url_secret, cl.client_id
           FROM facade_caller c
           JOIN facade_agent a ON a.client_id = c.client_id AND a.user_id = c.user_id
                              AND a.organization_id = c.organization_id
           JOIN authentikate_client cl ON cl.id = a.client_id
          WHERE c.id = $1 AND a.kind = 'WEBHOOK' AND a.hook_url IS NOT NULL AND a.hook_url <> ''
          ORDER BY a.id LIMIT 1",
    )
    .bind(caller)
    .fetch_optional(&ctx.db)
    .await?;
    let mut cache = webhook_cache().lock().expect("webhook cache");
    if cache.len() >= WEBHOOK_LOOKUP_MAX {
        cache.clear();
    }
    cache.insert(caller, (Instant::now(), target.clone()));
    Ok(target)
}

/// If the task's caller is a HookAgent, POST it the event's mirror
/// (`_deliver_caller_event_to_webhook`). Best effort, like every mirror.
pub async fn deliver_caller_event_to_webhook(ctx: &Context, caller: i64, event: &EventLike) {
    let target = match webhook_agent_for_caller(ctx, caller).await {
        Ok(Some(target)) => target,
        Ok(None) => return,
        Err(e) => {
            tracing::error!(caller, "looking up the caller's HookAgent failed: {e}");
            return;
        }
    };
    if let Some(mirror) = build_execution_event(event) {
        hooks::deliver_to_hook(&ctx.settings, &target, &frame_text(mirror)).await;
    }
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

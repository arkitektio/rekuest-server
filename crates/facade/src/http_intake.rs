//! The HTTP intake of HookAgents (`facade/http_intake.py`): `POST /agi/http/{agent}`.
//!
//! A HookAgent POSTs the frames a socket agent would send, signed, and they go through the same
//! [`message_router::route`]; the reply (`EVENT_ACK`, `ASSIGN_RESPONSE`, …) is the response
//! body. HTTP has no session, so every request stands alone: its signature is timestamped, and
//! its digest is claimed once in redis, across every replica, which also makes an ordinary HTTP
//! retry of a fire-and-forget report idempotent.

use axum::http::HeaderMap;
use serde_json::{json, Value};

use crate::context::Context;
use crate::hooks::{self, SIGNATURE_HEADER, SIGNATURE_V1_HEADER};
use crate::message_router::{self, RouteError};
use crate::messages::{AgentFrame, FromAgent, ToAgent};
use crate::redis_keys;
use crate::service_trust;
use crate::transport::frame_text;

/// A response: its status and JSON body.
pub type Answer = (u16, Value);

fn error(status: u16, message: impl Into<String>) -> Answer {
    (status, json!({"error": message.into()}))
}

/// A reply as the response body: the frame as the socket would carry it.
fn body_of(reply: ToAgent) -> Value {
    serde_json::from_str(&frame_text(reply)).expect("a frame is JSON")
}

#[derive(sqlx::FromRow)]
struct HookAgent {
    id: i64,
    blocked: bool,
    hook_url_secret: Option<String>,
    client_id: String,
}

/// `(ok, digest)`: whether the request is the agent's, and what names it for the replay guard
/// (`_authenticate`). A service agent's report carries a service token (its `jti` is the digest);
/// any other a V1 signature, or in `compat` mode the legacy one, which has no digest.
async fn authenticate(
    ctx: &Context,
    agent: &HookAgent,
    body: &[u8],
    headers: &HeaderMap,
    path: &str,
) -> (bool, Option<String>) {
    let header = |name: &str| headers.get(name).and_then(|v| v.to_str().ok());
    if let Some(entry) = service_trust::entry_for_client(&ctx.settings, &agent.client_id) {
        return match service_trust::verify_from(
            &ctx.settings,
            entry,
            "POST",
            path,
            body,
            header("Authorization"),
        )
        .await
        {
            Ok(verified) => (true, Some(format!("jwt:{}", verified.jti))),
            Err(e) => {
                tracing::info!("Service agent {}: refused a report: {e}", agent.id);
                (false, None)
            }
        };
    }
    let secret = agent.hook_url_secret.as_deref();
    if let Some(v1) = header(SIGNATURE_V1_HEADER) {
        let now = chrono::Utc::now().timestamp();
        return hooks::verify_v1(
            secret,
            &agent.id.to_string(),
            body,
            Some(v1),
            ctx.settings.hook_max_skew,
            now,
        );
    }
    if ctx.settings.hook_signature_strict {
        return (false, None);
    }
    if hooks::verify(secret, body, header(SIGNATURE_HEADER)) {
        tracing::warn!("HookAgent {} used the legacy body-only signature; it is replayable and goes away next release", agent.id);
        return (true, None);
    }
    (false, None)
}

fn replay_key(ctx: &Context, agent: i64, digest: &str) -> String {
    redis_keys::key(&ctx.settings, &[&"hook-replay", &agent, &digest])
}

/// Claim this exact request once; false when it was seen before. The key outlives the skew
/// window on both sides, the whole time a replay could still pass the signature (`_claim_request`).
async fn claim_request(ctx: &Context, agent: i64, digest: &str) -> redis::RedisResult<bool> {
    let mut redis = ctx.redis.clone();
    let set: Option<String> = redis::cmd("SET")
        .arg(replay_key(ctx, agent, digest))
        .arg("1")
        .arg("NX")
        .arg("EX")
        .arg((ctx.settings.hook_max_skew * 2).max(1))
        .query_async(&mut redis)
        .await?;
    Ok(set.is_some())
}

async fn release_request(ctx: &Context, agent: i64, digest: &str) {
    let mut redis = ctx.redis.clone();
    if let Err(e) = redis::cmd("DEL")
        .arg(replay_key(ctx, agent, digest))
        .query_async::<i64>(&mut redis)
        .await
    {
        tracing::error!(agent, "Could not release a hook replay claim: {e}");
    }
}

/// How to answer a request the replay guard has seen, or `None` to route it anyway
/// (`reply_for_duplicate`): an assign request and shelving are idempotent and routed; a report
/// is acked, so the agent stops retaining it; a control request is refused (409), since acking
/// an instruction not applied this time would be a lie; anything else is dropped.
pub fn reply_for_duplicate(frame: &AgentFrame) -> Option<Answer> {
    match &frame.message {
        FromAgent::AssignRequest { .. } | FromAgent::Shelve { .. } | FromAgent::Unshelve { .. } => {
            None
        }
        FromAgent::CancelRequest { .. }
        | FromAgent::InterruptRequest { .. }
        | FromAgent::PauseRequest { .. }
        | FromAgent::ResumeRequest { .. } => Some((
            409,
            body_of(ToAgent::ProtocolError {
                error: "Replayed control request — re-sign and retry.".into(),
            }),
        )),
        FromAgent::Started { .. }
        | FromAgent::Log { .. }
        | FromAgent::Progress { .. }
        | FromAgent::Yield { .. }
        | FromAgent::Completed { .. }
        | FromAgent::Failed { .. }
        | FromAgent::Critical { .. }
        | FromAgent::Cancelled { .. }
        | FromAgent::Interrupted { .. }
        | FromAgent::Paused { .. }
        | FromAgent::Resumed { .. }
        | FromAgent::Effect { .. } => Some((200, body_of(message_router::ack(frame)))),
        _ => Some((200, json!({"duplicate": true}))),
    }
}

/// Parse a frame as Python's `FromAgentPayload` would: a frame without an `id` gets one.
fn parse(body: &[u8]) -> Result<AgentFrame, String> {
    let mut value: Value = serde_json::from_slice(body).map_err(|e| e.to_string())?;
    if let Some(frame) = value.as_object_mut() {
        frame
            .entry("id")
            .or_insert_with(|| json!(uuid::Uuid::new_v4().to_string()));
    }
    serde_json::from_value(value).map_err(|e| e.to_string())
}

/// Authenticate, validate and route one frame of a HookAgent (`hook_intake`). `path` is the
/// request's full path, which a service token is bound to.
pub async fn hook_intake(
    ctx: &Context,
    agent_id: &str,
    path: &str,
    headers: &HeaderMap,
    body: &[u8],
) -> Answer {
    let Ok(agent_id) = agent_id.parse::<i64>() else {
        return error(404, "Unknown hook agent");
    };
    let agent: Option<HookAgent> = match sqlx::query_as(
        "SELECT a.id, a.blocked, a.hook_url_secret, c.client_id
           FROM facade_agent a JOIN authentikate_client c ON c.id = a.client_id
          WHERE a.id = $1 AND a.kind = 'WEBHOOK'",
    )
    .bind(agent_id)
    .fetch_optional(&ctx.db)
    .await
    {
        Ok(agent) => agent,
        Err(e) => return error(500, e.to_string()),
    };
    let Some(agent) = agent else {
        return error(404, "Unknown hook agent");
    };
    if agent.blocked {
        return error(403, "Agent is blocked");
    }
    let (authenticated, digest) = authenticate(ctx, &agent, body, headers, path).await;
    if !authenticated {
        return error(401, "Invalid signature");
    }
    let frame = match parse(body) {
        Ok(frame) => frame,
        Err(e) => return error(400, format!("Invalid message: {e}")),
    };
    if matches!(
        frame.message,
        FromAgent::Register { .. } | FromAgent::HeartbeatAnswer {}
    ) {
        return error(
            400,
            "Unhandled message: a HookAgent has no session to register or keep alive",
        );
    }

    if let Some(digest) = &digest {
        match claim_request(ctx, agent.id, digest).await {
            Ok(true) => {}
            Ok(false) => {
                tracing::info!("HookAgent {} replayed a request", agent.id);
                if let Some(answer) = reply_for_duplicate(&frame) {
                    return answer;
                }
            }
            Err(e) => {
                // Fail closed: without the guard, knocking redis over would be enough to replay.
                tracing::error!("Hook replay guard unavailable: {e}");
                return error(503, "Replay guard unavailable");
            }
        }
    }

    match message_router::route(ctx, agent.id, &frame, None).await {
        Ok(Some(reply)) => (200, body_of(reply)),
        Ok(None) => (200, json!({})),
        Err(e) => {
            tracing::info!("Hook intake refused: {e}");
            // The request never took effect: the sender's retry must not count as a replay.
            if let Some(digest) = &digest {
                release_request(ctx, agent.id, digest).await;
            }
            let message = match e {
                RouteError::Refused(reason) => reason,
                other => other.to_string(),
            };
            error(400, message)
        }
    }
}

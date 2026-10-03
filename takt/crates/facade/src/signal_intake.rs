//! `POST /agi/signal/{service}`: a hub service announces that something happened to one of its
//! objects (`facade/signals_intake.py`).
//!
//! The service is one of `SERVICES`; the request carries a service token signed with that
//! service's instance key, bound to this path and body. It is keyed by service NAME, so a service
//! can signal before it knows its agent id. A task's provenance token in the body is checked
//! against this rekuest's own key; only then does the signal carry a `causing_task`. The signal is
//! stored and acknowledged at once (202); matching and firing are the scheduler's triggers. A
//! resend with the same `id` is a no-op.

use axum::http::HeaderMap;
use serde::Deserialize;
use serde_json::{json, Map, Value};

use crate::context::Context;
use crate::http_intake::{claim_request, release_request, Answer};
use crate::provenance::verify::verify_own_token;
use crate::service_trust;

/// What a service sends (`SignalMessage`).
#[derive(Debug, Clone, Deserialize)]
pub struct SignalMessage {
    pub id: String,
    pub kind: String,
    pub identifier: String,
    pub object: String,
    pub organization: String,
    #[serde(default)]
    pub descriptors: Map<String, Value>,
    #[serde(default)]
    pub provenance: Option<String>,
    #[serde(default)]
    pub occurred_at: Option<chrono::DateTime<chrono::FixedOffset>>,
}

impl SignalMessage {
    /// The model's constraints: lengths and the kind vocabulary.
    fn check(&self) -> Result<(), String> {
        let within = |value: &str, field: &str, max: usize| {
            if value.is_empty() || value.chars().count() > max {
                Err(format!("{field}: must be 1..{max} characters"))
            } else {
                Ok(())
            }
        };
        within(&self.id, "id", 200)?;
        within(&self.identifier, "identifier", 1000)?;
        within(&self.object, "object", 1000)?;
        if self.organization.is_empty() {
            return Err("organization: must not be empty".into());
        }
        if !matches!(self.kind.as_str(), "CREATED" | "UPDATED" | "DELETED") {
            return Err(format!(
                "kind: {:?} is not CREATED, UPDATED or DELETED",
                self.kind
            ));
        }
        Ok(())
    }
}

/// The replay-guard namespace of a service's signals (`signed_id`), distinct from any agent id.
fn signed_id(service: &str) -> String {
    format!("signal:{service}")
}

fn error(status: u16, message: impl Into<String>) -> Answer {
    (status, json!({"error": message.into()}))
}

/// `(signal id, created, causing task)`, or `None` when the organization is unknown here
/// (`_store`).
async fn store(
    ctx: &Context,
    service: &str,
    message: &SignalMessage,
) -> Result<Option<(i64, bool, Option<i64>)>, sqlx::Error> {
    let organization: Option<i64> =
        sqlx::query_scalar("SELECT id FROM authentikate_organization WHERE slug = $1")
            .bind(&message.organization)
            .fetch_optional(&ctx.db)
            .await?;
    let Some(organization) = organization else {
        tracing::warn!(
            "Signal {} from {service} names unknown organization {:?}; dropped",
            message.id,
            message.organization
        );
        return Ok(None);
    };
    let existing = || async {
        sqlx::query_as::<_, (i64, Option<i64>)>(
            "SELECT id, causing_task_id FROM facade_signal WHERE service = $1 AND signal_id = $2",
        )
        .bind(service)
        .bind(&message.id)
        .fetch_optional(&ctx.db)
        .await
    };
    if let Some((id, cause)) = existing().await? {
        return Ok(Some((id, false, cause)));
    }

    // The token proves the service was called in that task; the organization check proves the
    // task belongs where the object does.
    let mut causing_task = None;
    if let Some(provenance) = verify_own_token(&ctx.settings, message.provenance.as_deref()) {
        let task: Option<i64> = match provenance.task.parse::<i64>() {
            Ok(id) => {
                sqlx::query_scalar(
                    "SELECT t.id FROM facade_task t JOIN facade_agent a ON a.id = t.agent_id
                      WHERE t.id = $1 AND a.organization_id = $2",
                )
                .bind(id)
                .bind(organization)
                .fetch_optional(&ctx.db)
                .await?
            }
            Err(_) => None,
        };
        match task {
            Some(task) => causing_task = Some(task),
            None => tracing::warn!(
                "Signal {} from {service}: provenance task {} is not in {}; stored without a cause",
                message.id,
                provenance.task,
                message.organization
            ),
        }
    }

    let inserted: Option<i64> = sqlx::query_scalar(
        "INSERT INTO facade_signal (service, signal_id, kind, identifier, object, descriptors, occurred_at,
                                    organization_id, causing_task_id)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
         ON CONFLICT (service, signal_id) DO NOTHING RETURNING id",
    )
    .bind(service)
    .bind(&message.id)
    .bind(&message.kind)
    .bind(&message.identifier)
    .bind(&message.object)
    .bind(Value::Object(message.descriptors.clone()))
    .bind(message.occurred_at)
    .bind(organization)
    .bind(causing_task)
    .fetch_optional(&ctx.db)
    .await?;
    match inserted {
        Some(id) => Ok(Some((id, true, causing_task))),
        // The same signal, raced in from a concurrent resend.
        None => Ok(existing().await?.map(|(id, cause)| (id, false, cause))),
    }
}

/// Authenticate, validate and store one signal (`signal_intake`).
pub async fn signal_intake(
    ctx: &Context,
    service: &str,
    path: &str,
    headers: &HeaderMap,
    body: &[u8],
) -> Answer {
    let Some(entry) = service_trust::entry_for_service(&ctx.settings, service) else {
        return error(404, "Unknown service");
    };
    let authorization = headers.get("Authorization").and_then(|v| v.to_str().ok());
    let verified =
        match service_trust::verify_from(&ctx.settings, entry, "POST", path, body, authorization)
            .await
        {
            Ok(verified) => verified,
            Err(e) => {
                tracing::info!("Refused a signal from {service}: {e}");
                return error(401, "Invalid signature");
            }
        };
    let digest = format!("jwt:{}", verified.jti);

    let message: SignalMessage = match serde_json::from_slice(body)
        .map_err(|e| e.to_string())
        .and_then(|m: SignalMessage| m.check().map(|_| m))
    {
        Ok(message) => message,
        Err(e) => return error(400, format!("Invalid signal: {e}")),
    };

    let namespace = signed_id(service);
    match claim_request(ctx, &namespace, &digest).await {
        Ok(true) => {}
        Ok(false) => return (202, json!({"duplicate": true})),
        Err(e) => {
            tracing::error!("Signal replay guard unavailable: {e}");
            return error(503, "Replay guard unavailable");
        }
    }

    match store(ctx, service, &message).await {
        Ok(Some((signal, created, cause))) => (
            202,
            json!({"signal": signal.to_string(), "created": created, "caused_by": cause.map(|c| c.to_string())}),
        ),
        Ok(None) => (202, json!({"dropped": "unknown organization"})),
        Err(e) => {
            release_request(ctx, &namespace, &digest).await;
            tracing::error!("Could not store signal {} from {service}: {e}", message.id);
            error(500, "Could not store the signal")
        }
    }
}

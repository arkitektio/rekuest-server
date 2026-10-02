//! An agent's row, as the GraphQL mutations and the hub-service provisioning set it
//! (`facade/mutations/agent.py`: `ensureAgent`, `implementAgent`).
//!
//! A socket agent does both itself on REGISTER; these serve the callers that have no socket:
//! dashboards, HookAgents bootstrapping, and the service agents rekuest provisions.

use rekuest_core::inputs::ImplementAgentInputModel;
use serde::{Deserialize, Deserializer};

use super::Refusal;
use crate::catalog_validation::Diagnostic;
use crate::consumers::agent_queue::{processing_key, queue_key};
use crate::context::Context;
use crate::registration::{clear_drawers, ensure_agent, implement_agent};
use crate::signals::{OnCommit, Signal};

/// `Some(None)` when the field is present and null (clear it), `None` when absent (keep it).
fn present<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Option<Option<String>>, D::Error> {
    Ok(Some(Option::deserialize(deserializer)?))
}

/// What `ensureAgent` sets (`AgentInput`).
#[derive(Debug, Default, Deserialize)]
pub struct EnsureAgentInput {
    /// Names a NEW agent; an existing agent keeps its name (`updateAgent` owns it).
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    /// `WEBSOCKET` or `WEBHOOK`.
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default, deserialize_with = "present")]
    pub hook_url: Option<Option<String>>,
    #[serde(default, deserialize_with = "present")]
    pub hook_url_secret: Option<Option<String>>,
    /// Forget what a previous process shelved (`ensureAgent` does; provisioning does not).
    #[serde(default)]
    pub clear_drawers: bool,
}

/// The agent of `(client, user, organization)`, created if missing, then configured
/// (`ensure_agent`). An agent turning WEBHOOK abandons its socket queue: no connection will
/// drain it again, so the frames are dropped and its unpicked tasks marked never dispatched, for
/// the pickup watchdog to redeliver over the hook.
pub async fn ensure(
    ctx: &Context,
    client: i64,
    user: i64,
    organization: i64,
    input: &EnsureAgentInput,
) -> Result<i64, Refusal> {
    if let Some(kind) = input.kind.as_deref() {
        if !matches!(kind, "WEBSOCKET" | "WEBHOOK") {
            return Err(Refusal::Invalid(format!("Unknown agent kind {kind:?}")));
        }
    }
    let existed: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM facade_agent WHERE client_id = $1 AND user_id = $2 AND organization_id = $3)",
    )
    .bind(client)
    .bind(user)
    .bind(organization)
    .fetch_one(&ctx.db)
    .await?;
    let agent = ensure_agent(&ctx.db, client, user, organization).await?;
    let mut on_commit = OnCommit::default();
    if !existed {
        if let Some(name) = input.name.as_deref().filter(|n| !n.is_empty()) {
            sqlx::query("UPDATE facade_agent SET name = $2 WHERE id = $1")
                .bind(agent)
                .bind(name)
                .execute(&ctx.db)
                .await?;
        }
        on_commit.push(Signal::AgentSaved {
            id: agent,
            created: true,
        });
    }
    if input.clear_drawers {
        clear_drawers(&ctx.db, agent).await?;
    }

    let before: String = sqlx::query_scalar("SELECT kind FROM facade_agent WHERE id = $1")
        .bind(agent)
        .fetch_one(&ctx.db)
        .await?;
    let changed = sqlx::query(
        "UPDATE facade_agent SET
             description = CASE WHEN $2 THEN $3 ELSE description END,
             kind = coalesce($4, kind),
             hook_url = CASE WHEN $5 THEN $6 ELSE hook_url END,
             hook_url_secret = CASE WHEN $7 THEN $8 ELSE hook_url_secret END
          WHERE id = $1 AND ($2 OR $4 IS NOT NULL OR $5 OR $7)",
    )
    .bind(agent)
    .bind(input.description.is_some())
    .bind(&input.description)
    .bind(&input.kind)
    .bind(input.hook_url.is_some())
    .bind(input.hook_url.clone().flatten())
    .bind(input.hook_url_secret.is_some())
    .bind(input.hook_url_secret.clone().flatten())
    .execute(&ctx.db)
    .await?
    .rows_affected();
    if changed > 0 {
        on_commit.push(Signal::AgentSaved {
            id: agent,
            created: false,
        });
    }
    if input.kind.as_deref() == Some("WEBHOOK") && before != "WEBHOOK" {
        abandon_socket_queue(ctx, agent).await?;
    }
    on_commit.publish(ctx).await;
    Ok(agent)
}

/// `_abandon_socket_queue`: drop what was queued for a socket that will never drain it, and let
/// the watchdog redeliver the unpicked work over the hook (its order is gone, its tokens stale).
async fn abandon_socket_queue(ctx: &Context, agent: i64) -> Result<(), Refusal> {
    let mut redis = ctx.redis.clone();
    let dropped: Result<i64, _> = redis::cmd("DEL")
        .arg(queue_key(&ctx.settings, agent))
        .arg(processing_key(&ctx.settings, agent))
        .query_async(&mut redis)
        .await;
    match dropped {
        Ok(n) if n > 0 => tracing::warn!(
            agent,
            "Agent {agent} became a HookAgent: dropped its socket queue"
        ),
        Ok(_) => {}
        Err(e) => tracing::error!(
            agent,
            "Could not drop the socket queue of agent {agent}: {e}"
        ),
    }
    sqlx::query("UPDATE facade_task SET dispatched_at = NULL WHERE agent_id = $1 AND NOT is_done AND picked_up_at IS NULL")
        .bind(agent)
        .execute(&ctx.db)
        .await?;
    Ok(())
}

/// Reconcile a declaration for the agent of `(client, user, organization)` (`implementAgent`):
/// the agent, and the diagnostics of the registration.
pub async fn implement(
    ctx: &Context,
    client: i64,
    user: i64,
    organization: i64,
    payload: &ImplementAgentInputModel,
) -> Result<(i64, Vec<Diagnostic>), Refusal> {
    let agent = ensure_agent(&ctx.db, client, user, organization).await?;
    let mut tx = ctx.db.begin().await?;
    let implemented = implement_agent(&mut tx, agent, payload).await?;
    tx.commit().await?;
    implemented.on_commit.publish(ctx).await;
    Ok((agent, implemented.diagnostics))
}

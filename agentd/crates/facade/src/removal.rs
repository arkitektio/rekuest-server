//! Deleting what agentd owns: an agent, an implementation. The rekuest server asks (its
//! `deleteAgent` and `deleteImplementation`); the rows, their cascade and the feeds are here.

use crate::backend::{parse_id, BackendError, BackendResult};
use crate::consumers::agent_queue::{processing_key, queue_key};
use crate::context::Context;
use crate::deletion::{self, DeleteError};
use crate::messages::ToAgent;
use crate::registration_lock::lock_organization;
use crate::signals::{OnCommit, Signal};
use crate::transport;

impl From<DeleteError> for BackendError {
    fn from(e: DeleteError) -> Self {
        match e {
            DeleteError::Db(e) => BackendError::Database(e),
            protected => BackendError::Refused(protected.to_string()),
        }
    }
}

/// Delete an agent of `organization` with everything below it (`delete_agent`).
///
/// A connected agent is kicked first: its socket closes, and should the process still run, it
/// registers again as a new agent. One that is away has its queue dropped, since nothing will
/// drain it.
pub async fn delete_agent(ctx: &Context, organization: i64, agent: &str) -> BackendResult<i64> {
    let id = parse_id(agent)?;
    let mut tx = ctx.db.begin().await?;
    // Registrations of the organization write this agent's rows: none may run beside the delete.
    lock_organization(&mut tx, organization).await?;
    let connected: Option<bool> = sqlx::query_scalar(
        "SELECT connected FROM facade_agent WHERE id = $1 AND organization_id = $2 FOR UPDATE",
    )
    .bind(id)
    .bind(organization)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(connected) = connected else {
        return Err(BackendError::Forbidden(format!(
            "No Agent {agent} in this organization."
        )));
    };
    if connected {
        // While the row still says how to reach it.
        transport::broadcast(
            ctx,
            id,
            ToAgent::Kick {
                reason: Some("The agent was deleted".into()),
            },
            false,
        )
        .await;
    }
    let mut on_commit = OnCommit::default();
    for deleted in deletion::delete_agent(&mut tx, id).await? {
        on_commit.push(Signal::ImplementationDeleted {
            id: deleted.id,
            agent_id: deleted.agent_id,
        });
    }
    on_commit.push(Signal::AgentDeleted { id, organization });
    tx.commit().await?;
    if !connected {
        let mut redis = ctx.redis.clone();
        let dropped: Result<i64, _> = redis::cmd("DEL")
            .arg(queue_key(&ctx.settings, id))
            .arg(processing_key(&ctx.settings, id))
            .query_async(&mut redis)
            .await;
        if let Err(e) = dropped {
            tracing::error!(
                agent = id,
                "Could not drop the queue of deleted agent {id}: {e}"
            );
        }
    }
    on_commit.publish(ctx).await;
    Ok(id)
}

/// Delete an implementation of an agent of `organization` (`delete_implementation`).
pub async fn delete_implementation(
    ctx: &Context,
    organization: i64,
    implementation: &str,
) -> BackendResult<i64> {
    let id = parse_id(implementation)?;
    let mut tx = ctx.db.begin().await?;
    lock_organization(&mut tx, organization).await?;
    let found: Option<i64> = sqlx::query_scalar(
        "SELECT i.id FROM facade_implementation i JOIN facade_agent a ON a.id = i.agent_id
          WHERE i.id = $1 AND a.organization_id = $2 FOR UPDATE OF i",
    )
    .bind(id)
    .bind(organization)
    .fetch_optional(&mut *tx)
    .await?;
    if found.is_none() {
        return Err(BackendError::Forbidden(format!(
            "No Implementation {implementation} in this organization."
        )));
    }
    let mut on_commit = OnCommit::default();
    for deleted in deletion::delete_implementations(&mut tx, &[id]).await? {
        on_commit.push(Signal::ImplementationDeleted {
            id: deleted.id,
            agent_id: deleted.agent_id,
        });
    }
    tx.commit().await?;
    on_commit.publish(ctx).await;
    Ok(id)
}

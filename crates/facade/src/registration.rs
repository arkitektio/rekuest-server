//! An agent's identity and its declaration (`facade/registration.py`).

use sqlx::PgPool;

/// The agent for `(client, user, organization)`, created with its memory shelve if it did not
/// exist (`ensure_agent`). A new agent is named after its client and belongs to the client's
/// release (and its app); an existing agent is not touched.
pub async fn ensure_agent(
    db: &PgPool,
    client: i64,
    user: i64,
    organization: i64,
) -> Result<i64, sqlx::Error> {
    let (agent, name): (i64, String) = sqlx::query_as(
        "WITH client AS (
             SELECT c.client_id, c.release_id, r.app_id
               FROM authentikate_client c JOIN authentikate_release r ON r.id = c.release_id
              WHERE c.id = $1
         )
         INSERT INTO facade_agent
             (installed_at, hash, name, health_check_interval, \"unique\", lease_epoch, on_instance,
              kind, latest_event, connected, blocked, app_id, release_id, client_id, user_id,
              organization_id)
         SELECT now(), '', client.client_id, 300, gen_random_uuid()::text, 0, 'all',
                'WEBSOCKET', 'DISCONNECT', false, false, client.app_id, client.release_id, $1, $2, $3
           FROM client
         ON CONFLICT (client_id, user_id, organization_id) DO UPDATE SET name = facade_agent.name
         RETURNING id, name",
    )
    .bind(client)
    .bind(user)
    .bind(organization)
    .fetch_one(db)
    .await?;

    sqlx::query(
        "INSERT INTO facade_memoryshelve (name, description, created_at, updated_at, agent_id, creator_id, organization_id)
         VALUES ($1, '', now(), now(), $2, $3, $4)
         ON CONFLICT (agent_id) DO NOTHING",
    )
    .bind(format!("{name} memory shelve"))
    .bind(agent)
    .bind(user)
    .bind(organization)
    .execute(db)
    .await?;
    Ok(agent)
}

/// Forget every drawer on the agent's shelve: a process that just started holds nothing.
pub async fn clear_drawers(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
) -> Result<u64, sqlx::Error> {
    Ok(sqlx::query(
        "DELETE FROM facade_memorydrawer d USING facade_memoryshelve s
          WHERE d.shelve_id = s.id AND s.agent_id = $1",
    )
    .bind(agent)
    .execute(executor)
    .await?
    .rows_affected())
}

/// Record that `agent` holds `resource_id` (an `identifier`) in memory (`shelve`): upsert the
/// drawer on its shelve, keyed by `resource_id`. `agent_minted` (a numbered SHELVE) marks a
/// drawer the agent references by that id; a later plain upsert never unsets it. The drawer's id.
pub async fn shelve(
    db: &PgPool,
    agent: i64,
    identifier: &str,
    resource_id: &str,
    label: Option<&str>,
    description: Option<&str>,
    agent_minted: bool,
) -> Result<i64, sqlx::Error> {
    let shelve: i64 = sqlx::query_scalar(
        "INSERT INTO facade_memoryshelve (name, description, created_at, updated_at, agent_id, creator_id, organization_id)
         SELECT a.name || ' memory shelve', '', now(), now(), a.id, a.user_id, a.organization_id
           FROM facade_agent a WHERE a.id = $1
         ON CONFLICT (agent_id) DO UPDATE SET agent_id = EXCLUDED.agent_id
         RETURNING id",
    )
    .bind(agent)
    .fetch_one(db)
    .await?;
    sqlx::query_scalar(
        "INSERT INTO facade_memorydrawer (shelve_id, resource_id, identifier, label, description, agent_minted)
         VALUES ($1, $2, $3, $4, $5, $6)
         ON CONFLICT (shelve_id, resource_id) WHERE resource_id IS NOT NULL
         DO UPDATE SET identifier = EXCLUDED.identifier, label = EXCLUDED.label, description = EXCLUDED.description,
                       agent_minted = facade_memorydrawer.agent_minted OR EXCLUDED.agent_minted
         RETURNING id",
    )
    .bind(shelve)
    .bind(resource_id)
    .bind(identifier)
    .bind(label)
    .bind(description)
    .bind(agent_minted)
    .fetch_one(db)
    .await
}

/// Why an unshelve was refused.
#[derive(Debug, thiserror::Error)]
pub enum UnshelveError {
    #[error("Unknown drawer {0:?}")]
    Unknown(String),
    #[error("This drawer does not belong to this agent.")]
    Foreign,
    #[error("database: {0}")]
    Database(#[from] sqlx::Error),
}

/// Drop the drawer `drawer` from `agent`'s shelve (`unshelve`). With `by_resource_id` (a
/// numbered UNSHELVE) it is first looked up as a resource id on the agent's own shelve, then
/// as a primary key.
pub async fn unshelve(
    db: &PgPool,
    agent: i64,
    drawer: &str,
    by_resource_id: bool,
) -> Result<(), UnshelveError> {
    if by_resource_id {
        let own = sqlx::query(
            "DELETE FROM facade_memorydrawer d USING facade_memoryshelve s
              WHERE d.shelve_id = s.id AND s.agent_id = $1 AND d.id = (
                    SELECT d2.id FROM facade_memorydrawer d2 JOIN facade_memoryshelve s2 ON s2.id = d2.shelve_id
                     WHERE s2.agent_id = $1 AND d2.resource_id = $2 ORDER BY d2.id LIMIT 1)",
        )
        .bind(agent)
        .bind(drawer)
        .execute(db)
        .await?
        .rows_affected();
        if own > 0 {
            return Ok(());
        }
    }
    let Some(id) = drawer
        .parse::<i64>()
        .ok()
        .filter(|_| drawer.chars().all(|c| c.is_ascii_digit()))
    else {
        return Err(UnshelveError::Unknown(drawer.to_owned()));
    };
    let owner: Option<i64> = sqlx::query_scalar(
        "SELECT s.agent_id FROM facade_memorydrawer d JOIN facade_memoryshelve s ON s.id = d.shelve_id WHERE d.id = $1",
    )
    .bind(id)
    .fetch_optional(db)
    .await?;
    match owner {
        None => Err(UnshelveError::Unknown(drawer.to_owned())),
        Some(owner) if owner != agent => Err(UnshelveError::Foreign),
        Some(_) => {
            sqlx::query("DELETE FROM facade_memorydrawer WHERE id = $1")
                .bind(id)
                .execute(db)
                .await?;
            Ok(())
        }
    }
}

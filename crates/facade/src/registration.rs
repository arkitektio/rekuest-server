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

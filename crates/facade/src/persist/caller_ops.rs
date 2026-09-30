//! The caller side of an agent (`facade/persist/caller_ops.py`).

use sqlx::PgPool;

/// The durable `Caller` for an agent's own identity: a connection joins `task_caller_{id}` to
/// receive the events of work it assigned (`get_or_create_caller_id`).
pub async fn get_or_create_caller_id(db: &PgPool, agent: i64) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar(
        "INSERT INTO facade_caller (client_id, user_id, organization_id)
         SELECT client_id, user_id, organization_id FROM facade_agent WHERE id = $1
         ON CONFLICT (client_id, user_id, organization_id) DO UPDATE SET client_id = EXCLUDED.client_id
         RETURNING id",
    )
    .bind(agent)
    .fetch_one(db)
    .await
}

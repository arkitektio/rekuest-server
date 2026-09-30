//! A blok's dependencies (`facade/mutations/blok.py`, `_sync_blok_dependencies`).

use rekuest_core::inputs::AgentDependencyInputModel;
use serde_json::Value;
use sqlx::PgConnection;

use crate::deletion::delete_blok_dependencies;

fn dump<T: serde::Serialize>(demands: &Option<Vec<T>>) -> Value {
    serde_json::to_value(demands.as_deref().unwrap_or_default()).expect("demands serialize")
}

/// Upsert the blok's dependency rows by `(blok, key)`, every declared field written; with
/// `replace`, the keys no longer declared are deleted. The rows' ids, in declaration order.
pub async fn sync_blok_dependencies(
    conn: &mut PgConnection,
    blok: i64,
    dependencies: &[AgentDependencyInputModel],
    replace: bool,
) -> Result<Vec<(i64, String)>, sqlx::Error> {
    let mut synced = vec![];
    for declared in dependencies {
        let id: i64 = sqlx::query_scalar(
            "INSERT INTO facade_blokdependency
                 (created_at, key, action_demands, state_demands, app_filter, version_filter, optional, description,
                  auto_resolvable, min_viable_instances, max_viable_instances, blok_id)
             VALUES (now(), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $1)
             ON CONFLICT (blok_id, key) DO UPDATE SET
                 action_demands = excluded.action_demands, state_demands = excluded.state_demands,
                 app_filter = excluded.app_filter, version_filter = excluded.version_filter,
                 optional = excluded.optional, description = excluded.description,
                 auto_resolvable = excluded.auto_resolvable, min_viable_instances = excluded.min_viable_instances,
                 max_viable_instances = excluded.max_viable_instances
             RETURNING id",
        )
        .bind(blok)
        .bind(&declared.key)
        .bind(dump(&declared.action_dependencies))
        .bind(dump(&declared.state_dependencies))
        .bind(&declared.app)
        .bind(&declared.version)
        .bind(declared.optional)
        .bind(&declared.description)
        .bind(declared.auto_resolvable)
        .bind(declared.min_viable_instances)
        .bind(declared.max_viable_instances)
        .fetch_one(&mut *conn)
        .await?;
        synced.push((id, declared.key.clone()));
    }
    if replace {
        let keys: Vec<&str> = dependencies.iter().map(|d| d.key.as_str()).collect();
        let stale: Vec<i64> = sqlx::query_scalar(
            "SELECT id FROM facade_blokdependency WHERE blok_id = $1 AND NOT (key = ANY($2))",
        )
        .bind(blok)
        .bind(&keys)
        .fetch_all(&mut *conn)
        .await?;
        delete_blok_dependencies(conn, &stale).await?;
    }
    Ok(synced)
}

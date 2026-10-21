//! The protocols an action is listed under, as rows of its organization (`facade/protocol.py`):
//! the ones inferred from its ports, and the hints its definition declares.

use rekuest_core::inputs::DefinitionInputModel;
use sqlx::PgConnection;

use crate::inference::{is_agent, is_hook, is_predicate};

/// The protocols `definition` implements, each upserted by `(organization, name)`
/// (`infer_protocols`).
pub async fn infer_protocols(
    conn: &mut PgConnection,
    definition: &DefinitionInputModel,
    organization: i64,
) -> Result<Vec<i64>, sqlx::Error> {
    let mut protocols = vec![];
    for (name, description) in [is_predicate, is_hook, is_agent]
        .iter()
        .filter_map(|infer| infer(definition))
    {
        protocols.push(
            sqlx::query_scalar(
                "INSERT INTO facade_protocol (name, description, organization_id) VALUES ($1, $3, $2)
                 ON CONFLICT (organization_id, name) DO UPDATE SET description = excluded.description
                 RETURNING id",
            )
            .bind(name)
            .bind(organization)
            .bind(description)
            .fetch_one(&mut *conn)
            .await?,
        );
    }
    Ok(protocols)
}

/// What a declared hint's row says until an inference names it too.
const DECLARED_DESCRIPTION: &str = "Declared by actions as where they mean to hook in";

/// Every protocol `definition` is listed under: inferred, then declared (`definition.protocols`).
///
/// A declared hint is upserted by name and never overwrites a description an
/// inference wrote: a hint is a name, the inference is the one that knows what it means.
pub async fn action_protocols(
    conn: &mut PgConnection,
    definition: &DefinitionInputModel,
    organization: i64,
) -> Result<Vec<i64>, sqlx::Error> {
    let mut protocols = infer_protocols(conn, definition, organization).await?;
    for name in &definition.protocols {
        let id: i64 = sqlx::query_scalar(
            "INSERT INTO facade_protocol (name, description, organization_id) VALUES ($1, $3, $2)
             ON CONFLICT (organization_id, name) DO UPDATE SET name = excluded.name
             RETURNING id",
        )
        .bind(name)
        .bind(organization)
        .bind(DECLARED_DESCRIPTION)
        .fetch_one(&mut *conn)
        .await?;
        if !protocols.contains(&id) {
            protocols.push(id);
        }
    }
    Ok(protocols)
}

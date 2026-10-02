//! The protocols a definition implements, as rows of its organization (`facade/protocol.py`).

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

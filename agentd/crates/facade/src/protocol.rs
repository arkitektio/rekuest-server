//! The protocols a definition implements, as rows of its organization (`facade/protocol.py`).

use rekuest_core::inputs::DefinitionInputModel;
use sqlx::PgConnection;

use crate::inference::{is_agent, is_hook, is_predicate};

/// The protocols `definition` implements, each upserted by `(name, organization)`
/// (`infer_protocols`).
///
/// `facade_protocol.name` is unique across organizations while the lookup is per organization,
/// as in Python: a second organization's first predicate fails on the constraint. Reproduced, not
/// fixed, so both servers refuse the same registrations.
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
        let existing: Option<i64> = sqlx::query_scalar(
            "UPDATE facade_protocol SET description = $3 WHERE name = $1 AND organization_id = $2 RETURNING id",
        )
        .bind(name)
        .bind(organization)
        .bind(description)
        .fetch_optional(&mut *conn)
        .await?;
        let id = match existing {
            Some(id) => id,
            None => {
                sqlx::query_scalar(
                    "INSERT INTO facade_protocol (name, description, organization_id) VALUES ($1, $3, $2) RETURNING id",
                )
                .bind(name)
                .bind(organization)
                .bind(description)
                .fetch_one(&mut *conn)
                .await?
            }
        };
        protocols.push(id);
    }
    Ok(protocols)
}

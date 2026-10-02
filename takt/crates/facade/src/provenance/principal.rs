//! Human-vs-service classification for the root-is-always-a-human invariant
//! (`facade/provenance/principal.py`).
//!
//! The auth token has no human/service flag, so classification is a configurable predicate over
//! roles: the live token's for the principal making the request, the persisted
//! `Membership.roles` for a stored `Caller`. Without configured `human_roles` everything counts as
//! human (the invariant is opt-in).

use sqlx::PgExecutor;

use crate::settings::ProvenanceSettings;

/// Whether `roles` mark an accountable human (or enforcement is off) (`is_human_by_roles`).
pub fn is_human_by_roles(settings: &ProvenanceSettings, roles: &[String]) -> bool {
    if settings.human_roles.is_empty() {
        return true;
    }
    roles.iter().any(|role| settings.human_roles.contains(role))
}

/// The persisted roles a caller's user holds within its organization (`roles_for_caller`).
pub async fn roles_for_caller(
    executor: impl PgExecutor<'_>,
    caller: i64,
) -> Result<Vec<String>, sqlx::Error> {
    let rows: Vec<Option<serde_json::Value>> = sqlx::query_scalar(
        "SELECT m.roles FROM authentikate_membership m
           JOIN facade_caller c ON c.user_id = m.user_id AND c.organization_id = m.organization_id
          WHERE c.id = $1
          ORDER BY m.id",
    )
    .bind(caller)
    .fetch_all(executor)
    .await?;
    Ok(rows
        .into_iter()
        .flatten()
        .filter_map(|roles| serde_json::from_value::<Vec<String>>(roles).ok())
        .flatten()
        .collect())
}

/// Classify a persisted `Caller` as a human principal (`is_human_caller`).
pub async fn is_human_caller(
    executor: impl PgExecutor<'_>,
    settings: &ProvenanceSettings,
    caller: i64,
) -> Result<bool, sqlx::Error> {
    if settings.human_roles.is_empty() {
        return Ok(true);
    }
    Ok(is_human_by_roles(
        settings,
        &roles_for_caller(executor, caller).await?,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn without_human_roles_everyone_is_human() {
        let mut settings = ProvenanceSettings::default();
        assert!(is_human_by_roles(&settings, &[]));
        settings.human_roles = vec!["human".into()];
        assert!(!is_human_by_roles(&settings, &[]));
        assert!(!is_human_by_roles(&settings, &["bot".into()]));
        assert!(is_human_by_roles(
            &settings,
            &["bot".into(), "human".into()]
        ));
    }
}

//! Expanding tokens into Django's rows, against a database the Python server migrated.
//! Needs `TAKT_TEST_DATABASE_URL` (`eval "$(scripts/test-db.sh)"`); skipped without it.

use authentikate::base_models::StaticToken;
use authentikate::expand::{expand_token_context, token_to_username};
use authentikate::{AuthentikateError, JwtToken};
use chrono::Utc;
use sqlx::PgPool;

async fn db() -> Option<PgPool> {
    let url = std::env::var("TAKT_TEST_DATABASE_URL").ok()?;
    Some(
        PgPool::connect(&url)
            .await
            .expect("the test database answers"),
    )
}

/// A token whose identity no other test uses.
fn token(overrides: serde_json::Value) -> JwtToken {
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let mut spec = serde_json::json!({
        "sub": format!("sub-{unique}"), "iss": "rust-tests", "org": format!("org-{unique}"),
        "client_id": format!("client-{unique}"), "client_app": format!("app-{unique}"),
        "client_release": "1.0", "client_device": format!("device-{unique}"),
        "preferred_username": "ada", "roles": ["researcher"],
    });
    for (key, value) in overrides.as_object().unwrap() {
        spec[key] = value.clone();
    }
    serde_json::from_value::<StaticToken>(spec)
        .unwrap()
        .to_token(Utc::now(), "raw")
}

#[tokio::test]
async fn a_token_becomes_its_rows_and_twice_is_the_same_rows() {
    let Some(db) = db().await else { return };
    let token = token(serde_json::json!({}));

    let first = expand_token_context(&db, &token).await.unwrap();
    let again = expand_token_context(&db, &token).await.unwrap();
    assert_eq!(first, again);

    let (username, first_name, is_active, password, org): (String, String, bool, String, Option<i64>) = sqlx::query_as(
        "SELECT username, first_name, is_active, password, active_organization_id FROM authentikate_user WHERE id = $1",
    )
    .bind(first.user)
    .fetch_one(&db)
    .await
    .unwrap();
    assert_eq!(username, token_to_username(&token));
    assert_eq!(
        first_name, "ada",
        "4.1.1 keeps preferred_username in first_name"
    );
    assert!(
        is_active && password.starts_with('!'),
        "unusable password, like set_unusable_password"
    );
    assert_eq!(org, Some(first.organization));

    let (release, device): (Option<i64>, Option<i64>) =
        sqlx::query_as("SELECT release_id, device_id FROM authentikate_client WHERE id = $1")
            .bind(first.client)
            .fetch_one(&db)
            .await
            .unwrap();
    assert!(release.is_some() && device.is_some());

    let roles: serde_json::Value =
        sqlx::query_scalar("SELECT roles FROM authentikate_membership WHERE id = $1")
            .bind(first.membership)
            .fetch_one(&db)
            .await
            .unwrap();
    assert_eq!(roles, serde_json::json!(["researcher"]));
}

#[tokio::test]
async fn changed_user_metadata_is_synced_and_roles_follow_the_token() {
    let Some(db) = db().await else { return };
    let original = token(serde_json::json!({}));
    let before = expand_token_context(&db, &original).await.unwrap();

    let mut renamed = original.clone();
    renamed.preferred_username = "lovelace".into();
    renamed.roles = vec!["admin".into()];
    let after = expand_token_context(&db, &renamed).await.unwrap();
    assert_eq!(before.user, after.user);

    let (first_name, hash): (String, String) =
        sqlx::query_as("SELECT first_name, changed_hash FROM authentikate_user WHERE id = $1")
            .bind(after.user)
            .fetch_one(&db)
            .await
            .unwrap();
    assert_eq!(
        (first_name.as_str(), hash),
        ("lovelace", renamed.changed_hash())
    );
    let roles: serde_json::Value =
        sqlx::query_scalar("SELECT roles FROM authentikate_membership WHERE id = $1")
            .bind(after.membership)
            .fetch_one(&db)
            .await
            .unwrap();
    assert_eq!(roles, serde_json::json!(["admin"]));
}

#[tokio::test]
async fn a_blocked_membership_and_a_missing_organization_refuse() {
    let Some(db) = db().await else { return };
    let token = token(serde_json::json!({}));
    let context = expand_token_context(&db, &token).await.unwrap();
    sqlx::query("UPDATE authentikate_membership SET blocked = true WHERE id = $1")
        .bind(context.membership)
        .execute(&db)
        .await
        .unwrap();
    assert!(matches!(
        expand_token_context(&db, &token).await,
        Err(AuthentikateError::BlockedMembership)
    ));

    let mut orgless = self::token(serde_json::json!({}));
    orgless.org = None;
    assert!(matches!(
        expand_token_context(&db, &orgless).await,
        Err(AuthentikateError::MissingActiveOrganization)
    ));
}

//! A verified token to its rows (`authentikate/expand.py`, 4.1.1).
//!
//! The organization comes from the `org` claim, the user from `(sub, iss)` (created on first
//! sight, updated when its `changed_hash` moves), the client from `(client_id, iss)` with its
//! app, release and device, and the membership from user and organization, with the token's
//! roles. A blocked membership refuses. Rows are Django's (`authentikate_*`); every column
//! Django fills from a Python-side default is written explicitly, since the database has none.

use chrono::Utc;
use rand::distr::{Alphanumeric, SampleString};
use sha2::{Digest, Sha256};
use sqlx::PgPool;

use crate::base_models::JwtToken;
use crate::errors::AuthentikateError;

const USERNAME_MAX_LENGTH: usize = 150;
const USERNAME_DIGEST_LENGTH: usize = 12;

/// The rows a token stands for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExpandedTokenContext {
    pub user: i64,
    pub client: i64,
    pub organization: i64,
    pub membership: i64,
}

/// A username valid for Django (`[\w.@+-]`, at most 150 characters) and stable for
/// `(iss, sub)`: the sanitized `{iss}_{sub}`, truncated, plus a digest of the original pair.
pub fn token_to_username(token: &JwtToken) -> String {
    let digest = hex::encode(Sha256::digest(format!("{}\0{}", token.iss, token.sub)));
    let digest = &digest[..USERNAME_DIGEST_LENGTH];
    let readable: String = format!("{}_{}", token.iss, token.sub)
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || matches!(c, '_' | '.' | '@' | '+' | '-') {
                c
            } else {
                '-'
            }
        })
        .collect();
    let room = USERNAME_MAX_LENGTH - USERNAME_DIGEST_LENGTH - 1;
    let readable: String = readable.chars().take(room).collect();
    format!("{readable}-{digest}")
}

/// Django's `set_unusable_password()`: `!` and 40 random characters.
fn unusable_password() -> String {
    format!("!{}", Alphanumeric.sample_string(&mut rand::rng(), 40))
}

async fn get_or_create_organization(db: &PgPool, slug: &str) -> Result<i64, AuthentikateError> {
    Ok(sqlx::query_scalar(
        "INSERT INTO authentikate_organization (slug) VALUES ($1)
         ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug RETURNING id",
    )
    .bind(slug)
    .fetch_one(db)
    .await?)
}

/// The organization the token acts in (`aexpand_organization_from_token`).
pub async fn expand_organization(db: &PgPool, token: &JwtToken) -> Result<i64, AuthentikateError> {
    let slug = token
        .org
        .as_deref()
        .filter(|org| !org.is_empty())
        .ok_or(AuthentikateError::MissingActiveOrganization)?;
    get_or_create_organization(db, slug).await
}

/// The user, created on first sight and synced when the token's user metadata changed
/// (`_aexpand_user`). No membership or blocked check here: that is [`expand_token_context`].
pub async fn expand_user(
    db: &PgPool,
    token: &JwtToken,
    organization: i64,
) -> Result<i64, AuthentikateError> {
    let changed_hash = token.changed_hash();
    let existing: Option<(i64, Option<String>)> = sqlx::query_as(
        "SELECT id, changed_hash FROM authentikate_user WHERE sub = $1 AND iss = $2",
    )
    .bind(&token.sub)
    .bind(&token.iss)
    .fetch_optional(db)
    .await?;

    let (id, stored_hash) = match existing {
        Some(row) => row,
        None => {
            let created: Result<i64, sqlx::Error> = sqlx::query_scalar(
                "INSERT INTO authentikate_user
                   (password, is_superuser, username, first_name, last_name, email, is_staff,
                    is_active, date_joined, sub, iss, changed_hash, active_organization_id)
                 VALUES ($1, false, $2, $3, '', '', false, true, $4, $5, $6, $7, $8)
                 RETURNING id",
            )
            .bind(unusable_password())
            .bind(token_to_username(token))
            .bind(&token.preferred_username)
            .bind(Utc::now())
            .bind(&token.sub)
            .bind(&token.iss)
            .bind(&changed_hash)
            .bind(organization)
            .fetch_one(db)
            .await;
            match created {
                Ok(id) => return Ok(id),
                // Lost a concurrent create race: the winner's row is the user.
                Err(sqlx::Error::Database(e)) if e.is_unique_violation() => sqlx::query_as(
                    "SELECT id, changed_hash FROM authentikate_user WHERE sub = $1 AND iss = $2",
                )
                .bind(&token.sub)
                .bind(&token.iss)
                .fetch_one(db)
                .await?,
                Err(e) => return Err(e.into()),
            }
        }
    };

    if stored_hash.as_deref() != Some(changed_hash.as_str()) {
        sqlx::query(
            "UPDATE authentikate_user
                SET first_name = $2, changed_hash = $3, active_organization_id = $4
              WHERE id = $1",
        )
        .bind(id)
        .bind(&token.preferred_username)
        .bind(&changed_hash)
        .bind(organization)
        .execute(db)
        .await?;
    }
    Ok(id)
}

/// The client's release (from `client_app` and `client_release`) and device.
async fn resolve_client_relations(
    db: &PgPool,
    token: &JwtToken,
) -> Result<(Option<i64>, Option<i64>), AuthentikateError> {
    let mut release = None;
    if let (Some(app), Some(version)) = (&token.client_app, &token.client_release) {
        let app_id: i64 = sqlx::query_scalar(
            "INSERT INTO authentikate_app (identifier) VALUES ($1)
             ON CONFLICT (identifier) DO UPDATE SET identifier = EXCLUDED.identifier RETURNING id",
        )
        .bind(app)
        .fetch_one(db)
        .await?;
        release = Some(
            sqlx::query_scalar(
                "INSERT INTO authentikate_release (app_id, version) VALUES ($1, $2)
                 ON CONFLICT (app_id, version) DO UPDATE SET version = EXCLUDED.version RETURNING id",
            )
            .bind(app_id)
            .bind(version)
            .fetch_one(db)
            .await?,
        );
    }
    let mut device = None;
    if let Some(device_id) = &token.client_device {
        device = Some(
            sqlx::query_scalar(
                "INSERT INTO authentikate_device (device_id) VALUES ($1)
                 ON CONFLICT (device_id) DO UPDATE SET device_id = EXCLUDED.device_id RETURNING id",
            )
            .bind(device_id)
            .fetch_one(db)
            .await?,
        );
    }
    Ok((release, device))
}

/// The client the token was issued to (`aexpand_client_from_token`): created with its
/// release and device, and given either later if it was created without.
pub async fn expand_client(db: &PgPool, token: &JwtToken) -> Result<i64, AuthentikateError> {
    let (release, device) = resolve_client_relations(db, token).await?;
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO authentikate_client (client_id, iss, release_id, device_id) VALUES ($1, $2, $3, $4)
         ON CONFLICT (iss, client_id) DO UPDATE SET client_id = EXCLUDED.client_id RETURNING id",
    )
    .bind(&token.client_id)
    .bind(&token.iss)
    .bind(release)
    .bind(device)
    .fetch_one(db)
    .await?;
    sqlx::query(
        "UPDATE authentikate_client
            SET device_id = COALESCE(device_id, $2), release_id = COALESCE(release_id, $3)
          WHERE id = $1",
    )
    .bind(id)
    .bind(device)
    .bind(release)
    .execute(db)
    .await?;
    Ok(id)
}

/// The membership, with the token's roles (`aexpand_membership`). Blocked refuses.
pub async fn expand_membership(
    db: &PgPool,
    user: i64,
    organization: i64,
    token: &JwtToken,
) -> Result<i64, AuthentikateError> {
    let (id, blocked): (i64, bool) = sqlx::query_as(
        "INSERT INTO authentikate_membership (user_id, organization_id, roles, blocked)
         VALUES ($1, $2, $3, false)
         ON CONFLICT (user_id, organization_id) DO UPDATE SET roles = EXCLUDED.roles
         RETURNING id, blocked",
    )
    .bind(user)
    .bind(organization)
    .bind(sqlx::types::Json(&token.roles))
    .fetch_one(db)
    .await?;
    if blocked {
        return Err(AuthentikateError::BlockedMembership);
    }
    Ok(id)
}

/// Everything a token stands for, in the Python order: organization, user, client, membership.
pub async fn expand_token_context(
    db: &PgPool,
    token: &JwtToken,
) -> Result<ExpandedTokenContext, AuthentikateError> {
    let organization = expand_organization(db, token).await?;
    let user = expand_user(db, token, organization).await?;
    let client = expand_client(db, token).await?;
    let membership = expand_membership(db, user, organization, token).await?;
    Ok(ExpandedTokenContext {
        user,
        client,
        organization,
        membership,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::base_models::StaticToken;

    fn token(iss: &str, sub: &str) -> JwtToken {
        let spec: StaticToken =
            serde_json::from_value(serde_json::json!({"sub": sub, "iss": iss})).unwrap();
        spec.to_token(Utc::now(), "raw")
    }

    #[test]
    fn usernames_are_sanitized_and_carry_the_pairs_digest() {
        // python: token_to_username for iss="https://lok.example/o", sub="42"
        let name = token_to_username(&token("https://lok.example/o", "42"));
        let digest = &hex::encode(Sha256::digest(b"https://lok.example/o\x0042"))[..12];
        assert_eq!(name, format!("https---lok.example-o_42-{digest}"));

        let long = token_to_username(&token(&"x".repeat(300), "1"));
        assert_eq!(long.chars().count(), 150);
    }
}

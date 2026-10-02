//! The identity of whoever originates a task, transport-independent (`facade/caller_context.py`).
//!
//! GraphQL builds it from the request, the agent socket from the registered agent, the internal
//! API from the principal block its caller sends. The backend never has to know which.

use sqlx::PgExecutor;

/// Who is originating work: the user, client and organization (their primary keys, and what the
/// wire carries of them) plus their roles.
#[derive(Debug, Clone, PartialEq)]
pub struct CallerContext {
    pub user: i64,
    /// `user.sub`: the Assign's `user`, the token's `sub`.
    pub user_sub: String,
    pub client: i64,
    pub organization: Option<i64>,
    /// `organization.slug`: the Assign's `org`.
    pub organization_slug: Option<String>,
    pub roles: Vec<String>,
}

#[derive(sqlx::FromRow)]
struct IdentityRow {
    user_id: i64,
    sub: String,
    client_id: i64,
    organization_id: Option<i64>,
    slug: Option<String>,
}

impl CallerContext {
    fn from_row(row: IdentityRow, roles: Vec<String>) -> Self {
        Self {
            user: row.user_id,
            user_sub: row.sub,
            client: row.client_id,
            organization: row.organization_id,
            organization_slug: row.slug,
            roles,
        }
    }

    /// Build from a registered agent (`from_agent`).
    pub async fn from_agent(
        executor: impl PgExecutor<'_>,
        agent: i64,
        roles: Vec<String>,
    ) -> Result<Self, sqlx::Error> {
        let row: IdentityRow = sqlx::query_as(
            "SELECT a.user_id, u.sub, a.client_id, a.organization_id, o.slug
               FROM facade_agent a
               JOIN authentikate_user u ON u.id = a.user_id
               LEFT JOIN authentikate_organization o ON o.id = a.organization_id
              WHERE a.id = $1",
        )
        .bind(agent)
        .fetch_one(executor)
        .await?;
        Ok(Self::from_row(row, roles))
    }

    /// Build from the primary keys of a user, client and organization (the internal API's
    /// principal, which the Python server resolved from its GraphQL request).
    pub async fn load(
        executor: impl PgExecutor<'_>,
        user: i64,
        client: i64,
        organization: Option<i64>,
        roles: Vec<String>,
    ) -> Result<Self, sqlx::Error> {
        let row: IdentityRow = sqlx::query_as(
            "SELECT u.id AS user_id, u.sub, c.id AS client_id, o.id AS organization_id, o.slug
               FROM authentikate_user u
               JOIN authentikate_client c ON c.id = $2
               LEFT JOIN authentikate_organization o ON o.id = $3
              WHERE u.id = $1",
        )
        .bind(user)
        .bind(client)
        .bind(organization)
        .fetch_one(executor)
        .await?;
        Ok(Self::from_row(row, roles))
    }
}

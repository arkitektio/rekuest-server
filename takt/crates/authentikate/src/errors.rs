//! What authenticating can fail with (`authentikate/errors.py`).

#[derive(Debug, thiserror::Error)]
pub enum AuthentikateError {
    /// The token is not a JWT we can read (no `kid`, no `iss`, a payload that is no token).
    #[error("malformed token: {0}")]
    MalformedJwtToken(String),
    /// Untrusted issuer, bad signature, or claims that do not hold.
    #[error("invalid token: {0}")]
    InvalidJwtToken(String),
    #[error("token has expired")]
    TokenExpired,
    #[error("token has been revoked")]
    TokenRevoked,
    #[error("organization {0:?} is not accepted by this service")]
    OrganizationNotAllowed(Option<String>),
    #[error("token does not contain an active organization")]
    MissingActiveOrganization,
    #[error("membership is blocked")]
    BlockedMembership,
    /// Key retrieval failed (issuer unreachable, malformed JWKS): not a permission
    /// decision, but still not authenticated.
    #[error("could not retrieve keys: {0}")]
    Jwks(String),
    #[error("database: {0}")]
    Database(#[from] sqlx::Error),
}

impl AuthentikateError {
    /// Whether this is a refusal of the token (as opposed to an infrastructure fault).
    pub fn is_permission_denied(&self) -> bool {
        !matches!(
            self,
            AuthentikateError::Jwks(_) | AuthentikateError::Database(_)
        )
    }
}

/// Settings that cannot be used (`ImproperlyConfigured` on the Python side).
#[derive(Debug, thiserror::Error)]
#[error("invalid settings for AUTHENTIKATE: {0}")]
pub struct ImproperlyConfigured(pub String);

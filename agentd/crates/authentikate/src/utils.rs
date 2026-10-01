//! Authenticating a token (`authentikate/utils.py`).

use chrono::Utc;

use crate::base_models::{AuthentikateSettings, JwtToken};
use crate::decode::Verifier;
use crate::errors::AuthentikateError;

/// Refuse a token naming an organization this service does not accept. Unset accepts all.
fn check_organization_allowed(
    token: &JwtToken,
    settings: &AuthentikateSettings,
) -> Result<(), AuthentikateError> {
    match &settings.allowed_organizations {
        Some(allowed) if !token.org.as_ref().is_some_and(|org| allowed.contains(org)) => {
            Err(AuthentikateError::OrganizationNotAllowed(token.org.clone()))
        }
        _ => Ok(()),
    }
}

/// The verified token behind `token`: a static token (whose expiry still holds) or a signed
/// JWT. Either way, its organization must be allowed.
pub async fn authenticate_token(
    verifier: &Verifier,
    token: &str,
) -> Result<JwtToken, AuthentikateError> {
    let settings = &verifier.settings;
    let decoded = match settings.static_tokens.get(token) {
        Some(static_token) => {
            let decoded = static_token.to_token(settings.loaded_at, token);
            if decoded.exp <= Utc::now() {
                return Err(AuthentikateError::TokenExpired);
            }
            decoded
        }
        None => verifier.decode(token).await?,
    };
    check_organization_allowed(&decoded, settings)?;
    Ok(decoded)
}

/// [`authenticate_token`], with every refusal (and a failed key fetch) as `None`.
pub async fn authenticate_token_or_none(verifier: &Verifier, token: &str) -> Option<JwtToken> {
    match authenticate_token(verifier, token).await {
        Ok(token) => Some(token),
        Err(AuthentikateError::Jwks(e)) => {
            tracing::warn!("Could not retrieve JWKS to verify token: {e}");
            None
        }
        Err(_) => None,
    }
}

/// The token of an `Authorization: Bearer <token>` header.
pub fn extract_plain_from_authorization(authorization: &str) -> Option<&str> {
    let rest = authorization.strip_prefix("Bearer")?;
    let token = rest.strip_prefix(char::is_whitespace)?;
    Some(token.split_whitespace().next().unwrap_or(""))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bearer_headers_are_read_like_the_python_regex() {
        assert_eq!(extract_plain_from_authorization("Bearer abc"), Some("abc"));
        assert_eq!(
            extract_plain_from_authorization("Bearer abc def"),
            Some("abc")
        );
        assert_eq!(extract_plain_from_authorization("Bearerabc"), None);
        assert_eq!(extract_plain_from_authorization("Basic abc"), None);
    }

    #[tokio::test]
    async fn static_tokens_authenticate_and_the_allow_list_applies() {
        let settings = AuthentikateSettings::prepare(
            &serde_json::json!({
                "audience": "rekuest",
                "static_tokens": {"t": {"sub": "1", "org": "lab"}, "other": {"sub": "2", "org": "elsewhere"}},
                "allowed_organizations": ["lab"],
            }),
            true,
        )
        .unwrap();
        let verifier = Verifier::new(settings);
        let token = authenticate_token(&verifier, "t").await.unwrap();
        assert_eq!(
            (token.sub.as_str(), token.org.as_deref()),
            ("1", Some("lab"))
        );
        assert!(matches!(
            authenticate_token(&verifier, "other").await,
            Err(AuthentikateError::OrganizationNotAllowed(_))
        ));
        assert!(authenticate_token_or_none(&verifier, "not-a-token")
            .await
            .is_none());
    }
}

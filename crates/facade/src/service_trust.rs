//! Service tokens (`facade/service_trust.py` over the vendored `rekuest_service.trust`).
//!
//! A request between services carries a short-lived JWT signed with the sender's instance key:
//!
//! ```text
//! Authorization: RekuestService <jwt>
//! header  {alg: Ed25519, kid: <RFC 7638 thumbprint>, typ: rekuest-service+jwt}
//! claims  {iss, aud, iat, exp (60 s), jti, htm: <METHOD>, htu: <path>, bh: <b64url sha256(body)>}
//! ```
//!
//! agentd verifies only one sender: the rekuest server it runs beside, which shares its
//! `config.yaml` and so its instance key. So the trust bundle is that one key: `kid` must be its
//! thumbprint, and `iss` and `aud` both the rekuest identifier. Replay beyond the time window is
//! refused by claiming the `jti` in redis.

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::context::Context;
use crate::provenance::keys::{segment, InstanceKey, ALGORITHM};
use crate::redis_keys;

pub const SCHEME: &str = "RekuestService";
pub const TYP: &str = "rekuest-service+jwt";
pub const LIFETIME_SECONDS: i64 = 60;
pub const MAX_SKEW_SECONDS: i64 = 30;

/// A request that does not prove who sent it; the message says why and is safe to log.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
#[error("{0}")]
pub struct TrustError(pub String);

fn refuse<T>(why: impl Into<String>) -> Result<T, TrustError> {
    Err(TrustError(why.into()))
}

/// Who sent a request that verified.
#[derive(Debug, Clone, PartialEq)]
pub struct Verified {
    pub issuer: String,
    pub jti: String,
    pub expires_at: i64,
}

/// `body_hash`: base64url SHA-256 of the body, unpadded.
pub fn body_hash(body: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(Sha256::digest(body))
}

/// The `Authorization` value of a request from `issuer` to `audience` (`sign`).
pub fn sign(
    key: &InstanceKey,
    method: &str,
    path: &str,
    body: &[u8],
    issuer: &str,
    audience: &str,
) -> String {
    let now = chrono::Utc::now().timestamp();
    let claims = json!({
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + LIFETIME_SECONDS,
        "jti": uuid::Uuid::new_v4().simple().to_string(),
        "htm": method.to_uppercase(),
        "htu": path,
        "bh": body_hash(body),
    });
    let header = json!({"alg": ALGORITHM, "kid": key.kid(), "typ": TYP});
    format!("{SCHEME} {}", key.sign_jwt(&header, &claims))
}

/// Check a request's service token against this instance's key (`verify`, `verify_from`).
pub fn verify(
    key: &InstanceKey,
    method: &str,
    path: &str,
    body: &[u8],
    authorization: Option<&str>,
    identifier: &str,
) -> Result<Verified, TrustError> {
    let Some(token) = authorization.and_then(|a| a.strip_prefix(&format!("{SCHEME} "))) else {
        return refuse("No service token");
    };
    let token = token.trim();
    let kid = token
        .split('.')
        .next()
        .and_then(|header| segment(header).ok())
        .and_then(|header| header.get("kid").and_then(Value::as_str).map(str::to_owned));
    let Some(kid) = kid else {
        return refuse("Service token without a key id");
    };
    if kid != key.kid() {
        return refuse(format!("No key {kid} in the hub's trust bundle"));
    }
    let (header, claims) = key
        .verify_jwt(token)
        .map_err(|e| TrustError(format!("Bad service token signature: {e}")))?;
    if header.get("alg").and_then(Value::as_str) != Some(ALGORITHM) {
        return refuse("Bad service token signature: not Ed25519");
    }
    if header.get("typ").and_then(Value::as_str) != Some(TYP) {
        return refuse("Not a service token");
    }
    let issuer = claims
        .get("iss")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if issuer != identifier {
        return refuse(format!(
            "Key {kid} belongs to '{identifier}', not to '{issuer}'"
        ));
    }
    let audience = claims
        .get("aud")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if audience != identifier {
        return refuse(format!("Token is for '{audience}', not '{identifier}'"));
    }
    let now = chrono::Utc::now().timestamp();
    let (Some(exp), Some(iat)) = (
        claims.get("exp").and_then(Value::as_i64),
        claims.get("iat").and_then(Value::as_i64),
    ) else {
        return refuse("Service token expired or not yet valid");
    };
    if exp < now - MAX_SKEW_SECONDS
        || iat > now + MAX_SKEW_SECONDS
        || exp - iat > LIFETIME_SECONDS + MAX_SKEW_SECONDS
    {
        return refuse("Service token expired or not yet valid");
    }
    if claims.get("htm").and_then(Value::as_str) != Some(&method.to_uppercase())
        || claims.get("htu").and_then(Value::as_str) != Some(path)
    {
        return refuse("Service token was signed for another request");
    }
    if claims.get("bh").and_then(Value::as_str) != Some(&body_hash(body)) {
        return refuse("Service token was signed for another body");
    }
    let jti = match claims.get("jti") {
        Some(Value::String(jti)) if !jti.is_empty() => jti.clone(),
        _ => return refuse("Service token without an id"),
    };
    Ok(Verified {
        issuer: issuer.to_owned(),
        jti,
        expires_at: exp,
    })
}

/// Claim a verified token's `jti` once, across every replica: false for a replay. Held until the
/// token could no longer verify anyway.
pub async fn claim(ctx: &Context, verified: &Verified) -> redis::RedisResult<bool> {
    let key = redis_keys::key(&ctx.settings, &[&"service-jti", &verified.jti]);
    let ttl = (verified.expires_at + MAX_SKEW_SECONDS - chrono::Utc::now().timestamp()).max(1);
    let mut redis = ctx.redis.clone();
    let set: Option<String> = redis::cmd("SET")
        .arg(&key)
        .arg(1)
        .arg("NX")
        .arg("EX")
        .arg(ttl)
        .query_async(&mut redis)
        .await?;
    Ok(set.is_some())
}

#[cfg(test)]
mod tests {
    use super::*;

    const PEM: &str = "-----BEGIN PRIVATE KEY-----\n\
        MC4CAQAwBQYDK2VwBCIEILK+rl9gVEjfKGiye+mLLjfEUGIdoP0WPC8lMZS3NYK2\n\
        -----END PRIVATE KEY-----\n";
    const ME: &str = "live.arkitekt.rekuest";

    #[test]
    fn a_signed_request_verifies_only_as_signed() {
        let key = InstanceKey::from_pem(PEM).unwrap();
        let header = sign(&key, "post", "/rekuest/internal/assign", b"{}", ME, ME);
        let verified = verify(
            &key,
            "POST",
            "/rekuest/internal/assign",
            b"{}",
            Some(&header),
            ME,
        )
        .unwrap();
        assert_eq!(verified.issuer, ME);

        let refused = |method, path, body: &[u8], header: &str| {
            verify(&key, method, path, body, Some(header), ME)
                .unwrap_err()
                .0
        };
        assert_eq!(
            refused("POST", "/internal/assign", b"{}", &header),
            "Service token was signed for another request"
        );
        assert_eq!(
            refused("POST", "/rekuest/internal/assign", b"{\"a\":1}", &header),
            "Service token was signed for another body"
        );
        let other = sign(&key, "POST", "/p", b"", "live.arkitekt.mikro", ME);
        assert!(refused("POST", "/p", b"", &other).starts_with("Key "));
        assert_eq!(
            verify(&key, "POST", "/p", b"", Some("Bearer x"), ME)
                .unwrap_err()
                .0,
            "No service token"
        );
    }
}

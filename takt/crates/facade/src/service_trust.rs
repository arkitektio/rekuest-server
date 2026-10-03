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
//! takt verifies only one sender: the rekuest server it runs beside, which shares its
//! `config.yaml` and so its instance key. So the trust bundle is that one key: `kid` must be its
//! thumbprint, and `iss` and `aud` both the rekuest identifier. Replay beyond the time window is
//! refused by claiming the `jti` in redis.
//!
//! The hub's configured instances (its services, its hook agents) are checked against the hub's
//! trust bundle instead: the coord-vouched public keys of every instance, each listed under its
//! service's identifier ([`TrustBundle`], [`verify_from`]); deliveries to them are signed with
//! this instance's key ([`sign_to`]).

use std::collections::HashMap;
use std::time::{Duration, Instant};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::context::Context;
use crate::provenance::keys::{segment, InstanceKey, ALGORITHM};
use crate::redis_keys;
use crate::settings::{Settings, TrustedInstance};

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
    let (jti, exp) = check_request_claims(&claims, method, path, body)?;
    Ok(Verified {
        issuer: issuer.to_owned(),
        jti,
        expires_at: exp,
    })
}

/// The request-binding half of `verify`, shared by both trust roots: the lifetime, the method
/// and path, the body, the id. `(jti, exp)`.
fn check_request_claims(
    claims: &Value,
    method: &str,
    path: &str,
    body: &[u8],
) -> Result<(String, i64), TrustError> {
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
    match claims.get("jti") {
        Some(Value::String(jti)) if !jti.is_empty() => Ok((jti.clone(), exp)),
        _ => refuse("Service token without an id"),
    }
}

/// How long a fetched bundle is trusted before it is fetched again (`BUNDLE_TTL_SECONDS`).
pub const BUNDLE_TTL: Duration = Duration::from_secs(300);
/// An unknown `kid` triggers a refetch at most this often (`REFETCH_MIN_SECONDS`).
pub const REFETCH_MIN: Duration = Duration::from_secs(60);

#[derive(Debug, Default)]
struct BundleState {
    keys: HashMap<String, Value>,
    fetched_at: Option<Instant>,
    last_attempt: Option<Instant>,
}

/// The hub's instance public keys, each listed with its `service` (`TrustBundle`): inline, or
/// fetched from the coord and cached. A failed fetch keeps the last bundle: a coord outage must
/// not drop trust.
#[derive(Debug, Default)]
pub struct TrustBundle {
    uri: Option<String>,
    state: tokio::sync::Mutex<BundleState>,
}

fn load_keys(jwks: &Value) -> HashMap<String, Value> {
    jwks.get("keys")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|jwk| {
            jwk.get("kty").and_then(Value::as_str) == Some("OKP")
                && jwk.get("crv").and_then(Value::as_str) == Some("Ed25519")
        })
        .filter_map(|jwk| {
            Some((
                jwk.get("kid")?
                    .as_str()
                    .filter(|k| !k.is_empty())?
                    .to_owned(),
                jwk.clone(),
            ))
        })
        .collect()
}

impl TrustBundle {
    pub fn new(uri: Option<String>, inline: Option<&Value>) -> Self {
        Self {
            uri: uri.filter(|u| !u.is_empty()),
            state: tokio::sync::Mutex::new(BundleState {
                keys: inline.map(load_keys).unwrap_or_default(),
                ..BundleState::default()
            }),
        }
    }

    /// The JWK for `kid`, or `None` when the bundle does not vouch for it.
    pub async fn get(&self, kid: &str) -> Option<Value> {
        let mut state = self.state.lock().await;
        if let Some(uri) = &self.uri {
            let stale = state.fetched_at.is_none_or(|at| at.elapsed() > BUNDLE_TTL);
            let missing = !state.keys.contains_key(kid)
                && state
                    .last_attempt
                    .is_none_or(|at| at.elapsed() > REFETCH_MIN);
            if stale || missing {
                state.last_attempt = Some(Instant::now());
                match fetch_bundle(uri).await {
                    Ok(jwks) => {
                        state.keys = load_keys(&jwks);
                        state.fetched_at = Some(Instant::now());
                    }
                    Err(e) => {
                        tracing::warn!("Could not fetch the hub trust bundle from {uri}: {e}")
                    }
                }
            }
        }
        state.keys.get(kid).cloned()
    }
}

async fn fetch_bundle(uri: &str) -> Result<Value, reqwest::Error> {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()?
        .get(uri)
        .send()
        .await?
        .error_for_status()?
        .json()
        .await
}

/// A compact JWS verified against a public Ed25519 JWK.
fn verify_with_jwk(token: &str, jwk: &Value) -> Result<(Value, Value), String> {
    let x = jwk
        .get("x")
        .and_then(Value::as_str)
        .ok_or("the key has no x")?;
    let public = URL_SAFE_NO_PAD
        .decode(x)
        .map_err(|_| "the key is not base64url".to_owned())?;
    let mut parts = token.split('.');
    let (Some(header), Some(claims), Some(signature), None) =
        (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return Err("not a compact JWS".into());
    };
    let signature = URL_SAFE_NO_PAD
        .decode(signature)
        .map_err(|_| "the signature is not base64url".to_owned())?;
    ring::signature::UnparsedPublicKey::new(&ring::signature::ED25519, &public)
        .verify(format!("{header}.{claims}").as_bytes(), &signature)
        .map_err(|_| "bad signature".to_owned())?;
    Ok((segment(header)?, segment(claims)?))
}

/// Check a request's service token against the hub's trust bundle (`trust.verify`): the key
/// must be listed, and listed for the service that claims to have signed.
pub async fn verify_with_bundle(
    bundle: &TrustBundle,
    method: &str,
    path: &str,
    body: &[u8],
    authorization: Option<&str>,
    audience: &str,
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
    let Some(kid) = kid.filter(|k| !k.is_empty()) else {
        return refuse("Service token without a key id");
    };
    let Some(jwk) = bundle.get(&kid).await else {
        return refuse(format!("No key {kid} in the hub's trust bundle"));
    };
    let (header, claims) = verify_with_jwk(token, &jwk)
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
        .filter(|i| !i.is_empty());
    let service = jwk.get("service").and_then(Value::as_str);
    let Some(issuer) = issuer.filter(|issuer| service == Some(*issuer)) else {
        return refuse(format!(
            "Key {kid} belongs to {}, not to {}",
            py_str(service),
            py_str(issuer)
        ));
    };
    let aud = claims.get("aud").and_then(Value::as_str);
    if aud != Some(audience) {
        return refuse(format!("Token is for {}, not '{audience}'", py_str(aud)));
    }
    let (jti, exp) = check_request_claims(&claims, method, path, body)?;
    Ok(Verified {
        issuer: issuer.to_owned(),
        jti,
        expires_at: exp,
    })
}

fn py_str(value: Option<&str>) -> String {
    value.map_or_else(|| "None".to_owned(), |v| format!("'{v}'"))
}

/// Configured hook agents' clients are minted by rekuest as `rekuest:hook-<name>`
/// (`HOOK_CLIENT_PREFIX`): that is how an agent is recognised as one.
pub const HOOK_CLIENT_PREFIX: &str = "rekuest:hook-";

/// The identifier an entry's instance signs as (`identifier_of`).
pub fn identifier_of(entry: &TrustedInstance) -> String {
    entry
        .identifier
        .clone()
        .unwrap_or_else(|| format!("live.arkitekt.{}", entry.name))
}

/// The `HOOK_AGENTS` entry of the agent whose client is `client_id`; `None` for any other
/// agent (`hook_agent_entry_for`).
pub fn hook_agent_for_client<'a>(
    settings: &'a Settings,
    client_id: &str,
) -> Option<&'a TrustedInstance> {
    let name = client_id.strip_prefix(HOOK_CLIENT_PREFIX)?;
    settings.hook_agents.iter().find(|entry| entry.name == name)
}

/// The `SERVICES` entry named `service` (`service_entry`).
pub fn entry_for_service<'a>(settings: &'a Settings, service: &str) -> Option<&'a TrustedInstance> {
    settings.services.iter().find(|entry| entry.name == service)
}

/// The `Authorization` of a request from rekuest to a service (`sign_to`).
pub fn sign_to(
    settings: &Settings,
    entry: &TrustedInstance,
    url: &str,
    body: &[u8],
) -> Result<String, TrustError> {
    let Some(key) = settings.instance_key.as_deref() else {
        return refuse("No instance key configured (settings.INSTANCE['PRIVATE_KEY'])");
    };
    let path = reqwest::Url::parse(url)
        .map(|u| u.path().to_owned())
        .unwrap_or_default();
    Ok(sign(
        key,
        "POST",
        &path,
        body,
        &settings.rekuest_identifier,
        &identifier_of(entry),
    ))
}

/// Check a request claiming to come from `entry`'s service (`verify_from`).
pub async fn verify_from(
    settings: &Settings,
    entry: &TrustedInstance,
    method: &str,
    path: &str,
    body: &[u8],
    authorization: Option<&str>,
) -> Result<Verified, TrustError> {
    let verified = verify_with_bundle(
        &settings.trust_bundle,
        method,
        path,
        body,
        authorization,
        &settings.rekuest_identifier,
    )
    .await?;
    let expected = identifier_of(entry);
    if verified.issuer != expected {
        return refuse(format!("Signed by {}, not by {expected}", verified.issuer));
    }
    Ok(verified)
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

    #[tokio::test]
    async fn a_service_is_trusted_only_under_its_own_name() {
        let key = InstanceKey::from_pem(PEM).unwrap();
        let mut jwk = key.public_jwk();
        jwk["service"] = json!("live.arkitekt.mikro");
        let bundle = TrustBundle::new(None, Some(&json!({"keys": [jwk]})));
        let mikro = sign(
            &key,
            "POST",
            "/rekuest/agi/http/7",
            b"{}",
            "live.arkitekt.mikro",
            ME,
        );
        let verified = verify_with_bundle(
            &bundle,
            "POST",
            "/rekuest/agi/http/7",
            b"{}",
            Some(&mikro),
            ME,
        )
        .await
        .unwrap();
        assert_eq!(verified.issuer, "live.arkitekt.mikro");

        let posing = sign(
            &key,
            "POST",
            "/rekuest/agi/http/7",
            b"{}",
            "live.arkitekt.kabinet",
            ME,
        );
        let refused = verify_with_bundle(
            &bundle,
            "POST",
            "/rekuest/agi/http/7",
            b"{}",
            Some(&posing),
            ME,
        )
        .await
        .unwrap_err();
        assert_eq!(
            refused.0,
            format!(
                "Key {} belongs to 'live.arkitekt.mikro', not to 'live.arkitekt.kabinet'",
                key.kid()
            )
        );

        let empty = TrustBundle::default();
        let unknown = verify_with_bundle(
            &empty,
            "POST",
            "/rekuest/agi/http/7",
            b"{}",
            Some(&mikro),
            ME,
        )
        .await
        .unwrap_err();
        assert_eq!(
            unknown.0,
            format!("No key {} in the hub's trust bundle", key.kid())
        );
    }
}

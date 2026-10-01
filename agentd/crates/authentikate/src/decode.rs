//! Decoding and verifying a JWT (`authentikate/decode.py`), with the issuers' key caches.
//!
//! The token's `iss` claim selects which configured issuer's keys verify the signature, so a
//! token can never be verified with another issuer's key. Nothing read before verification is
//! trusted: `iss` and `kid` only pick the key, and an `iss` naming no configured issuer is
//! refused before any key is fetched. Then the registered claims are validated: `exp` always,
//! `iss` against the selected issuer, `aud` against this service (`*` still requires one).

use std::collections::HashMap;
use std::str::FromStr;
use std::time::{Duration, Instant};

use base64::Engine;
use jsonwebtoken::{jwk::Jwk, Algorithm, DecodingKey, Validation};
use serde_json::Value;
use tokio::sync::Mutex;

use crate::base_models::{AuthentikateSettings, Issuer, JwtToken, KeySource, ANY_AUDIENCE};
use crate::errors::AuthentikateError;
use crate::revocation::RevocationList;

/// The runtime half of the settings: fetched keys and revocation lists, per issuer.
pub struct Verifier {
    pub settings: AuthentikateSettings,
    client: reqwest::Client,
    jwks: Vec<Mutex<JwksCache>>,
    revocations: Vec<Option<RevocationList>>,
}

#[derive(Default)]
struct JwksCache {
    keys: Option<Vec<Value>>,
    last_refresh: Option<Instant>,
    last_failed_load: Option<Instant>,
}

fn segment(token: &str, index: usize, what: &str) -> Result<Value, AuthentikateError> {
    let malformed = || AuthentikateError::MalformedJwtToken(format!("Error decoding token {what}"));
    let raw = token.split('.').nth(index).ok_or_else(malformed)?;
    let bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(raw.trim_end_matches('='))
        .map_err(|_| malformed())?;
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| malformed())?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(AuthentikateError::MalformedJwtToken(format!(
            "Token {what} is not an object"
        )))
    }
}

/// The `iss` claim and `kid` header that select the verification key, read unverified.
pub fn select_key_hints(token: &str) -> Result<(String, String), AuthentikateError> {
    let kid = segment(token, 0, "header")?
        .get("kid")
        .and_then(Value::as_str)
        .filter(|kid| !kid.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| AuthentikateError::MalformedJwtToken("Missing kid in header".into()))?;
    let iss = segment(token, 1, "payload")?
        .get("iss")
        .and_then(Value::as_str)
        .filter(|iss| !iss.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| AuthentikateError::MalformedJwtToken("Missing iss claim in token".into()))?;
    Ok((iss, kid))
}

/// An RSA public key in OpenSSH form (`ssh-rsa AAAA… comment`): its modulus and exponent.
fn openssh_rsa(public_key: &str) -> Option<DecodingKey> {
    let blob = base64::engine::general_purpose::STANDARD
        .decode(public_key.split_whitespace().nth(1)?)
        .ok()?;
    let mut rest = blob.as_slice();
    let mut next = || -> Option<&[u8]> {
        let len = u32::from_be_bytes(rest.get(..4)?.try_into().ok()?) as usize;
        let (field, tail) = rest.get(4..)?.split_at_checked(len)?;
        rest = tail;
        Some(field)
    };
    if next()? != b"ssh-rsa" {
        return None;
    }
    let exponent = next()?.to_vec();
    let modulus = next()?.to_vec();
    let strip = |bytes: &[u8]| {
        bytes
            .iter()
            .skip_while(|b| **b == 0)
            .copied()
            .collect::<Vec<u8>>()
    };
    Some(DecodingKey::from_rsa_raw_components(
        &strip(&modulus),
        &strip(&exponent),
    ))
}

fn rsa_key(public_key: &str) -> Result<DecodingKey, AuthentikateError> {
    let text = public_key.trim();
    if text.starts_with("ssh-rsa") {
        return openssh_rsa(text)
            .ok_or_else(|| AuthentikateError::Jwks("unreadable ssh-rsa public key".into()));
    }
    DecodingKey::from_rsa_pem(text.as_bytes())
        .map_err(|e| AuthentikateError::Jwks(format!("unreadable RSA public key: {e}")))
}

/// Index one issuer's keys by `kid`: unique within the issuer, and at least one.
fn index_jwks(
    iss: &str,
    keys: &[Value],
) -> Result<HashMap<String, DecodingKey>, AuthentikateError> {
    let mut indexed = HashMap::new();
    for key in keys {
        let kid = key.get("kid").and_then(Value::as_str).ok_or_else(|| {
            AuthentikateError::Jwks(format!("key of issuer {iss:?} must contain a kid field"))
        })?;
        if indexed.contains_key(kid) {
            return Err(AuthentikateError::Jwks(format!(
                "Duplicate kid {kid:?} in jwks of issuer {iss:?}"
            )));
        }
        let jwk: Jwk = serde_json::from_value(key.clone())
            .map_err(|e| AuthentikateError::Jwks(format!("unreadable key {kid:?}: {e}")))?;
        let decoding = DecodingKey::from_jwk(&jwk)
            .map_err(|e| AuthentikateError::Jwks(format!("unusable key {kid:?}: {e}")))?;
        indexed.insert(kid.to_owned(), decoding);
    }
    if indexed.is_empty() {
        return Err(AuthentikateError::Jwks(format!(
            "No keys found in jwks of issuer {iss:?}"
        )));
    }
    Ok(indexed)
}

/// `Ed25519` is RFC 9864's name for what jsonwebtoken calls `EdDSA`.
fn algorithm(name: &str) -> Option<Algorithm> {
    match name {
        "Ed25519" => Some(Algorithm::EdDSA),
        other => Algorithm::from_str(other).ok(),
    }
}

impl Verifier {
    pub fn new(settings: AuthentikateSettings) -> Self {
        let jwks = settings.issuers.iter().map(|_| Mutex::default()).collect();
        let revocations = settings
            .issuers
            .iter()
            .map(|issuer| {
                issuer.revocation_uri.as_deref().map(|uri| {
                    RevocationList::new(
                        uri,
                        issuer.revocation_refresh_interval,
                        issuer.revocation_request_timeout,
                    )
                })
            })
            .collect();
        Self {
            settings,
            client: reqwest::Client::new(),
            jwks,
            revocations,
        }
    }

    async fn fetch(&self, uri: &str, timeout: f64) -> Result<Vec<Value>, AuthentikateError> {
        let fail =
            |e: String| AuthentikateError::Jwks(format!("Error fetching jwks from {uri}: {e}"));
        let response = self
            .client
            .get(uri)
            .timeout(Duration::from_secs_f64(timeout.max(0.0)))
            .send()
            .await
            .and_then(reqwest::Response::error_for_status)
            .map_err(|e| fail(e.to_string()))?;
        let document: Value = response.json().await.map_err(|e| fail(e.to_string()))?;
        document
            .get("keys")
            .and_then(Value::as_array)
            .cloned()
            .ok_or_else(|| fail("no keys list".into()))
    }

    /// The keys of `issuer`, fetched (and throttled on failure) for a remote JWKS. With
    /// `refresh`, a remote JWKS is fetched again unless it was within `min_refresh_interval`.
    async fn keys(
        &self,
        index: usize,
        issuer: &Issuer,
        refresh: bool,
    ) -> Result<HashMap<String, DecodingKey>, AuthentikateError> {
        match &issuer.keys {
            KeySource::Rsa { key_id, public_key } => {
                Ok(HashMap::from([(key_id.clone(), rsa_key(public_key)?)]))
            }
            KeySource::RsaFile {
                key_id,
                public_key_pem_file,
            } => {
                let pem = std::fs::read_to_string(public_key_pem_file)
                    .map_err(|e| AuthentikateError::Jwks(format!("{public_key_pem_file}: {e}")))?;
                Ok(HashMap::from([(key_id.clone(), rsa_key(&pem)?)]))
            }
            KeySource::JwksDict { jwks } => {
                let keys = jwks.get("keys").and_then(Value::as_array).ok_or_else(|| {
                    AuthentikateError::Jwks("jwks_dict must contain a keys list".into())
                })?;
                index_jwks(&issuer.iss, keys)
            }
            KeySource::JwksUri {
                jwks_uri,
                min_refresh_interval,
                request_timeout,
            } => {
                let min_interval = Duration::from_secs_f64(min_refresh_interval.max(0.0));
                let mut cache = self.jwks[index].lock().await;
                if cache.keys.is_none() {
                    if let Some(failed) = cache.last_failed_load {
                        if failed.elapsed() < min_interval {
                            return Err(AuthentikateError::Jwks(format!(
                                "Not retrying the JWKS load from {jwks_uri}: the previous attempt failed {:.1}s ago",
                                failed.elapsed().as_secs_f64()
                            )));
                        }
                    }
                    match self.fetch(jwks_uri, *request_timeout).await {
                        Ok(keys) => {
                            cache.keys = Some(keys);
                            cache.last_failed_load = None;
                        }
                        Err(e) => {
                            cache.last_failed_load = Some(Instant::now());
                            return Err(e);
                        }
                    }
                } else if refresh
                    && cache
                        .last_refresh
                        .is_none_or(|at| at.elapsed() >= min_interval)
                {
                    cache.last_refresh = Some(Instant::now());
                    cache.keys = Some(self.fetch(jwks_uri, *request_timeout).await?);
                }
                index_jwks(&issuer.iss, cache.keys.as_deref().unwrap_or_default())
            }
        }
    }

    /// Verify `token` and read it (`adecode_token`).
    pub async fn decode(&self, token: &str) -> Result<JwtToken, AuthentikateError> {
        let (iss, kid) = select_key_hints(token)?;
        let (index, issuer) = self
            .settings
            .issuers
            .iter()
            .enumerate()
            .find(|(_, issuer)| issuer.iss == iss)
            .ok_or_else(|| {
                AuthentikateError::InvalidJwtToken(format!("Untrusted issuer: {iss:?}"))
            })?;

        let mut keys = self.keys(index, issuer, false).await?;
        if !keys.contains_key(&kid) {
            keys = self.keys(index, issuer, true).await?;
        }
        let key = keys
            .get(&kid)
            .ok_or_else(|| AuthentikateError::InvalidJwtToken("Error decoding token".into()))?;

        let header = jsonwebtoken::decode_header(token)
            .map_err(|_| AuthentikateError::InvalidJwtToken("Error decoding token".into()))?;
        let allowed: Vec<Algorithm> = self
            .settings
            .algorithms
            .iter()
            .filter_map(|name| algorithm(name))
            .collect();
        if !allowed.contains(&header.alg) {
            return Err(AuthentikateError::InvalidJwtToken(
                "Error decoding token".into(),
            ));
        }

        let mut validation = Validation::new(header.alg);
        validation.algorithms = vec![header.alg];
        validation.leeway = 0;
        validation.validate_exp = true;
        validation.validate_nbf = true;
        validation.set_issuer(&[iss.as_str()]);
        validation.set_required_spec_claims(&["exp", "iss", "aud"]);
        if self.settings.audience == ANY_AUDIENCE {
            validation.validate_aud = false;
        } else {
            validation.set_audience(&[self.settings.audience.as_str()]);
        }

        let claims = jsonwebtoken::decode::<Value>(token, key, &validation)
            .map_err(|e| match e.kind() {
                jsonwebtoken::errors::ErrorKind::ExpiredSignature => {
                    AuthentikateError::TokenExpired
                }
                jsonwebtoken::errors::ErrorKind::InvalidSignature => {
                    AuthentikateError::InvalidJwtToken("Error decoding token".into())
                }
                _ => AuthentikateError::InvalidJwtToken("Token claims are invalid".into()),
            })?
            .claims;

        if let Some(revocation) = &self.revocations[index] {
            let jti = claims
                .get("jti")
                .and_then(Value::as_str)
                .filter(|jti| !jti.is_empty())
                .ok_or_else(|| {
                    AuthentikateError::MalformedJwtToken("Missing jti claim in token".into())
                })?;
            if revocation.is_revoked(&self.client, jti).await {
                return Err(AuthentikateError::TokenRevoked);
            }
        }

        let mut claims = claims;
        if let Some(map) = claims.as_object_mut() {
            map.insert("raw".into(), Value::String(token.to_owned()));
        }
        serde_json::from_value(claims)
            .map_err(|_| AuthentikateError::MalformedJwtToken("Error decoding token".into()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_hints_come_from_the_header_and_payload() {
        let b64 = |v: Value| base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(v.to_string());
        let token = format!(
            "{}.{}.sig",
            b64(serde_json::json!({"alg": "RS256", "kid": "k1"})),
            b64(serde_json::json!({"iss": "lok"}))
        );
        assert_eq!(
            select_key_hints(&token).unwrap(),
            ("lok".into(), "k1".into())
        );

        let no_kid = format!(
            "{}.{}.sig",
            b64(serde_json::json!({"alg": "RS256"})),
            b64(serde_json::json!({"iss": "lok"}))
        );
        assert!(matches!(
            select_key_hints(&no_kid),
            Err(AuthentikateError::MalformedJwtToken(_))
        ));
        assert!(matches!(
            select_key_hints("garbage"),
            Err(AuthentikateError::MalformedJwtToken(_))
        ));
    }

    #[test]
    fn an_openssh_rsa_key_is_read() {
        // The key the conformance stack's config trusts for issuer `lok`.
        let key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDXx/g7dyiwmfIQvkBKvUAM+KBuL1KdvZfZMlYhbyrz9Auk2xao1gLYiQJ183joiXadUreL4BugUrasQVdR9JvBWXOBeqm2FD5Ehf9VvqXdzCOQ7/fUXkb9N8sqmz5YitQuheKGDh0weNmVcfdsCGU38sACEvFQg+5dv1tqLbm7EllT+vogntDVX16DmvpvFEX4HYHpT8qM/xuqSB6yQ8+wBuQd3K7WTFgUrUqQx6zFrSMUbSArPrrY5Z0GJAeDAHKp4+V+um4mSHpYM2nBJ5pn80wYpsf97KIL9P+UJzjb/indB8dCqpPkAk3HogBhj67Tp5JoqCMoaXHFU3tiYQCp";
        assert!(openssh_rsa(key).is_some());
        assert!(rsa_key(key).is_ok());
        assert!(rsa_key("ssh-rsa bm90IGEga2V5").is_err());
    }
}

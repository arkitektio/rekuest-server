//! The token and the settings (`authentikate/base_models.py`, 4.1.1).

use std::collections::BTreeMap;

use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Deserializer};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::errors::ImproperlyConfigured;

/// A configured `audience` meaning "accept a token minted for any service". The token must
/// still carry an `aud`: the wildcard widens the check, it does not remove it.
pub const ANY_AUDIENCE: &str = "*";

/// Every asymmetric algorithm the default allow-list admits (symmetric `HS*` and `none`
/// are what pinning excludes).
pub const ASYMMETRIC_ALGORITHMS: &[&str] = &[
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "Ed25519",
    "Ed448", "EdDSA",
];

/// A verified token: who, acting in which organization, through which client.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct JwtToken {
    #[serde(deserialize_with = "string_or_number")]
    pub sub: String,
    pub iss: String,
    #[serde(deserialize_with = "unix_or_datetime")]
    pub exp: DateTime<Utc>,
    #[serde(default)]
    pub org: Option<String>,
    pub client_id: String,
    pub preferred_username: String,
    pub roles: Vec<String>,
    pub scope: String,
    #[serde(deserialize_with = "unix_or_datetime")]
    pub iat: DateTime<Utc>,
    #[serde(deserialize_with = "string_or_list")]
    pub aud: Vec<String>,
    #[serde(default)]
    pub jti: Option<String>,
    #[serde(default)]
    pub raw: String,
    #[serde(default)]
    pub client_app: Option<String>,
    #[serde(default)]
    pub client_release: Option<String>,
    #[serde(default)]
    pub client_device: Option<String>,
}

impl JwtToken {
    /// A hash that changes when the user changes: JSON of `[sub, preferred_username,
    /// sorted(roles), org]` with compact separators, sha256, as 4.1.1 computes it. It is
    /// persisted on the user, so it must match the Python side byte for byte.
    pub fn changed_hash(&self) -> String {
        let mut roles = self.roles.clone();
        roles.sort();
        let fingerprint = serde_json::to_string(&serde_json::json!([
            self.sub,
            self.preferred_username,
            roles,
            self.org
        ]))
        .expect("strings serialize");
        hex::encode(Sha256::digest(python_json_ascii(&fingerprint).as_bytes()))
    }

    pub fn scopes(&self) -> Vec<&str> {
        self.scope.split(' ').collect()
    }

    pub fn has_any_role(&self, roles: &[&str]) -> bool {
        roles.is_empty()
            || roles
                .iter()
                .any(|role| self.roles.iter().any(|r| r == role))
    }
}

/// `json.dumps` escapes non-ASCII by default (`ensure_ascii=True`); serde_json does not.
fn python_json_ascii(json: &str) -> String {
    let mut out = String::with_capacity(json.len());
    for c in json.chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut buf = [0u16; 2];
            for unit in c.encode_utf16(&mut buf) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

/// A pre-defined token that skips signature verification (tests only). Every field but
/// `sub` has a default, exactly as `StaticToken` does; unknown keys are ignored.
#[derive(Debug, Clone, Deserialize)]
pub struct StaticToken {
    #[serde(deserialize_with = "string_or_number")]
    pub sub: String,
    #[serde(default = "static_iss")]
    pub iss: String,
    #[serde(default, deserialize_with = "optional_unix_or_datetime")]
    pub iat: Option<DateTime<Utc>>,
    #[serde(default, deserialize_with = "optional_unix_or_datetime")]
    pub exp: Option<DateTime<Utc>>,
    #[serde(default = "static_client_id")]
    pub client_id: String,
    #[serde(default = "static_client_app")]
    pub client_app: String,
    #[serde(default = "static_client_release")]
    pub client_release: String,
    #[serde(default = "static_client_device")]
    pub client_device: String,
    #[serde(default = "static_org")]
    pub org: String,
    #[serde(default = "static_aud", deserialize_with = "string_or_list")]
    pub aud: Vec<String>,
    #[serde(default = "static_username")]
    pub preferred_username: String,
    #[serde(default = "static_scope")]
    pub scope: String,
    #[serde(default = "static_roles")]
    pub roles: Vec<String>,
    #[serde(default)]
    pub jti: Option<String>,
}

fn static_iss() -> String {
    "static_issuer".into()
}
fn static_client_id() -> String {
    "static".into()
}
fn static_client_app() -> String {
    "static_app".into()
}
fn static_client_release() -> String {
    "v1.0.0".into()
}
fn static_client_device() -> String {
    "static_device".into()
}
fn static_org() -> String {
    "static_org".into()
}
fn static_aud() -> Vec<String> {
    vec!["static_audience".into()]
}
fn static_username() -> String {
    "static_user".into()
}
fn static_scope() -> String {
    "openid profile email".into()
}
fn static_roles() -> Vec<String> {
    vec!["admin".into()]
}

impl StaticToken {
    /// The token this static token stands for. `iat` defaults to when the settings were
    /// read and `exp` to a day after, as the Python defaults do at validation time.
    pub fn to_token(&self, loaded_at: DateTime<Utc>, raw: &str) -> JwtToken {
        JwtToken {
            sub: self.sub.clone(),
            iss: self.iss.clone(),
            exp: self.exp.unwrap_or(loaded_at + Duration::days(1)),
            org: Some(self.org.clone()),
            client_id: self.client_id.clone(),
            preferred_username: self.preferred_username.clone(),
            roles: self.roles.clone(),
            scope: self.scope.clone(),
            iat: self.iat.unwrap_or(loaded_at),
            aud: self.aud.clone(),
            jti: self.jti.clone(),
            raw: raw.to_owned(),
            client_app: Some(self.client_app.clone()),
            client_release: Some(self.client_release.clone()),
            client_device: Some(self.client_device.clone()),
        }
    }
}

/// A trusted issuer: whose tokens are accepted, where its keys come from, and where it
/// publishes revoked tokens. Field aliases follow the Python models.
#[derive(Debug, Clone, Deserialize)]
pub struct Issuer {
    #[serde(alias = "issuer", alias = "issuer_url", alias = "ISSUER")]
    pub iss: String,
    #[serde(flatten)]
    pub keys: KeySource,
    /// Where the issuer lists the `jti`s of revoked, unexpired tokens; unset disables the check.
    #[serde(default, alias = "REVOCATION_URI")]
    pub revocation_uri: Option<String>,
    #[serde(
        default = "default_revocation_refresh",
        alias = "REVOCATION_REFRESH_INTERVAL"
    )]
    pub revocation_refresh_interval: f64,
    #[serde(
        default = "default_request_timeout",
        alias = "REVOCATION_REQUEST_TIMEOUT"
    )]
    pub revocation_request_timeout: f64,
}

/// Where an issuer's verification keys come from, by `kind`.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "kind")]
pub enum KeySource {
    /// A single RSA public key, PEM or OpenSSH (`ssh-rsa …`).
    #[serde(rename = "rsa")]
    Rsa {
        #[serde(default = "default_kid", alias = "kid", alias = "KID")]
        key_id: String,
        #[serde(alias = "PUBLIC_KEY")]
        public_key: String,
    },
    /// A single RSA public key read from a PEM file on every use.
    #[serde(rename = "rsa_file")]
    RsaFile {
        #[serde(default = "default_kid", alias = "kid", alias = "KID")]
        key_id: String,
        #[serde(alias = "PUBLIC_KEY_PEM_FILE")]
        public_key_pem_file: String,
    },
    /// An inline JWKS document.
    #[serde(rename = "jwks_dict")]
    JwksDict {
        #[serde(alias = "JWKS", alias = "JWKS_DICT")]
        jwks: Value,
    },
    /// A JWKS fetched from a URL, cached, refreshed on an unknown `kid`.
    #[serde(rename = "jwks_uri")]
    JwksUri {
        #[serde(alias = "JWKS_URI")]
        jwks_uri: String,
        #[serde(default = "default_min_refresh", alias = "MIN_REFRESH_INTERVAL")]
        min_refresh_interval: f64,
        #[serde(default = "default_request_timeout", alias = "REQUEST_TIMEOUT")]
        request_timeout: f64,
    },
}

fn default_kid() -> String {
    "1".into()
}
fn default_min_refresh() -> f64 {
    10.0
}
fn default_request_timeout() -> f64 {
    5.0
}
fn default_revocation_refresh() -> f64 {
    60.0
}

/// The `authentikate` block of the configuration (`AuthentikateSettings`).
#[derive(Debug, Clone, Deserialize)]
pub struct AuthentikateSettings {
    #[serde(default, alias = "ISSUERS")]
    pub issuers: Vec<Issuer>,
    #[serde(default, alias = "STATIC_TOKENS")]
    pub static_tokens: BTreeMap<String, StaticToken>,
    #[serde(alias = "AUDIENCE")]
    pub audience: String,
    #[serde(default = "default_algorithms", alias = "ALGORITHMS")]
    pub algorithms: Vec<String>,
    #[serde(default, alias = "ALLOWED_ORGANIZATIONS")]
    pub allowed_organizations: Option<Vec<String>>,
    #[serde(default, alias = "ALLOW_STATIC_TOKENS_IN_PRODUCTION")]
    pub allow_static_tokens_in_production: bool,
    /// When the settings were read: a static token without its own `iat`/`exp` was issued
    /// then and expires a day later, as the Python defaults (evaluated once, at load) do.
    #[serde(skip, default = "Utc::now")]
    pub loaded_at: DateTime<Utc>,
}

fn default_algorithms() -> Vec<String> {
    ASYMMETRIC_ALGORITHMS
        .iter()
        .map(|s| (*s).to_owned())
        .collect()
}

impl AuthentikateSettings {
    /// Parse and check the block, as `prepare_settings` does at startup: a blank audience,
    /// an empty or `none` algorithm list, and static tokens outside debug (unless explicitly
    /// allowed) are refused.
    pub fn prepare(block: &Value, debug: bool) -> Result<Self, ImproperlyConfigured> {
        let parsed: Self = serde_json::from_value(block.clone())
            .map_err(|e| ImproperlyConfigured(e.to_string()))?;
        if parsed.audience.trim().is_empty() {
            return Err(ImproperlyConfigured(format!(
                "audience must not be blank: set it to this service's identifier, or to \
                 {ANY_AUDIENCE:?} to accept a token minted for any service"
            )));
        }
        if parsed.algorithms.is_empty() {
            return Err(ImproperlyConfigured("algorithms must not be empty".into()));
        }
        if parsed
            .algorithms
            .iter()
            .any(|alg| alg.trim().eq_ignore_ascii_case("none"))
        {
            return Err(ImproperlyConfigured(
                "The 'none' algorithm is not allowed".into(),
            ));
        }
        if !parsed.static_tokens.is_empty() && !debug {
            if !parsed.allow_static_tokens_in_production {
                return Err(ImproperlyConfigured(
                    "AUTHENTIKATE.STATIC_TOKENS is set while DEBUG is False. Static tokens \
                     bypass signature verification and are for tests only. Remove them, or \
                     set ALLOW_STATIC_TOKENS_IN_PRODUCTION if this is deliberate."
                        .into(),
                ));
            }
            tracing::warn!(
                "AUTHENTIKATE.STATIC_TOKENS is enabled with DEBUG=False: these tokens bypass \
                 signature verification"
            );
        }
        if parsed.audience == ANY_AUDIENCE {
            tracing::warn!(
                "AUTHENTIKATE.AUDIENCE is '*': a token this issuer minted for any other service \
                 is accepted here"
            );
        }
        Ok(parsed)
    }

    pub fn find_issuer(&self, iss: &str) -> Option<&Issuer> {
        self.issuers.iter().find(|issuer| issuer.iss == iss)
    }
}

fn string_or_number<'de, D: Deserializer<'de>>(d: D) -> Result<String, D::Error> {
    match Value::deserialize(d)? {
        Value::String(s) => Ok(s),
        Value::Number(n) => Ok(n.to_string()),
        other => Err(serde::de::Error::custom(format!(
            "expected a string, got {other}"
        ))),
    }
}

fn string_or_list<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<String>, D::Error> {
    match Value::deserialize(d)? {
        Value::String(s) if s.is_empty() => Ok(vec![]),
        Value::String(s) => Ok(vec![s]),
        Value::Null => Ok(vec![]),
        other => serde_json::from_value(other).map_err(serde::de::Error::custom),
    }
}

fn to_datetime(value: Value) -> Result<DateTime<Utc>, String> {
    match value {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .and_then(|secs| DateTime::from_timestamp(secs, 0))
            .ok_or_else(|| format!("not a timestamp: {n}")),
        Value::String(s) => DateTime::parse_from_rfc3339(&s)
            .map(|d| d.with_timezone(&Utc))
            .map_err(|e| e.to_string()),
        other => Err(format!("not a timestamp: {other}")),
    }
}

fn unix_or_datetime<'de, D: Deserializer<'de>>(d: D) -> Result<DateTime<Utc>, D::Error> {
    to_datetime(Value::deserialize(d)?).map_err(serde::de::Error::custom)
}

fn optional_unix_or_datetime<'de, D: Deserializer<'de>>(
    d: D,
) -> Result<Option<DateTime<Utc>>, D::Error> {
    match Value::deserialize(d)? {
        Value::Null => Ok(None),
        value => to_datetime(value)
            .map(Some)
            .map_err(serde::de::Error::custom),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_changed_hash_is_the_one_python_persists() {
        // authentikate 4.1.1: sha256(json.dumps(["1", "admin", ["a", "b"], "org"], separators=(",", ":")))
        let token = JwtToken {
            sub: "1".into(),
            iss: "lok".into(),
            exp: Utc::now(),
            org: Some("org".into()),
            client_id: "c".into(),
            preferred_username: "admin".into(),
            roles: vec!["b".into(), "a".into()],
            scope: "openid".into(),
            iat: Utc::now(),
            aud: vec!["rekuest".into()],
            jti: None,
            raw: String::new(),
            client_app: None,
            client_release: None,
            client_device: None,
        };
        assert_eq!(
            token.changed_hash(),
            hex::encode(Sha256::digest(br#"["1","admin",["a","b"],"org"]"#))
        );
        let mut umlaut = token.clone();
        umlaut.preferred_username = "jörg".into();
        assert_eq!(
            umlaut.changed_hash(),
            hex::encode(Sha256::digest(br#"["1","j\u00f6rg",["a","b"],"org"]"#))
        );
    }

    #[test]
    fn static_tokens_fill_in_the_python_defaults() {
        let settings = AuthentikateSettings::prepare(
            &serde_json::json!({
                "audience": "rekuest",
                "static_tokens": {"test": {"sub": 1, "iss": "lok", "active_org": "ignored", "client_id": "test"}},
            }),
            true,
        )
        .unwrap();
        let token = settings.static_tokens["test"].to_token(Utc::now(), "test");
        assert_eq!(token.sub, "1");
        assert_eq!(
            token.org.as_deref(),
            Some("static_org"),
            "active_org is not a field"
        );
        assert_eq!(token.client_app.as_deref(), Some("static_app"));
        assert_eq!(token.roles, vec!["admin"]);
    }

    #[test]
    fn unsafe_settings_are_refused_at_startup() {
        let with_static =
            serde_json::json!({"audience": "r", "static_tokens": {"t": {"sub": "1"}}});
        assert!(AuthentikateSettings::prepare(&with_static, false).is_err());
        assert!(AuthentikateSettings::prepare(&with_static, true).is_ok());
        assert!(
            AuthentikateSettings::prepare(&serde_json::json!({"audience": " "}), true).is_err()
        );
        assert!(AuthentikateSettings::prepare(
            &serde_json::json!({"audience": "r", "algorithms": ["RS256", "none"]}),
            true
        )
        .is_err());
    }
}

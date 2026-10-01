//! This instance's Ed25519 key and the JWKS document (`facade/provenance/keys.py`).
//!
//! One key (`instance.private_key`, PKCS#8 PEM) signs the provenance tokens and every service
//! token; its `kid` is the RFC 7638 thumbprint of its public half, which is what the hub's trust
//! bundle lists it under (`settings.PROVENANCE["KID"]`). A static key is required: an ephemeral
//! one would sign tokens that fail to verify across restarts and replicas.
//!
//! Tokens are compact JWS with the RFC 9864 algorithm name `Ed25519` (not the deprecated
//! `EdDSA`), which is why they are built here rather than with a JWT library.

use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use base64::Engine;
use ring::signature::{Ed25519KeyPair, KeyPair, UnparsedPublicKey, ED25519};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// `ALGORITHM`: the JWS `alg` of every token this key signs.
pub const ALGORITHM: &str = "Ed25519";

#[derive(Debug, thiserror::Error)]
pub enum KeyError {
    #[error("the instance key is not a PEM: {0}")]
    Pem(String),
    #[error("the instance key is not an Ed25519 PKCS#8 key: {0}")]
    Key(String),
}

/// An Ed25519 key pair and its thumbprint.
pub struct InstanceKey {
    pair: Ed25519KeyPair,
    kid: String,
}

impl std::fmt::Debug for InstanceKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("InstanceKey")
            .field("kid", &self.kid)
            .finish_non_exhaustive()
    }
}

impl InstanceKey {
    /// Import a PKCS#8 PEM (`OKPKey.import_key`). Accepts PKCS#8 v1, which is what
    /// `cryptography` writes and what the configs hold.
    pub fn from_pem(pem: &str) -> Result<Self, KeyError> {
        let body: String = pem
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty() && !line.starts_with("-----"))
            .collect();
        if body.is_empty() {
            return Err(KeyError::Pem("empty".into()));
        }
        let der = STANDARD
            .decode(body)
            .map_err(|e| KeyError::Pem(e.to_string()))?;
        let pair = Ed25519KeyPair::from_pkcs8_maybe_unchecked(&der)
            .map_err(|e| KeyError::Key(e.to_string()))?;
        let kid = thumbprint(pair.public_key().as_ref());
        Ok(Self { pair, kid })
    }

    /// The RFC 7638 thumbprint of the public key (`OKPKey.thumbprint()`).
    pub fn kid(&self) -> &str {
        &self.kid
    }

    /// The raw 32-byte public key.
    pub fn public_bytes(&self) -> &[u8] {
        self.pair.public_key().as_ref()
    }

    /// The public key as a JWK, with `kid`, `use` and `alg` (`get_public_jwk`).
    pub fn public_jwk(&self) -> Value {
        json!({
            "crv": "Ed25519",
            "x": URL_SAFE_NO_PAD.encode(self.public_bytes()),
            "kty": "OKP",
            "kid": self.kid,
            "use": "sig",
            "alg": ALGORITHM,
        })
    }

    /// A compact JWS of `claims` under `header` (`joserfc.jwt.encode`).
    pub fn sign_jwt(&self, header: &Value, claims: &Value) -> String {
        let signing_input = format!(
            "{}.{}",
            URL_SAFE_NO_PAD.encode(serde_json::to_vec(header).expect("a JSON header")),
            URL_SAFE_NO_PAD.encode(serde_json::to_vec(claims).expect("JSON claims")),
        );
        let signature = self.pair.sign(signing_input.as_bytes());
        format!(
            "{signing_input}.{}",
            URL_SAFE_NO_PAD.encode(signature.as_ref())
        )
    }

    /// The header and claims of a compact JWS this key signed, or why not.
    pub fn verify_jwt(&self, token: &str) -> Result<(Value, Value), String> {
        let mut parts = token.split('.');
        let (Some(header), Some(claims), Some(signature), None) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        else {
            return Err("not a compact JWS".into());
        };
        let signature = URL_SAFE_NO_PAD
            .decode(signature)
            .map_err(|_| "the signature is not base64url".to_owned())?;
        UnparsedPublicKey::new(&ED25519, self.public_bytes())
            .verify(format!("{header}.{claims}").as_bytes(), &signature)
            .map_err(|_| "bad signature".to_owned())?;
        Ok((segment(header)?, segment(claims)?))
    }
}

/// A JWS segment's JSON, unverified (the caller verified, or only reads the `kid`).
pub fn segment(part: &str) -> Result<Value, String> {
    let bytes = URL_SAFE_NO_PAD
        .decode(part.trim_end_matches('='))
        .map_err(|_| "a segment is not base64url".to_owned())?;
    serde_json::from_slice(&bytes).map_err(|_| "a segment is not JSON".to_owned())
}

/// RFC 7638: the SHA-256 of the required members in lexicographic order, base64url.
fn thumbprint(public: &[u8]) -> String {
    let canonical = format!(
        r#"{{"crv":"Ed25519","kty":"OKP","x":"{}"}}"#,
        URL_SAFE_NO_PAD.encode(public)
    );
    URL_SAFE_NO_PAD.encode(Sha256::digest(canonical.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The conformance stack's key (`conformance/stack/configs/rekuest.yaml`).
    const PEM: &str = "-----BEGIN PRIVATE KEY-----\n\
        MC4CAQAwBQYDK2VwBCIEILK+rl9gVEjfKGiye+mLLjfEUGIdoP0WPC8lMZS3NYK2\n\
        -----END PRIVATE KEY-----\n";

    #[test]
    fn imports_the_pkcs8_pem_and_derives_the_public_half() {
        let key = InstanceKey::from_pem(PEM).unwrap();
        // The config's public_key: MCowBQYDK2VwAyEA6lDpKZJ0ManLzreBg43+fZPoPP5lHOVk2E23pFDzNck=
        let spki = STANDARD
            .decode("MCowBQYDK2VwAyEA6lDpKZJ0ManLzreBg43+fZPoPP5lHOVk2E23pFDzNck=")
            .unwrap();
        assert_eq!(key.public_bytes(), &spki[12..]);
        // python: OKPKey.import_key(pem).thumbprint()
        assert_eq!(key.kid(), "7eIQqUMbaH9I7r7g4sjurBd2Xx49fLxU2RYvdUBPl6g");
    }

    #[test]
    fn signs_and_verifies_its_own_tokens() {
        let key = InstanceKey::from_pem(PEM).unwrap();
        let token = key.sign_jwt(
            &json!({"alg": ALGORITHM, "kid": key.kid(), "typ": "JWT"}),
            &json!({"iss": "rekuest"}),
        );
        let (header, claims) = key.verify_jwt(&token).unwrap();
        assert_eq!(header["kid"], key.kid());
        assert_eq!(claims["iss"], "rekuest");
        let tampered = token.replace(".ey", ".eX");
        assert!(key.verify_jwt(&tampered).is_err());
    }
}

//! Signed tokens, verified end to end: every key form a config can name, and every refusal.

use authentikate::{authenticate_token, AuthentikateError, AuthentikateSettings, Verifier};
use base64::Engine;
use jsonwebtoken::{Algorithm, EncodingKey, Header};
use serde_json::{json, Value};

const PRIVATE: &str = include_str!("fixtures/test_private.pem");
const PUBLIC_PEM: &str = include_str!("fixtures/test_public.pem");
const PUBLIC_SSH: &str = include_str!("fixtures/test_public.ssh");

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

fn claims(overrides: Value) -> Value {
    let mut claims = json!({
        "sub": "7", "iss": "lok", "exp": now() + 300, "iat": now(), "aud": ["rekuest"],
        "org": "lab", "client_id": "cli", "preferred_username": "ada",
        "roles": ["researcher"], "scope": "openid", "client_app": "app", "client_release": "1",
        "client_device": "dev",
    });
    for (key, value) in overrides.as_object().unwrap() {
        if value.is_null() {
            claims.as_object_mut().unwrap().remove(key);
        } else {
            claims[key] = value.clone();
        }
    }
    claims
}

fn sign(claims: &Value, kid: &str) -> String {
    let mut header = Header::new(Algorithm::RS256);
    header.kid = Some(kid.into());
    jsonwebtoken::encode(
        &header,
        claims,
        &EncodingKey::from_rsa_pem(PRIVATE.as_bytes()).unwrap(),
    )
    .unwrap()
}

/// The fixture's public key as a JWK, read from its ssh-rsa form.
fn jwk(kid: &str) -> Value {
    let blob = base64::engine::general_purpose::STANDARD
        .decode(PUBLIC_SSH.split_whitespace().nth(1).unwrap())
        .unwrap();
    let mut fields = vec![];
    let mut rest = blob.as_slice();
    while rest.len() >= 4 {
        let len = u32::from_be_bytes(rest[..4].try_into().unwrap()) as usize;
        fields.push(rest[4..4 + len].to_vec());
        rest = &rest[4 + len..];
    }
    let b64 = |bytes: &[u8]| {
        let trimmed: Vec<u8> = bytes.iter().skip_while(|b| **b == 0).copied().collect();
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(trimmed)
    };
    json!({"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig", "e": b64(&fields[1]), "n": b64(&fields[2])})
}

fn verifier(issuer: Value, audience: &str) -> Verifier {
    Verifier::new(
        AuthentikateSettings::prepare(&json!({"audience": audience, "issuers": [issuer]}), false)
            .unwrap(),
    )
}

fn pem_issuer() -> Value {
    json!({"kind": "rsa", "iss": "lok", "kid": "k1", "public_key": PUBLIC_PEM})
}

#[tokio::test]
async fn every_key_form_verifies_the_same_token() {
    let token = sign(&claims(json!({})), "k1");
    for issuer in [
        pem_issuer(),
        json!({"kind": "rsa", "issuer": "lok", "key_id": "k1", "public_key": PUBLIC_SSH}),
        json!({"kind": "jwks_dict", "iss": "lok", "jwks": {"keys": [jwk("k1")]}}),
    ] {
        let decoded = authenticate_token(&verifier(issuer.clone(), "rekuest"), &token)
            .await
            .unwrap_or_else(|e| panic!("{issuer} refused a good token: {e}"));
        assert_eq!(decoded.sub, "7");
        assert_eq!(decoded.org.as_deref(), Some("lab"));
        assert_eq!(decoded.aud, vec!["rekuest"]);
        assert_eq!(decoded.raw, token);
    }
}

#[tokio::test]
async fn audience_is_matched_and_any_audience_still_requires_one() {
    let other = sign(&claims(json!({"aud": "mikro"})), "k1");
    assert!(matches!(
        authenticate_token(&verifier(pem_issuer(), "rekuest"), &other).await,
        Err(AuthentikateError::InvalidJwtToken(_))
    ));
    assert!(authenticate_token(&verifier(pem_issuer(), "*"), &other)
        .await
        .is_ok());

    let none = sign(&claims(json!({"aud": null})), "k1");
    assert!(authenticate_token(&verifier(pem_issuer(), "*"), &none)
        .await
        .is_err());
}

#[tokio::test]
async fn expiry_issuer_and_key_are_enforced() {
    let v = verifier(pem_issuer(), "rekuest");
    let expired = sign(&claims(json!({"exp": now() - 10})), "k1");
    assert!(matches!(
        authenticate_token(&v, &expired).await,
        Err(AuthentikateError::TokenExpired)
    ));

    let stranger = sign(&claims(json!({"iss": "someone-else"})), "k1");
    assert!(matches!(
        authenticate_token(&v, &stranger).await,
        Err(AuthentikateError::InvalidJwtToken(_))
    ));

    let unknown_kid = sign(&claims(json!({})), "k2");
    assert!(authenticate_token(&v, &unknown_kid).await.is_err());

    // A symmetric token signed with the public key as its secret: the classic confusion attack.
    let mut header = Header::new(Algorithm::HS256);
    header.kid = Some("k1".into());
    let forged = jsonwebtoken::encode(
        &header,
        &claims(json!({})),
        &EncodingKey::from_secret(PUBLIC_PEM.as_bytes()),
    )
    .unwrap();
    assert!(authenticate_token(&v, &forged).await.is_err());
}

//! The HTTP transport of HookAgents (`facade/hooks.py`): an agent of kind `WEBHOOK` speaks the
//! same messages as a socket agent, over HTTP. The server POSTs to its `hook_url`, and it POSTs
//! to the intake ([`crate::http_intake`]). Both ways are authenticated by an HMAC over the
//! shared `hook_url_secret`, or, for the hub's own services, by their instance keys
//! ([`crate::service_trust`]).
//!
//! Delivery is persist-then-POST: the row is written first, so a failed POST is logged, never
//! raised, and the row stays the record a redelivery works from.

use std::sync::OnceLock;
use std::time::Duration;

use hmac::{Hmac, Mac};
use sha2::Sha256;

use crate::settings::Settings;

/// The legacy body-only signature (`SIGNATURE_HEADER`): replayable, accepted in `compat` mode.
pub const SIGNATURE_HEADER: &str = "X-Rekuest-Signature";
/// The replay-protected signature, `t=<unix seconds>,v1=<hex>` over `v1:{agent}:{t}:` + body
/// (`SIGNATURE_V1_HEADER`).
pub const SIGNATURE_V1_HEADER: &str = "X-Rekuest-Signature-V1";
/// The HookAgent a delivery is for (`AGENT_HEADER`): the receiver needs it to verify V1.
pub const AGENT_HEADER: &str = "X-Rekuest-Agent";
const TIMEOUT: Duration = Duration::from_secs(10);

/// HMAC-SHA256 hex of `body` under `secret` (`sign`).
pub fn sign(secret: &str, body: &[u8]) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("HMAC takes any key");
    mac.update(body);
    hex::encode(mac.finalize().into_bytes())
}

/// `hmac.compare_digest` for two strings.
fn equal(a: &str, b: &str) -> bool {
    a.len() == b.len()
        && a.bytes()
            .zip(b.bytes())
            .fold(0u8, |acc, (x, y)| acc | (x ^ y))
            == 0
}

/// A legacy signature checks out (`verify`).
pub fn verify(secret: Option<&str>, body: &[u8], signature: Option<&str>) -> bool {
    match (
        secret.filter(|s| !s.is_empty()),
        signature.filter(|s| !s.is_empty()),
    ) {
        (Some(secret), Some(signature)) => equal(&sign(secret, body), signature),
        _ => false,
    }
}

/// What a V1 signature covers (`signed_payload_v1`).
pub fn signed_payload_v1(agent: &str, timestamp: i64, body: &[u8]) -> Vec<u8> {
    let mut payload = format!("v1:{agent}:{timestamp}:").into_bytes();
    payload.extend_from_slice(body);
    payload
}

/// The V1 header value for `body` addressed to `agent` (`sign_v1`).
pub fn sign_v1(secret: &str, agent: &str, body: &[u8], timestamp: i64) -> String {
    format!(
        "t={timestamp},v1={}",
        sign(secret, &signed_payload_v1(agent, timestamp, body))
    )
}

/// `(timestamp, digest)` of a V1 header (`parse_v1`).
pub fn parse_v1(header: Option<&str>) -> Option<(i64, String)> {
    let header = header.filter(|h| !h.is_empty())?;
    let (mut timestamp, mut digest) = (None, None);
    for piece in header.split(',') {
        if let Some((key, value)) = piece.split_once('=') {
            match key {
                "t" => timestamp = Some(value),
                "v1" => digest = Some(value),
                _ => {}
            }
        }
    }
    Some((timestamp?.parse().ok()?, digest?.to_owned()))
}

/// `(ok, digest)` of a V1-signed request (`verify_v1`): the digest names it for the replay
/// guard. Outside `max_skew`, either way, it is refused even with a valid signature.
pub fn verify_v1(
    secret: Option<&str>,
    agent: &str,
    body: &[u8],
    header: Option<&str>,
    max_skew: i64,
    now: i64,
) -> (bool, Option<String>) {
    let (Some(secret), Some((timestamp, digest))) =
        (secret.filter(|s| !s.is_empty()), parse_v1(header))
    else {
        return (false, None);
    };
    if (now - timestamp).abs() > max_skew {
        return (false, Some(digest));
    }
    let expected = sign(secret, &signed_payload_v1(agent, timestamp, body));
    (equal(&expected, &digest), Some(digest))
}

/// A HookAgent as a delivery needs it.
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct HookTarget {
    pub id: i64,
    pub hook_url: Option<String>,
    pub hook_url_secret: Option<String>,
    /// Its client's `client_id`: tells a configured hook agent from any other.
    pub client_id: String,
}

fn client() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(TIMEOUT)
            .build()
            .expect("an HTTP client")
    })
}

/// POST `body` (one JSON message) to the agent's `hook_url`, signed; whether it answered 2xx
/// (`deliver_to_hook`). Never fails loudly: the persisted row is the record.
pub async fn deliver_to_hook(settings: &Settings, agent: &HookTarget, body: &str) -> bool {
    let Some(url) = agent.hook_url.as_deref().filter(|u| !u.is_empty()) else {
        tracing::error!(
            agent = agent.id,
            "HookAgent {} has no hook_url; dropping message",
            agent.id
        );
        return false;
    };
    let raw = body.as_bytes();
    let mut request = client()
        .post(url)
        .header("Content-Type", "application/json")
        .header(AGENT_HEADER, agent.id.to_string());
    if let Some(entry) = crate::service_trust::hook_agent_for_client(settings, &agent.client_id) {
        match crate::service_trust::sign_to(settings, entry, url, raw) {
            Ok(authorization) => request = request.header("Authorization", authorization),
            Err(e) => {
                tracing::error!(
                    agent = agent.id,
                    "Could not sign a delivery to hook agent {}: {e}",
                    agent.id
                );
                return false;
            }
        }
    } else if let Some(secret) = agent.hook_url_secret.as_deref().filter(|s| !s.is_empty()) {
        let now = chrono::Utc::now().timestamp();
        request = request.header(
            SIGNATURE_V1_HEADER,
            sign_v1(secret, &agent.id.to_string(), raw, now),
        );
        if !settings.hook_signature_strict {
            // Beside V1 during the compatibility window; `strict` drops it.
            request = request.header(SIGNATURE_HEADER, sign(secret, raw));
        }
    }
    match request
        .body(body.to_owned())
        .send()
        .await
        .and_then(reqwest::Response::error_for_status)
    {
        Ok(_) => true,
        Err(e) => {
            tracing::error!(
                agent = agent.id,
                "Failed to deliver message to HookAgent {} at {url}: {e}",
                agent.id
            );
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signatures_are_pythons() {
        // python: hmac.new(b"s3cret", b'{"a":1}', hashlib.sha256).hexdigest()
        assert_eq!(
            sign("s3cret", br#"{"a":1}"#),
            "5910e62016ef5034272c926c27071992a465c2335cecf41851bda071577f4f6d"
        );
        // python: facade.hooks.sign_v1("s3cret", 7, b"{}", 1700000000)
        assert_eq!(
            sign_v1("s3cret", "7", b"{}", 1_700_000_000),
            "t=1700000000,v1=d963a256b457cc83e0e52a34411f472fd58dd61720874d33324f2b4dccd1cc94"
        );
        let header = sign_v1("s3cret", "7", b"{}", 1_700_000_000);
        assert_eq!(parse_v1(Some(&header)).unwrap().0, 1_700_000_000);
        assert!(
            verify_v1(
                Some("s3cret"),
                "7",
                b"{}",
                Some(&header),
                300,
                1_700_000_100
            )
            .0
        );
        assert!(
            !verify_v1(
                Some("s3cret"),
                "8",
                b"{}",
                Some(&header),
                300,
                1_700_000_100
            )
            .0,
            "bound to the agent"
        );
        assert!(
            !verify_v1(
                Some("s3cret"),
                "7",
                b"{}",
                Some(&header),
                300,
                1_700_000_400
            )
            .0,
            "outside the skew"
        );
        assert!(!verify_v1(None, "7", b"{}", Some(&header), 300, 1_700_000_100).0);
        assert!(verify(Some("s3cret"), b"x", Some(&sign("s3cret", b"x"))));
        assert!(!verify(Some("s3cret"), b"x", Some("nope")));
        assert_eq!(parse_v1(Some("t=abc,v1=ff")), None);
    }
}

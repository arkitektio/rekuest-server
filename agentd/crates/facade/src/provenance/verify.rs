//! Verify a provenance token this rekuest minted: the proof a service was called in a task
//! (`facade/provenance/verify.py`). Issuer, signature and expiry are checked against this
//! instance's own key; `jti` is not single-use here, since one task legitimately creates many
//! objects and each announces itself with the same token.

use serde_json::Value;

use super::keys::ALGORITHM;
use crate::settings::Settings;

/// The claims a signal needs: which task, in which tree, for which human (`OwnProvenance`).
#[derive(Debug, Clone, PartialEq)]
pub struct OwnProvenance {
    pub task: String,
    pub root: String,
    pub parent: Option<String>,
    pub root_caused_by: Option<Value>,
}

fn text(value: &Value) -> Option<String> {
    match value {
        Value::String(s) if !s.is_empty() => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

/// The claims of a token this rekuest minted, or `None` when absent or not genuine
/// (`verify_own_token`). Every failure is the same to a signal: logged and ignored.
pub fn verify_own_token(settings: &Settings, raw: Option<&str>) -> Option<OwnProvenance> {
    let raw = raw.filter(|r| !r.is_empty())?;
    let key = settings.instance_key.as_deref()?;
    let refuse = |why: &str| {
        tracing::warn!("Ignoring a provenance token that does not verify: {why}");
        None
    };
    let (header, claims) = match key.verify_jwt(raw) {
        Ok(verified) => verified,
        Err(e) => return refuse(&e),
    };
    if header.get("alg").and_then(Value::as_str) != Some(ALGORITHM) {
        return refuse("not Ed25519");
    }
    if claims.get("iss").and_then(Value::as_str) != Some(settings.provenance.issuer.as_str()) {
        return refuse("another issuer");
    }
    match claims.get("exp").and_then(Value::as_i64) {
        Some(exp) if exp >= chrono::Utc::now().timestamp() => {}
        _ => return refuse("expired, or no exp"),
    }
    let Some(task) = claims.get("tsk").and_then(text) else {
        return refuse("no tsk");
    };
    Some(OwnProvenance {
        root: claims
            .get("rtk")
            .and_then(text)
            .unwrap_or_else(|| task.clone()),
        parent: claims.get("ptk").and_then(text),
        root_caused_by: claims.get("rcb").filter(|v| !v.is_null()).cloned(),
        task,
    })
}

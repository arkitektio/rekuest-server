//! Checking verified tokens against an issuer's revocation list (`authentikate/revocation.py`).
//!
//! The list is fetched at most once per `refresh_interval`, however many tokens are presented,
//! and a failed refresh is not retried before the next interval: the request rate is set by
//! configuration, not by traffic. Fail-open by design, with a stale copy preferred over none:
//! revocation is defence in depth on top of signature and expiry, and failing closed would let
//! an unreachable issuer log everyone out. The document is `{"revoked": [jti, …]}` or a bare list.

use std::collections::HashSet;
use std::time::{Duration, Instant};

use serde_json::Value;
use tokio::sync::Mutex;

#[derive(Default)]
struct State {
    revoked: Option<HashSet<String>>,
    last_attempt: Option<Instant>,
    last_success: Option<Instant>,
}

pub struct RevocationList {
    uri: String,
    refresh_interval: Duration,
    request_timeout: Duration,
    state: Mutex<State>,
}

fn parse_revoked(payload: Value) -> Option<HashSet<String>> {
    let list = match payload {
        Value::Object(mut map) => map.remove("revoked")?,
        other => other,
    };
    list.as_array()?
        .iter()
        .map(|jti| jti.as_str().map(str::to_owned))
        .collect()
}

impl RevocationList {
    pub fn new(uri: &str, refresh_interval: f64, request_timeout: f64) -> Self {
        Self {
            uri: uri.to_owned(),
            refresh_interval: Duration::from_secs_f64(refresh_interval.max(0.0)),
            request_timeout: Duration::from_secs_f64(request_timeout.max(0.0)),
            state: Mutex::new(State::default()),
        }
    }

    /// Whether `jti` is revoked, refreshing the list first when it is due.
    pub async fn is_revoked(&self, client: &reqwest::Client, jti: &str) -> bool {
        let mut state = self.state.lock().await;
        let due = state
            .last_attempt
            .is_none_or(|at| at.elapsed() >= self.refresh_interval);
        if due {
            self.refresh(client, &mut state).await;
        }
        state
            .revoked
            .as_ref()
            .is_some_and(|revoked| revoked.contains(jti))
    }

    async fn refresh(&self, client: &reqwest::Client, state: &mut State) {
        let attempt = Instant::now();
        state.last_attempt = Some(attempt);
        let fetched = async {
            let response = client
                .get(&self.uri)
                .timeout(self.request_timeout)
                .send()
                .await
                .ok()?
                .error_for_status()
                .ok()?;
            parse_revoked(response.json().await.ok()?)
        }
        .await;
        match fetched {
            Some(revoked) => {
                state.revoked = Some(revoked);
                state.last_success = Some(attempt);
            }
            None if state.last_success.is_none() => tracing::warn!(
                "Could not load the revocation list from {}; no token is treated as revoked until it loads",
                self.uri
            ),
            None => tracing::warn!(
                "Could not refresh the revocation list from {}; keeping the previous copy",
                self.uri
            ),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn both_document_shapes_parse() {
        let object = parse_revoked(serde_json::json!({"revoked": ["a", "b"]})).unwrap();
        assert!(object.contains("a") && object.contains("b"));
        assert_eq!(parse_revoked(serde_json::json!(["c"])).unwrap().len(), 1);
        assert!(parse_revoked(serde_json::json!({"revoked": [1]})).is_none());
        assert!(parse_revoked(serde_json::json!({"other": []})).is_none());
    }
}

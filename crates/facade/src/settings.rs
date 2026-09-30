//! What the app reads from the project's settings (the Python side's `django.conf.settings`).
//!
//! The `rekuest` crate builds this from the configuration, as `rekuest/settings.py` does.

use std::time::Duration;

#[derive(Debug, Clone)]
pub struct Settings {
    /// `REDIS_KEY_PREFIX`: the namespace of every redis key the protocol writes.
    pub redis_key_prefix: String,
    /// `AGENT_HEARTBEAT_INTERVAL`: how often the server pings an agent.
    pub agent_heartbeat_interval: Duration,
    /// `AGENT_HEARTBEAT_RESPONSE_TIMEOUT`: how long an answer may take.
    pub agent_heartbeat_response_timeout: Duration,
    /// `AGENT_STALE_AFTER`: without a heartbeat this long, a `connected` agent is presumed dead.
    pub agent_stale_after: Duration,
}

impl Default for Settings {
    /// The Python server's defaults (`rekuest/settings.py`).
    fn default() -> Self {
        let interval = Duration::from_secs(10);
        Self {
            redis_key_prefix: "rekuest".into(),
            agent_heartbeat_interval: interval,
            agent_heartbeat_response_timeout: Duration::from_secs(5),
            agent_stale_after: interval * 3,
        }
    }
}

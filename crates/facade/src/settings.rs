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
    /// `REKUEST_GRACE["DEFAULT"]`: how long a cleanly disconnected agent's in-flight work waits
    /// for it to come back before it ends LOST; zero fails it inline on the disconnect.
    pub grace: Duration,
    /// `REKUEST_GRACE["SWEEP_INTERVAL"]`: how often the reaper ticks; bounds how late any
    /// deadline fires.
    pub sweep_interval: Duration,
    /// `REKUEST_GRACE["PICKUP_DEADLINE"]`: how long a dispatched task may go without any agent
    /// report; zero disables the pickup watchdog.
    pub pickup_deadline: Duration,
    /// `REKUEST_GRACE["DISCONNECTED_EXPIRY"]`: how long a gone agent keeps its undelivered
    /// work before it ends LOST; zero never expires it.
    pub disconnected_expiry: Duration,
    /// `REKUEST_GRACE["CONTROL_DEADLINE"]`: how long a cancel may stay unconfirmed before it
    /// escalates to an interrupt (and an interrupt before it is finalized); zero disables.
    pub control_deadline: Duration,
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
            grace: Duration::from_secs(30),
            sweep_interval: Duration::from_secs(5),
            pickup_deadline: Duration::from_secs(60),
            disconnected_expiry: Duration::from_secs(3600),
            control_deadline: Duration::from_secs(60),
        }
    }
}

//! The settings the app reads, derived from the configuration (`rekuest/settings.py`).

use std::time::Duration;

use facade::settings::Settings;

use crate::Configuration;

/// `AGENT_HEARTBEAT_INTERVAL`, `AGENT_HEARTBEAT_RESPONSE_TIMEOUT` and `AGENT_STALE_AFTER` are
/// constants in `settings.py` too, not configuration.
const AGENT_HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
const AGENT_HEARTBEAT_RESPONSE_TIMEOUT: Duration = Duration::from_secs(5);

pub fn from_configuration(configuration: &Configuration) -> Settings {
    let rekuest = &configuration.rekuest;
    Settings {
        redis_key_prefix: configuration.redis.key_prefix.clone(),
        agent_heartbeat_interval: AGENT_HEARTBEAT_INTERVAL,
        agent_heartbeat_response_timeout: AGENT_HEARTBEAT_RESPONSE_TIMEOUT,
        agent_stale_after: AGENT_HEARTBEAT_INTERVAL * 3,
        grace: Duration::from_secs(rekuest.grace_default),
        // `sweep_interval_seconds` floors it: a zero interval would spin the reaper.
        sweep_interval: Duration::from_secs(rekuest.sweep_interval).max(Duration::from_millis(50)),
        pickup_deadline: Duration::from_secs(rekuest.pickup_deadline),
        disconnected_expiry: Duration::from_secs(rekuest.disconnected_expiry),
        control_deadline: Duration::from_secs(rekuest.control_deadline),
    }
}

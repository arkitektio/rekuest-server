//! Single source of truth for "is this websocket agent alive?" (`facade/liveness.py`).
//!
//! Liveness is `connected AND a fresh heartbeat`, and the asymmetry is deliberate:
//! `connected = false` is a definitive negative (a clean close was observed, or the sweep
//! revoked the lease), while `connected = true` is only not-yet-refuted: a killed worker never
//! disconnects, so the flag can stay stuck. The heartbeat lease (`last_seen`) is what makes
//! true trustworthy: it expires on its own, with no writer.
//!
//! Correctness lives in the write discipline (`facade::persist::leases`): transitions (claim,
//! release, revoke) take a row lock; renewal (the heartbeat) is a lock-free compare-and-set on
//! `lease_epoch`. All timestamps use the application clock.

use chrono::{DateTime, Utc};

use crate::settings::Settings;

/// Whether a websocket connection is genuinely alive: connected AND a fresh heartbeat.
pub fn agent_is_live(
    settings: &Settings,
    connected: bool,
    last_seen: Option<DateTime<Utc>>,
    now: DateTime<Utc>,
) -> bool {
    match last_seen {
        Some(seen) if connected => {
            let window = chrono::Duration::from_std(settings.agent_stale_after)
                .unwrap_or(chrono::Duration::MAX);
            seen > now - window
        }
        _ => false,
    }
}

/// Whether an agent is stuck-connected: `connected` but its lease expired (or never began).
/// Exactly the rows the sweep revokes. Not the same as `!agent_is_live`: a cleanly
/// disconnected agent is neither live nor stale.
pub fn agent_is_stale(
    settings: &Settings,
    connected: bool,
    last_seen: Option<DateTime<Utc>>,
    now: DateTime<Utc>,
) -> bool {
    connected && !agent_is_live(settings, connected, last_seen, now)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    #[test]
    fn live_needs_both_the_flag_and_a_fresh_heartbeat() {
        let settings = Settings::default();
        let now = Utc::now();
        let fresh = Some(now - Duration::seconds(5));
        let expired = Some(now - Duration::seconds(31));

        assert!(agent_is_live(&settings, true, fresh, now));
        assert!(!agent_is_live(&settings, false, fresh, now));
        assert!(!agent_is_live(&settings, true, expired, now));
        assert!(!agent_is_live(&settings, true, None, now));

        assert!(agent_is_stale(&settings, true, expired, now));
        assert!(agent_is_stale(&settings, true, None, now));
        assert!(
            !agent_is_stale(&settings, false, expired, now),
            "a clean close is not stale"
        );
        assert!(!agent_is_stale(&settings, true, fresh, now));
    }
}

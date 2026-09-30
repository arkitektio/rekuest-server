//! Guard the one assumption the liveness model makes across backends: a shared clock
//! (`facade/clock.py`).
//!
//! [`crate::liveness`] compares application clocks: one backend writes `Agent.last_seen` with
//! its clock, another's sweep compares it against its own. A backend whose clock runs ahead sees
//! a healthy agent as stale, revokes its lease and fails its work; the agent reconnects and is
//! revoked again. The margin is how much older than the stale window a fresh heartbeat can look:
//!
//! ```text
//! tolerated skew = AGENT_STALE_AFTER − AGENT_HEARTBEAT_INTERVAL − AGENT_HEARTBEAT_RESPONSE_TIMEOUT
//! ```
//!
//! halved, since two backends may drift in opposite directions. A backend measures itself
//! against the database clock, the one clock every backend shares, and refuses to sweep while
//! it is out.

use chrono::{DateTime, Utc};
use sqlx::PgPool;

use crate::settings::Settings;

/// How far this backend's clock may be from the database's before it stops sweeping, in
/// seconds (`max_skew_seconds`).
pub fn max_skew_seconds(settings: &Settings) -> f64 {
    let budget = settings.agent_stale_after.as_secs_f64()
        - settings.agent_heartbeat_interval.as_secs_f64()
        - settings.agent_heartbeat_response_timeout.as_secs_f64();
    (budget / 2.0).max(1.0)
}

/// Seconds this process's clock is ahead of (positive) or behind (negative) the database's
/// (`measure_skew`).
pub async fn measure_skew(db: &PgPool) -> Result<f64, sqlx::Error> {
    let database_now: DateTime<Utc> = sqlx::query_scalar("SELECT now()").fetch_one(db).await?;
    Ok((Utc::now() - database_now).as_seconds_f64())
}

/// The measured skew, or `None` when it could not be measured: never a reason to block work
/// (`check_skew`).
pub async fn check_skew(db: &PgPool) -> Option<f64> {
    match measure_skew(db).await {
        Ok(skew) => Some(skew),
        Err(e) => {
            tracing::debug!("could not measure clock skew: {e}");
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_skew_budget_is_half_the_heartbeat_slack() {
        // 30 − 10 − 5 = 15, halved.
        assert_eq!(max_skew_seconds(&Settings::default()), 7.5);
        let tight = Settings {
            agent_stale_after: std::time::Duration::from_secs(12),
            ..Settings::default()
        };
        assert_eq!(max_skew_seconds(&tight), 1.0, "never below a second");
    }
}

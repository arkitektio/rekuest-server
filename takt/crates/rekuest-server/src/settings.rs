//! The settings the app reads, derived from the configuration (`rekuest/settings.py`).

use std::sync::Arc;
use std::time::Duration;

use facade::provenance::keys::InstanceKey;
use facade::service_trust::TrustBundle;
use facade::settings::{ProvenanceSettings, ServiceAgent, Settings};

use crate::Configuration;

/// `AGENT_HEARTBEAT_INTERVAL`, `AGENT_HEARTBEAT_RESPONSE_TIMEOUT` and `AGENT_STALE_AFTER` are
/// constants in `settings.py` too, not configuration.
const AGENT_HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
const AGENT_HEARTBEAT_RESPONSE_TIMEOUT: Duration = Duration::from_secs(5);

/// The settings, or why the configuration cannot give them (an unreadable instance key: the
/// Python server refuses to start without one too).
pub fn from_configuration(configuration: &Configuration) -> anyhow::Result<Settings> {
    let instance_key = configuration
        .instance
        .as_ref()
        .map(|instance| InstanceKey::from_pem(&instance.private_key))
        .transpose()
        .map_err(|e| anyhow::anyhow!("instance.private_key: {e}"))?
        .map(Arc::new);
    let rekuest = &configuration.rekuest;
    let provenance = &configuration.provenance;
    Ok(Settings {
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
        provenance: ProvenanceSettings {
            issuer: provenance.issuer.clone(),
            token_ttl: Duration::from_secs(provenance.token_ttl_seconds),
            human_roles: provenance.human_roles.clone(),
            strict: provenance.strict,
        },
        instance_key,
        rekuest_identifier: rekuest.identifier.clone(),
        server_url: match rekuest.server_url.as_deref() {
            None => Some(
                format!(
                    "http://rekuest:80/{}",
                    configuration.django.force_script_name.trim_matches('/')
                )
                .trim_end_matches('/')
                .to_owned(),
            ),
            Some(url) if url.trim().is_empty() => None,
            Some(url) => Some(url.trim_end_matches('/').to_owned()),
        },
        probe_ttl: Duration::from_secs(rekuest.probe_ttl),
        probe_linger: Duration::from_secs(rekuest.probe_linger),
        probe_max_inflight: rekuest.probe_max_inflight,
        hook_signature_strict: rekuest.hook_signature_mode.eq_ignore_ascii_case("strict"),
        hook_max_skew: rekuest.hook_max_skew as i64,
        task_retention: Duration::from_secs(rekuest.task_retention),
        ephemeral_task_retention: Duration::from_secs(rekuest.ephemeral_task_retention),
        signal_retention: Duration::from_secs(rekuest.signal_retention),
        trigger_max_depth: rekuest.trigger_max_depth,
        service_agents: rekuest
            .service_agents
            .iter()
            .map(|entry| ServiceAgent {
                service: entry.service.clone(),
                identifier: entry.identifier.clone(),
            })
            .collect(),
        trust_bundle: Arc::new(configuration.instance.as_ref().map_or_else(
            TrustBundle::default,
            |instance| {
                TrustBundle::new(
                    instance.trust.jwks_uri.clone(),
                    instance.trust.jwks.as_ref(),
                )
            },
        )),
    })
}

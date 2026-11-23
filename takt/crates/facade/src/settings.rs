//! What the app reads from the project's settings (the Python side's `django.conf.settings`).
//!
//! The `rekuest` crate builds this from the configuration, as `rekuest/settings.py` does.

use std::sync::Arc;
use std::time::Duration;

use crate::provenance::keys::InstanceKey;

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
    /// `PROVENANCE`: how provenance tokens are minted.
    pub provenance: ProvenanceSettings,
    /// `INSTANCE["PRIVATE_KEY"]`: this instance's Ed25519 key. Signs provenance tokens and
    /// verifies the internal API's service tokens. `None` only where nothing is minted (tests).
    pub instance_key: Option<Arc<InstanceKey>>,
    /// `REKUEST_IDENTIFIER`: what this rekuest signs as, and what service tokens are for.
    pub rekuest_identifier: String,
    /// `AUTHENTIKATE["hub"]`: the uuid the hub generated for itself. Service tokens carry it, and
    /// one checked against the hub's bundle must be for it. None: not checked (a hub of none).
    pub hub: Option<String>,
    /// Where the Python server answers, script name included (`rekuest.server_url`): takt asks
    /// it for the upkeep job. `None` turns upkeep off (tests).
    pub server_url: Option<String>,
    /// `PROBE_TTL_SECONDS`: a live probe's redis state expires this long after its last write.
    pub probe_ttl: Duration,
    /// `PROBE_LINGER_SECONDS`: how long a finished probe's state stays for late subscribers.
    pub probe_linger: Duration,
    /// `PROBE_MAX_INFLIGHT_PER_CALLER`.
    pub probe_max_inflight: i64,
    /// `HOOK_SIGNATURE_MODE`: `compat` accepts and sends the legacy body-only signature beside
    /// V1, `strict` only V1.
    pub hook_signature_strict: bool,
    /// `HOOK_MAX_SKEW`: how far a V1 signature's timestamp may be from now, either way.
    pub hook_max_skew: i64,
    /// `TASK_RETENTION_SECONDS`: terminal root task trees older than this are deleted; zero
    /// keeps them forever.
    pub task_retention: Duration,
    /// `EPHEMERAL_TASK_RETENTION_SECONDS`: the same for ephemeral trees (a schedule's
    /// housekeeping runs), which have a short horizon of their own.
    pub ephemeral_task_retention: Duration,
    /// `SIGNAL_RETENTION_SECONDS`: processed signals older than this are deleted; zero keeps them.
    pub signal_retention: Duration,
    /// `TRIGGER_MAX_DEPTH`: how many trigger firings may chain before a signal stops firing.
    pub trigger_max_depth: i16,
    /// `DEPENDENCY_MAX_DEPTH`: how many levels of dependencies an assign resolves below the
    /// assigned implementation before it refuses.
    pub dependency_max_depth: usize,
    /// `SERVICES`: this hub's services, whose signals are signed with their instance keys.
    pub services: Vec<TrustedInstance>,
    /// `HOOK_AGENTS`: this hub's hook agents, reached and answering with instance keys. A
    /// separate list: a hook agent is not a service's.
    pub hook_agents: Vec<TrustedInstance>,
    /// The hub's trust bundle (`INSTANCE["TRUST_JWKS_URI"]` / `["TRUST_JWKS"]`).
    pub trust_bundle: Arc<crate::service_trust::TrustBundle>,
    /// Caller sessions (`/session`, [`crate::session`]).
    pub session: SessionSettings,
}

/// How caller sessions behave (`rekuest.session_*`; docs/session-protocol.md).
#[derive(Debug, Clone)]
pub struct SessionSettings {
    /// How long a session whose socket is gone may be taken back (`HELLO` naming it).
    pub grace: Duration,
    /// How often a connected session's `last_seen` is written to Postgres (the dead man
    /// switch reads it): well under the shortest switch.
    pub beat: Duration,
    /// Without a frame from the window this long, the socket is closed (the window pings).
    pub idle_timeout: Duration,
    /// The most items one replay sends; past it, the topic gets a `RESYNC`.
    pub replay_max: i64,
    /// The most rows a list topic's snapshot holds.
    pub list_snapshot: i64,
    /// How often a session checks its watched tasks and states against Postgres for an item
    /// the live feed lost.
    pub watch_check: Duration,
    /// How long an answer is kept for a request sent again.
    pub request_memory: Duration,
    /// The most pushes kept since the window last said how far it got.
    pub resume_window: usize,
    /// The most bytes queued for a window before its slowest topics are sent a `RESYNC`.
    pub buffer_max: usize,
    /// The shortest dead man switch accepted, in seconds.
    pub deadman_min: u32,
}

impl Default for SessionSettings {
    fn default() -> Self {
        Self {
            grace: Duration::from_secs(60),
            beat: Duration::from_secs(5),
            idle_timeout: Duration::from_secs(45),
            replay_max: 500,
            list_snapshot: 100,
            watch_check: Duration::from_secs(30),
            request_memory: Duration::from_secs(600),
            resume_window: 4096,
            buffer_max: 4 * 1024 * 1024,
            deadman_min: 15,
        }
    }
}

/// One entry of `SERVICES` or `HOOK_AGENTS`, as far as trust needs it: its name there, and the
/// identifier its instance signs as (default: its name in the hub).
#[derive(Debug, Clone, PartialEq)]
pub struct TrustedInstance {
    pub name: String,
    pub identifier: Option<String>,
}

/// `settings.PROVENANCE`, less the key (which is [`Settings::instance_key`]).
#[derive(Debug, Clone)]
pub struct ProvenanceSettings {
    /// `ISSUER`: the token's `iss`.
    pub issuer: String,
    /// `TOKEN_TTL_SECONDS`.
    pub token_ttl: Duration,
    /// `HUMAN_ROLES`: the roles that mark an accountable human; empty disables the check.
    pub human_roles: Vec<String>,
    /// `STRICT`: refuse the assign (rather than skip the token) when the root is no human.
    pub strict: bool,
}

impl Default for ProvenanceSettings {
    fn default() -> Self {
        Self {
            issuer: "rekuest".into(),
            token_ttl: Duration::from_secs(3600),
            human_roles: vec![],
            strict: false,
        }
    }
}

impl Default for Settings {
    /// The Python server's defaults (`rekuest/settings.py`, `rekuest/configuration.py`).
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
            provenance: ProvenanceSettings::default(),
            instance_key: None,
            rekuest_identifier: "rekuest".into(),
            hub: None,
            server_url: None,
            probe_ttl: Duration::from_secs(3600),
            probe_linger: Duration::from_secs(300),
            probe_max_inflight: 32,
            hook_signature_strict: false,
            hook_max_skew: 300,
            task_retention: Duration::ZERO,
            ephemeral_task_retention: Duration::from_secs(86400),
            signal_retention: Duration::from_secs(604_800),
            trigger_max_depth: 3,
            dependency_max_depth: 8,
            services: vec![],
            hook_agents: vec![],
            trust_bundle: Arc::default(),
            session: SessionSettings::default(),
        }
    }
}

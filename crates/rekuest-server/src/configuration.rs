//! The rekuest server's `config.yaml` (`rekuest/configuration.py`), read by agentd too.
//!
//! One file configures both processes: the Python server (GraphQL, subscriptions) and agentd
//! (the agent protocol). agentd reads only the blocks it needs; everything else in the file is
//! ignored. Field names and defaults follow `rekuest/configuration.py`.

use std::path::Path;

use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Clone, Deserialize)]
pub struct Configuration {
    #[serde(default)]
    pub django: DjangoBlock,
    pub postgres: PostgresBlock,
    pub redis: RedisBlock,
    /// authentikate's settings (issuers, audience, static tokens), read in Phase 1.
    #[serde(default)]
    pub authentikate: Value,
    #[serde(default)]
    pub rekuest: RekuestBlock,
    #[serde(default)]
    pub provenance: ProvenanceBlock,
    pub instance: Option<InstanceBlock>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct DjangoBlock {
    /// Debug mode: static tokens are accepted only while it is on.
    #[serde(default)]
    pub debug: bool,
    /// The URL prefix the server is mounted under (e.g. `rekuest`).
    #[serde(default)]
    pub force_script_name: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PostgresBlock {
    pub db_name: String,
    pub username: String,
    pub password: String,
    pub host: String,
    #[serde(default = "default_pg_port")]
    pub port: u16,
}

fn default_pg_port() -> u16 {
    5432
}

impl PostgresBlock {
    pub fn url(&self) -> String {
        format!(
            "postgres://{}:{}@{}:{}/{}",
            self.username, self.password, self.host, self.port, self.db_name
        )
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct RedisBlock {
    pub host: String,
    #[serde(default = "default_redis_port")]
    pub port: u16,
    /// Namespace of every key the protocol writes (agent queues, probes, the tick token).
    #[serde(default = "default_prefix")]
    pub key_prefix: String,
    /// The channels_redis layer's prefix, shared with the Python server's fan-out.
    #[serde(default = "default_prefix")]
    pub channel_prefix: String,
    #[serde(default = "default_capacity")]
    pub channel_capacity: u64,
}

fn default_redis_port() -> u16 {
    6379
}

fn default_prefix() -> String {
    "rekuest".into()
}

fn default_capacity() -> u64 {
    5000
}

impl RedisBlock {
    pub fn url(&self) -> String {
        format!("redis://{}:{}/", self.host, self.port)
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct RekuestBlock {
    #[serde(default = "d30")]
    pub grace_default: u64,
    #[serde(default = "d5")]
    pub sweep_interval: u64,
    #[serde(default = "d60")]
    pub pickup_deadline: u64,
    #[serde(default = "d3600")]
    pub disconnected_expiry: u64,
    #[serde(default = "d60")]
    pub control_deadline: u64,
    #[serde(default = "compat")]
    pub hook_signature_mode: String,
    #[serde(default = "d300")]
    pub hook_max_skew: u64,
    /// What this rekuest signs as, and what service tokens to it must be for.
    #[serde(default = "rekuest_identifier")]
    pub identifier: String,
    #[serde(default = "d3600")]
    pub probe_ttl: u64,
    #[serde(default = "d300")]
    pub probe_linger: u64,
    #[serde(default = "d32")]
    pub probe_max_inflight: i64,
}

impl Default for RekuestBlock {
    fn default() -> Self {
        serde_json::from_value(serde_json::json!({})).expect("every field has a default")
    }
}

fn d5() -> u64 {
    5
}
fn d30() -> u64 {
    30
}
fn d60() -> u64 {
    60
}
fn d300() -> u64 {
    300
}
fn d3600() -> u64 {
    3600
}
fn d32() -> i64 {
    32
}
fn rekuest_identifier() -> String {
    "live.arkitekt.rekuest".into()
}
fn compat() -> String {
    "compat".into()
}

#[derive(Debug, Clone, Deserialize)]
pub struct ProvenanceBlock {
    #[serde(default = "issuer")]
    pub issuer: String,
    #[serde(default = "d3600")]
    pub token_ttl_seconds: u64,
    #[serde(default)]
    pub human_roles: Vec<String>,
    #[serde(default)]
    pub strict: bool,
}

impl Default for ProvenanceBlock {
    fn default() -> Self {
        serde_json::from_value(serde_json::json!({})).expect("every field has a default")
    }
}

fn issuer() -> String {
    "rekuest".into()
}

#[derive(Debug, Clone, Deserialize)]
pub struct InstanceBlock {
    /// Ed25519 private key (PKCS#8 PEM): signs provenance tokens and verifies service tokens.
    pub private_key: String,
}

impl Configuration {
    pub fn load(path: impl AsRef<Path>) -> anyhow::Result<Self> {
        let path = path.as_ref();
        let text = std::fs::read_to_string(path)
            .map_err(|e| anyhow::anyhow!("reading {}: {e}", path.display()))?;
        serde_yaml::from_str(&text).map_err(|e| anyhow::anyhow!("parsing {}: {e}", path.display()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_servers_config_and_ignores_the_rest() {
        let config: Configuration = serde_yaml::from_str(
            r#"
django:
  secret_key: s
  debug: true
  force_script_name: rekuest
postgres:
  db_name: rekuest
  username: u
  password: p
  host: db
redis:
  host: redis
  channel_prefix: rekuest
embeddings:
  enabled: false
rekuest:
  pickup_deadline: 0
"#,
        )
        .unwrap();
        assert!(config.django.debug);
        assert_eq!(config.postgres.url(), "postgres://u:p@db:5432/rekuest");
        assert_eq!(config.redis.key_prefix, "rekuest");
        assert_eq!(config.rekuest.pickup_deadline, 0);
        assert_eq!(config.rekuest.grace_default, 30);
        assert_eq!(config.provenance.issuer, "rekuest");
        assert_eq!(config.rekuest.identifier, "live.arkitekt.rekuest");
        assert_eq!(config.rekuest.probe_max_inflight, 32);
    }
}

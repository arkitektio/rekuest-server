//! The frames, as the server reads them (`facade/messages.py`).
//!
//! The wire types are `rekuest-protocol`'s. What only a server needs is here: the declaration a
//! `REGISTER` carries, parsed as the registration will validate it.

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub use rekuest_protocol::messages::*;

/// One GPU of an agent's machine.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentGpu {
    pub name: String,
    #[serde(default)]
    pub memory_mib: Option<i32>,
}

/// What an agent's machine has, as a runtime formula reads it. Unknown stays null.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AgentHardware {
    #[serde(default)]
    pub cpu_count: Option<i32>,
    #[serde(default)]
    pub memory_mib: Option<i32>,
    #[serde(default)]
    pub gpus: Vec<AgentGpu>,
}

/// What a `REGISTER` declares besides the token: the agent's name and, when it declares at all,
/// its implementations, states, locks, bloks and assets (validated by `facade::registration`).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct RegisterDeclaration {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub implementations: Option<Vec<Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub states: Option<Vec<Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub locks: Option<Vec<Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bloks: Option<Vec<Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub assets: Option<Vec<Value>>,
    /// The app the agent says controls its lifecycle (e.g. `plugin_engine`). A self-declared
    /// hint written on every `REGISTER`; not part of the declaration (or its hash).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub supervisor: Option<String>,
    /// What the agent's machine has. Written on every `REGISTER`, like the supervisor, and not
    /// part of the declaration (or its hash): moving machines declares nothing new.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hardware: Option<AgentHardware>,
}

impl RegisterDeclaration {
    /// Whether this `REGISTER` carries a declaration to reconcile.
    pub fn declares(&self) -> bool {
        self.hash.is_some()
            || self.implementations.is_some()
            || self.states.is_some()
            || self.locks.is_some()
            || self.bloks.is_some()
            || self.assets.is_some()
    }
}

/// A frame from an agent, as this server parses it.
pub type AgentFrame = Envelope<RegisterDeclaration>;

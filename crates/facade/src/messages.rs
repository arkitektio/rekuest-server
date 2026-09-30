//! The frames, as the server reads them (`facade/messages.py`).
//!
//! The wire types are `rekuest-protocol`'s. What only a server needs is here: the declaration a
//! `REGISTER` carries, parsed as the registration will validate it.

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub use rekuest_protocol::messages::*;

/// What a `REGISTER` declares besides the token: the agent's name and, when it declares at all,
/// its implementations, states, locks and bloks (validated by `facade::registration`).
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
}

impl RegisterDeclaration {
    /// Whether this `REGISTER` carries a declaration to reconcile.
    pub fn declares(&self) -> bool {
        self.hash.is_some()
            || self.implementations.is_some()
            || self.states.is_some()
            || self.locks.is_some()
            || self.bloks.is_some()
    }
}

/// A frame from an agent, as this server parses it.
pub type AgentFrame = Envelope<RegisterDeclaration>;
/// A message from an agent, as this server parses it.
pub type AgentMessage = FromAgent<RegisterDeclaration>;

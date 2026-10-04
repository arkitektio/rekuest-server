//! This process's live agent connections, by agent: what the Python side's `agent-{id}`
//! channel-layer group is for. A connection that wins the lease tells the others of its agent
//! to stop (`kick_others`). A connection on another replica is fenced by the lease itself: its
//! next delivery or renewal finds it is no longer the active connection and closes.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use tokio::sync::mpsc;

/// What one connection can be told from outside.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Control {
    /// Another connection of this agent took the lease: stop and close with `AGENT_REPLACED`.
    Displace,
}

/// One agent's connections in this process, by connection id.
type Group = HashMap<String, mpsc::UnboundedSender<Control>>;

#[derive(Clone, Default)]
pub struct Connections {
    inner: Arc<Mutex<HashMap<i64, Group>>>,
}

impl Connections {
    /// Join the agent's group (`register_connection`).
    pub fn join(&self, agent: i64, connection: &str, control: mpsc::UnboundedSender<Control>) {
        self.inner
            .lock()
            .expect("connections lock")
            .entry(agent)
            .or_default()
            .insert(connection.to_owned(), control);
    }

    /// Leave it again (on disconnect).
    pub fn leave(&self, agent: i64, connection: &str) {
        let mut inner = self.inner.lock().expect("connections lock");
        if let Some(group) = inner.get_mut(&agent) {
            group.remove(connection);
            if group.is_empty() {
                inner.remove(&agent);
            }
        }
    }

    /// Tell every other connection of `agent` in this process to stop (`kick_others`).
    pub fn kick_others(&self, agent: i64, initiator: &str) -> usize {
        let inner = self.inner.lock().expect("connections lock");
        inner
            .get(&agent)
            .map(|group| {
                group
                    .iter()
                    .filter(|(connection, _)| connection.as_str() != initiator)
                    .filter(|(_, control)| control.send(Control::Displace).is_ok())
                    .count()
            })
            .unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_others_are_kicked() {
        let connections = Connections::default();
        let (mine, mut my_inbox) = mpsc::unbounded_channel();
        let (theirs, mut their_inbox) = mpsc::unbounded_channel();
        connections.join(1, "mine", mine);
        connections.join(1, "theirs", theirs);

        assert_eq!(connections.kick_others(1, "mine"), 1);
        assert_eq!(their_inbox.try_recv(), Ok(Control::Displace));
        assert!(my_inbox.try_recv().is_err());

        connections.leave(1, "theirs");
        assert_eq!(connections.kick_others(1, "mine"), 0);
    }
}

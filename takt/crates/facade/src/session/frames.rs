//! The frames of a caller session (docs/session-protocol.md §3).
//!
//! JSON, `type` in SCREAMING_SNAKE_CASE, fields in snake_case, as the agent protocol's frames.
//! Every push the server makes to a window that the window must not miss carries the session's
//! `pos` (numbered per session); the window reports the newest it received on every `PING`.
//! `tests/fixtures/session_wire_examples.json` holds one of each, set in full: the contract
//! both sides test against.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::backend::AssignInput;
use crate::probes::backend::ProbeInput;

/// What a window sends.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "type", rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ClientFrame {
    /// The first frame: who the window is, and (resuming) the session it had and how far it got.
    Hello {
        token: String,
        #[serde(default)]
        session: Option<String>,
        #[serde(default)]
        last_pos: Option<u64>,
        #[serde(default)]
        client_info: Value,
    },
    /// A fresh token for a socket that outlives the one it opened with.
    Reauth {
        token: String,
    },
    /// Follow a topic (§4): a `RESYNC` with its snapshot comes back, then its items.
    Watch {
        topic: String,
    },
    Unwatch {
        topic: String,
    },
    /// Start a task (the GraphQL `assign`'s input). `deadman`: the switch, in seconds (§7).
    Assign {
        request: String,
        input: Box<AssignInput>,
        #[serde(default)]
        deadman: Option<u32>,
    },
    Cancel {
        request: String,
        task: String,
    },
    Interrupt {
        request: String,
        task: String,
    },
    Pause {
        request: String,
        task: String,
    },
    Resume {
        request: String,
        task: String,
        #[serde(default)]
        step: bool,
    },
    /// Answer a held task.
    Decide {
        request: String,
        task: String,
        approved: bool,
        #[serde(default)]
        answer: Option<String>,
        #[serde(default)]
        values: Option<Map<String, Value>>,
        #[serde(default)]
        remember: Option<String>,
    },
    /// Ask a task's agent whether it is still on it.
    Inquire {
        request: String,
        task: String,
    },
    /// Arm the dead man switch on this session (`seconds`), or disarm it (`null`).
    Deadman {
        request: String,
        task: String,
        seconds: Option<u32>,
    },
    Probe {
        request: String,
        input: Box<ProbeInput>,
    },
    ProbeCancel {
        request: String,
        probe: String,
    },
    ProbePause {
        request: String,
        probe: String,
    },
    ProbeResume {
        request: String,
        probe: String,
    },
    /// Liveness, and how far the window got (the newest `pos` it received).
    Ping {
        #[serde(default)]
        pos: Option<u64>,
    },
}

impl ClientFrame {
    /// The request id of a call, for its answer.
    pub fn request(&self) -> Option<&str> {
        match self {
            ClientFrame::Assign { request, .. }
            | ClientFrame::Cancel { request, .. }
            | ClientFrame::Interrupt { request, .. }
            | ClientFrame::Pause { request, .. }
            | ClientFrame::Resume { request, .. }
            | ClientFrame::Decide { request, .. }
            | ClientFrame::Inquire { request, .. }
            | ClientFrame::Deadman { request, .. }
            | ClientFrame::Probe { request, .. }
            | ClientFrame::ProbeCancel { request, .. }
            | ClientFrame::ProbePause { request, .. }
            | ClientFrame::ProbeResume { request, .. } => Some(request),
            _ => None,
        }
    }
}

/// What the window may count on, told at `WELCOME`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Limits {
    /// Seconds a gone window may resume within.
    pub session_grace: u64,
    /// Seconds between the window's pings (it is closed after a few missed).
    pub ping_interval: u64,
    /// The most items one replay sends.
    pub replay_max: i64,
    /// The shortest dead man switch accepted, in seconds.
    pub deadman_min: u32,
}

/// The answer to a call, before it is numbered: kept as it is for a request sent again.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Answer {
    /// The task an `ASSIGN` started (or found again), with its record (a `TaskChange`).
    Assigned {
        request: String,
        reference: String,
        task: String,
        created: bool,
        record: Value,
    },
    /// The call was taken; `result` is what it returns, if anything (an inquiry, a probe).
    Accepted {
        request: String,
        #[serde(default, skip_serializing_if = "Value::is_null")]
        result: Value,
    },
    /// The call was refused, and why, as a sentence.
    Refused { request: String, error: String },
}

impl Answer {
    pub fn request(&self) -> &str {
        match self {
            Answer::Assigned { request, .. }
            | Answer::Accepted { request, .. }
            | Answer::Refused { request, .. } => request,
        }
    }

    /// The answer as the frame that carries `pos`.
    pub fn numbered(self, pos: u64) -> ServerFrame {
        match self {
            Answer::Assigned {
                request,
                reference,
                task,
                created,
                record,
            } => ServerFrame::Assigned {
                pos,
                request,
                reference,
                task,
                created,
                record,
            },
            Answer::Accepted { request, result } => ServerFrame::Accepted {
                pos,
                request,
                result,
            },
            Answer::Refused { request, error } => ServerFrame::Refused {
                pos,
                request: Some(request),
                topic: None,
                error,
            },
        }
    }
}

/// What the server sends.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ServerFrame {
    Welcome {
        session: String,
        /// Whether the session named in `HELLO` was taken back (its watches with it).
        resumed: bool,
        limits: Limits,
    },
    /// One item of a watched topic.
    Data {
        pos: u64,
        topic: String,
        item: Value,
    },
    /// Replace what you hold for this topic with `snapshot` (§5).
    Resync {
        pos: u64,
        topic: String,
        reason: String,
        snapshot: Value,
    },
    Assigned {
        pos: u64,
        request: String,
        reference: String,
        task: String,
        created: bool,
        record: Value,
    },
    Accepted {
        pos: u64,
        request: String,
        #[serde(default, skip_serializing_if = "Value::is_null")]
        result: Value,
    },
    /// A call (`request`) or a watch (`topic`) refused.
    Refused {
        pos: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        request: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        topic: Option<String>,
        error: String,
    },
    Pong,
    /// The server is about to close the socket (`token_expired`, `shutting_down`, …).
    Closing {
        reason: String,
    },
}

/// `RESYNC` reasons (§5.2).
pub mod reason {
    /// A new watch: its snapshot.
    pub const NEW: &str = "new";
    /// A resumed session's list topic: its rows as they are now.
    pub const RESUMED: &str = "resumed";
    pub const TOO_FAR_BEHIND: &str = "too_far_behind";
    pub const PRUNED: &str = "pruned";
    pub const AGENT_RESTARTED: &str = "agent_restarted";
    pub const SLOW_CONSUMER: &str = "slow_consumer";
    /// The state's record changed beyond a patch (a new snapshot, a new definition).
    pub const STATE_REPLACED: &str = "state_replaced";
    /// A state's patch arrived after a newer one: the state is sent whole instead.
    pub const OUT_OF_ORDER: &str = "out_of_order";
}

/// Close codes of a session socket (4400–4499: the window should not retry blindly).
pub mod close {
    /// The first frame was not a `HELLO`, or a frame did not parse.
    pub const PROTOCOL: u16 = 4400;
    /// The token was refused: sign in again, do not retry with it.
    pub const UNAUTHORIZED: u16 = 4401;
    /// Nothing heard from the window for `idle_timeout`.
    pub const IDLE: u16 = 4408;
    /// Another connection took the session over (the window came back on a new socket).
    pub const SUPERSEDED: u16 = 4409;
    /// The server's own trouble: retry later.
    pub const UNAVAILABLE: u16 = 4503;
}

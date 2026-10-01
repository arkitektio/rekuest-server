//! The websocket close codes the agent protocol uses (`facade/codes.py`).

/// The agent did not answer a heartbeat in time.
pub const HEARTBEAT_NOT_RESPONDED_CODE: u16 = 3001;
pub const FROM_AGENT_MESSAGE_IS_NOT_VALID_JSON_CODE: u16 = 3002;
pub const FROM_AGENT_MESSAGE_DOES_NOT_MATCH_SCHEMA_CODE: u16 = 3003;
pub const FROM_AGENT_MESSAGE_RECEIVED_BEFORE_REGISTRATION_CODE: u16 = 3004;
/// Server-side delivery to this socket failed; the agent should simply reconnect.
pub const AGENT_TRANSPORT_FAILED_CODE: u16 = 3005;
pub const AGENT_IS_BLOCKED_CODE: u16 = 4003;
/// Another connection is already live and `force` was not set.
pub const AGENT_ALREADY_CONNECTED_CODE: u16 = 4004;
/// The incumbent connection was kicked: a newer connection registered with `force`, or the
/// same process reconnected.
pub const AGENT_REPLACED_CODE: u16 = 4005;
/// The declaration `REGISTER` carried was refused (catalog mismatch, ownership conflict, …).
pub const AGENT_REGISTRATION_REJECTED_CODE: u16 = 4006;

//! Map a task event to its caller-bound `…_EVENT` mirror (`facade/caller_events.py`).
//!
//! The participant that originated work (an `ASSIGN_REQUEST`) receives a minimal per-kind mirror
//! of each event of that work over its own socket. Pure and database-free. `event` and `seq`
//! both derive from the event's id: `event` is the dedup handle, `seq` the order.

use serde_json::Value;

use crate::channel_events::TaskEventPayload;
use crate::channels;
use crate::messages::{ExecutionEvent, LogLevel, ToAgent};

/// What [`build_execution_event`] reads (`EventLike`): a persisted event, the payload of one
/// off the channel layer, or a probe's event (whose id is its per-probe seq).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct EventLike {
    pub id: u64,
    pub task: String,
    pub kind: String,
    pub message: Option<String>,
    pub progress: Option<i64>,
    pub returns: Option<Value>,
    pub level: Option<String>,
    pub value: Option<Value>,
}

impl EventLike {
    /// `_PayloadEventLike`: a `TaskEventPayload` as the channel layer carries it.
    pub fn from_payload(payload: &Value) -> Option<Self> {
        let id = match payload.get("id")? {
            Value::String(id) => id.parse().ok()?,
            Value::Number(id) => id.as_u64()?,
            _ => return None,
        };
        Some(Self {
            id,
            task: string_of(payload.get("task")?)?,
            kind: payload.get("kind")?.as_str()?.to_owned(),
            message: payload
                .get("message")
                .and_then(Value::as_str)
                .map(str::to_owned),
            progress: payload.get("progress").and_then(Value::as_i64),
            returns: payload.get("returns").filter(|r| !r.is_null()).cloned(),
            level: payload
                .get("level")
                .and_then(Value::as_str)
                .map(str::to_owned),
            value: payload.get("value").filter(|v| !v.is_null()).cloned(),
        })
    }

    /// `_ProbeEventLike`: a `ProbeEventBroadcast`; `id` is the per-probe seq, `task` the probe.
    pub fn from_probe_payload(payload: &Value) -> Option<Self> {
        Some(Self {
            id: payload.get("seq")?.as_u64()?,
            task: payload.get("probe")?.as_str()?.to_owned(),
            kind: payload.get("kind")?.as_str()?.to_owned(),
            message: payload
                .get("message")
                .and_then(Value::as_str)
                .map(str::to_owned),
            progress: payload.get("progress").and_then(Value::as_i64),
            returns: payload.get("returns").filter(|r| !r.is_null()).cloned(),
            level: payload
                .get("level")
                .and_then(Value::as_str)
                .map(str::to_owned),
            value: payload.get("value").filter(|v| !v.is_null()).cloned(),
        })
    }
}

impl From<&TaskEventPayload> for EventLike {
    fn from(payload: &TaskEventPayload) -> Self {
        Self {
            id: payload.id.parse().unwrap_or_default(),
            task: payload.task.clone(),
            kind: payload.kind.clone(),
            message: payload.message.clone(),
            progress: payload.progress,
            returns: payload.returns.clone(),
            level: payload.level.clone(),
            value: payload.value.clone(),
        }
    }
}

fn string_of(value: &Value) -> Option<String> {
    match value {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

fn level(level: Option<&str>) -> LogLevel {
    match level {
        Some("DEBUG") => LogLevel::Debug,
        Some("WARN") => LogLevel::Warn,
        Some("ERROR") => LogLevel::Error,
        Some("CRITICAL") => LogLevel::Critical,
        _ => LogLevel::Info,
    }
}

/// The mirror of `event`, or `None` for a kind that is not forwarded (`UNASSIGN`, unknown).
pub fn build_execution_event(event: &EventLike) -> Option<ToAgent> {
    let base = ExecutionEvent {
        task: event.task.clone(),
        event: event.id.to_string(),
        seq: event.id,
    };
    Some(match event.kind.as_str() {
        "PROGRESS" => ToAgent::ProgressEvent {
            event: base,
            progress: event.progress,
            message: event.message.clone(),
        },
        "YIELD" => ToAgent::YieldEvent {
            event: base,
            returns: event.returns.as_ref().and_then(Value::as_object).cloned(),
        },
        "LOG" => ToAgent::LogEvent {
            event: base,
            message: event.message.clone(),
            level: Some(level(event.level.as_deref())),
        },
        "FAILED" => ToAgent::FailedEvent {
            event: base,
            error: event.message.clone(),
        },
        "CRITICAL" => ToAgent::CriticalEvent {
            event: base,
            error: event.message.clone(),
        },
        "LOST" => {
            let details = event.value.as_ref().and_then(Value::as_object);
            let get = |key: &str| details.and_then(|d| d.get(key)).filter(|v| !v.is_null());
            ToAgent::LostEvent {
                event: base,
                started: Some(get("started").is_none_or(is_truthy)),
                last_progress: get("last_progress").and_then(Value::as_i64),
                effects: get("effects").and_then(Value::as_str).map(str::to_owned),
                reason: get("reason")
                    .filter(|r| is_truthy(r))
                    .and_then(Value::as_str)
                    .map(str::to_owned)
                    .or_else(|| event.message.clone()),
            }
        }
        "COMPLETED" => ToAgent::CompletedEvent(base),
        "BOUND" => ToAgent::BoundEvent(base),
        "QUEUED" => ToAgent::QueuedEvent(base),
        "STARTED" => ToAgent::StartedEvent(base),
        "DELEGATE" => ToAgent::DelegateEvent(base),
        "CANCELLING" => ToAgent::CancellingEvent(base),
        "CANCELLED" => ToAgent::CancelledEvent(base),
        "INTERRUPTING" => ToAgent::InterruptingEvent(base),
        "INTERRUPTED" => ToAgent::InterruptedEvent(base),
        "PAUSING" => ToAgent::PausingEvent(base),
        "PAUSED" => ToAgent::PausedEvent(base),
        "RESUMING" => ToAgent::ResumingEvent(base),
        "RESUMED" => ToAgent::ResumedEvent(base),
        _ => return None,
    })
}

/// The mirror of a message off the caller group (`channel_TaskEventCreatedEvent`,
/// `channel_probe_event_broadcast`). Only a task event's `event` branch is forwarded: a `create`
/// is covered by the `ASSIGN_RESPONSE`, and forwarding it too would race it.
pub fn mirror_of_channel_message(message: &Value) -> Option<ToAgent> {
    let event = if let Some(payload) = kante::channel::payload_of(message, channels::TASK_EVENT) {
        EventLike::from_payload(payload.get("event").filter(|e| !e.is_null())?)
    } else if let Some(payload) = kante::channel::payload_of(message, channels::PROBE_EVENT) {
        EventLike::from_probe_payload(payload)
    } else {
        None
    }?;
    build_execution_event(&event)
}

/// Python truthiness of a JSON value.
fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn mirrors_carry_the_event_id_as_seq() {
        let event = EventLike {
            id: 42,
            task: "7".into(),
            kind: "LOG".into(),
            message: Some("hi".into()),
            level: Some("NOPE".into()),
            ..EventLike::default()
        };
        let mirror = serde_json::to_value(build_execution_event(&event).unwrap()).unwrap();
        assert_eq!(
            mirror,
            json!({"type": "LOG_EVENT", "task": "7", "event": "42", "seq": 42, "message": "hi", "level": "INFO"})
        );
        let unassign = EventLike {
            kind: "UNASSIGN".into(),
            ..event
        };
        assert!(build_execution_event(&unassign).is_none());
    }

    #[test]
    fn a_probe_payload_uses_its_seq_and_probe_id() {
        let payload = json!({"probe": "p-1", "kind": "COMPLETED", "seq": 3, "message": null});
        let event = EventLike::from_probe_payload(&payload).unwrap();
        assert_eq!((event.id, event.task.as_str()), (3, "p-1"));
    }

    #[test]
    fn only_the_event_branch_of_a_channel_message_is_mirrored() {
        let event = json!({"type": "channel.TaskEventCreatedEvent", "message": {"create": null, "event": {
            "id": "9", "task": "4", "kind": "STARTED", "message": null, "progress": null,
            "returns": null, "level": null, "value": null, "created_at": "2026-09-30T09:44:08Z",
        }}});
        assert_eq!(
            serde_json::to_value(mirror_of_channel_message(&event).unwrap()).unwrap(),
            json!({"type": "STARTED_EVENT", "task": "4", "event": "9", "seq": 9})
        );
        let create = json!({"type": "channel.TaskEventCreatedEvent", "message": {"event": null, "create": {"id": "4"}}});
        assert!(mirror_of_channel_message(&create).is_none());
        let probe = json!({"type": "channel.probe_event_broadcast", "message": {
            "probe": "p-1", "kind": "PROGRESS", "seq": 2, "progress": 50, "message": null,
        }});
        assert_eq!(
            serde_json::to_value(mirror_of_channel_message(&probe).unwrap()).unwrap(),
            json!({"type": "PROGRESS_EVENT", "task": "p-1", "event": "2", "seq": 2, "progress": 50})
        );
        let other = json!({"type": "channel.child_task_feed", "message": {}});
        assert!(mirror_of_channel_message(&other).is_none());
    }

    #[test]
    fn a_lost_event_reads_its_details() {
        let event = EventLike {
            id: 1,
            task: "1".into(),
            kind: "LOST".into(),
            message: Some("gone".into()),
            value: Some(json!({"started": false, "last_progress": 40})),
            ..EventLike::default()
        };
        let ToAgent::LostEvent {
            started,
            last_progress,
            reason,
            ..
        } = build_execution_event(&event).unwrap()
        else {
            panic!()
        };
        assert_eq!(
            (started, last_progress, reason.as_deref()),
            (Some(false), Some(40), Some("gone"))
        );
    }
}

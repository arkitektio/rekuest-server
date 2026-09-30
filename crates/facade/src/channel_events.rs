//! The payloads of the change feeds (`facade/channel_events.py`), serialized exactly as
//! pydantic's `model_dump(mode="json")` does: every field present (unset ones as null) and
//! datetimes as ISO 8601 in UTC with a `Z`, microseconds only when there are any.

use chrono::{DateTime, Timelike, Utc};
use serde::{Serialize, Serializer};
use serde_json::Value;

/// A datetime as pydantic writes it.
pub fn pydantic_datetime(at: &DateTime<Utc>) -> String {
    if at.nanosecond() / 1000 == 0 {
        at.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        at.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
    }
}

fn datetime<S: Serializer>(at: &DateTime<Utc>, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&pydantic_datetime(at))
}

fn optional_datetime<S: Serializer>(at: &Option<DateTime<Utc>>, s: S) -> Result<S::Ok, S::Error> {
    match at {
        Some(at) => datetime(at, s),
        None => s.serialize_none(),
    }
}

/// `StateUpdateEvent`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct StateUpdateEvent {
    pub state: i64,
}

/// `PatchEvent`: the whole patch, so subscribers need not re-fetch it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PatchEvent {
    pub create: i64,
    pub state: i64,
    pub agent: Option<i64>,
    pub interface: String,
    pub op: String,
    pub path: String,
    pub value: Value,
    pub global_rev: i64,
    pub session: Option<i64>,
    #[serde(serialize_with = "optional_datetime")]
    pub timestamp: Option<DateTime<Utc>>,
}

/// `TaskChangePayload`: a snapshot of a task for the change feeds. `revision` orders the
/// changes, which the channel layer does not deliver in commit order.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TaskChangePayload {
    pub id: String,
    pub reference: Option<String>,
    pub is_done: bool,
    pub latest_event_kind: String,
    pub latest_instruct_kind: String,
    pub status_message: Option<String>,
    pub action: String,
    pub implementation: Option<String>,
    pub agent: Option<String>,
    pub root: Option<String>,
    pub parent: Option<String>,
    #[serde(serialize_with = "datetime")]
    pub created_at: DateTime<Utc>,
    #[serde(serialize_with = "datetime")]
    pub updated_at: DateTime<Utc>,
    #[serde(serialize_with = "optional_datetime")]
    pub finished_at: Option<DateTime<Utc>>,
    pub revision: i64,
}

/// `TaskEventPayload`: a persisted task event; `id` stays the event's primary key.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TaskEventPayload {
    pub id: String,
    pub task: String,
    pub kind: String,
    pub message: Option<String>,
    pub progress: Option<i64>,
    pub returns: Option<Value>,
    pub level: Option<String>,
    pub value: Option<Value>,
    #[serde(serialize_with = "datetime")]
    pub created_at: DateTime<Utc>,
}

/// `TaskEventCreatedEvent`: a persisted event, or a freshly created root task.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TaskEventCreatedEvent {
    pub event: Option<TaskEventPayload>,
    pub create: Option<TaskChangePayload>,
}

/// `ChildTaskEvent`: a task created or updated.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ChildTaskEvent {
    pub create: Option<TaskChangePayload>,
    pub update: Option<TaskChangePayload>,
}

/// `AgentEvent`, `ImplementationEvent` and `ActionEvent`: the id of the row that changed.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct CrudEvent {
    pub create: Option<i64>,
    pub update: Option<i64>,
    pub delete: Option<i64>,
}

impl CrudEvent {
    pub fn saved(id: i64, created: bool) -> Self {
        if created {
            Self {
                create: Some(id),
                ..Self::default()
            }
        } else {
            Self {
                update: Some(id),
                ..Self::default()
            }
        }
    }

    pub fn deleted(id: i64) -> Self {
        Self {
            delete: Some(id),
            ..Self::default()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn datetimes_are_written_as_pydantic_writes_them() {
        let at = Utc.with_ymd_and_hms(2026, 9, 30, 9, 44, 8).unwrap();
        assert_eq!(pydantic_datetime(&at), "2026-09-30T09:44:08Z");
        let micro = at + chrono::Duration::microseconds(500);
        assert_eq!(pydantic_datetime(&micro), "2026-09-30T09:44:08.000500Z");
        let precise = at + chrono::Duration::microseconds(960_420);
        assert_eq!(pydantic_datetime(&precise), "2026-09-30T09:44:08.960420Z");
    }
}

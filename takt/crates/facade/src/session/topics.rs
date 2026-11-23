//! What a caller session can follow (docs/session-protocol.md §4): each topic's name, who may
//! follow it, the channel-layer groups it listens on (the ones the GraphQL subscriptions listen
//! on, fed by `signals`), its snapshot, and how it is brought up to date from a cursor.
//!
//! Authorization mirrors the Python server's `scoped_get`: an object is visible when its
//! organization (a task's agent's, a state's agent's, an agent's) is the caller's.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::channel_events::{TaskChangePayload, TaskEventPayload};
use crate::channels;
use crate::context::Context;
use crate::probes;

/// Who a session is: its caller, organization, the principal its calls are made as, and its id.
#[derive(Debug, Clone)]
pub struct Who {
    pub caller: i64,
    pub organization: i64,
    pub principal: crate::caller_context::CallerContext,
    pub session: String,
}

/// A followable thing.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Topic {
    /// The caller's own root tasks, as they are made and report (`mytasks`).
    TasksMine,
    /// The organization's root tasks (`tasks`).
    TasksOrg,
    /// One task's events, gap-free (`taskEvents`).
    Task(i64),
    /// The tasks under a task (`childTasks`).
    Children(i64),
    /// The tasks an agent runs (`agentTasks`).
    AgentTasks(i64),
    /// The organization's agents (`agents`).
    AgentsOrg,
    /// One state of an agent (`watchState`).
    State { agent: i64, interface: String },
    /// All of an agent's states (`watchAgent`).
    States(i64),
    /// One probe's events (`probeEvents`).
    Probe(String),
    /// The task an agent's action last ran as, kept current (`latestTask`).
    Latest { agent: i64, interface: String },
}

impl Topic {
    pub fn parse(name: &str) -> Option<Topic> {
        let id = |s: &str| s.parse::<i64>().ok();
        match name.split_once(':') {
            None => None,
            Some(("tasks", "mine")) => Some(Topic::TasksMine),
            Some(("tasks", "org")) => Some(Topic::TasksOrg),
            Some(("agents", "org")) => Some(Topic::AgentsOrg),
            Some(("task", rest)) => id(rest).map(Topic::Task),
            Some(("children", rest)) => id(rest).map(Topic::Children),
            Some(("states", rest)) => id(rest).map(Topic::States),
            Some(("probe", rest)) if !rest.is_empty() => Some(Topic::Probe(rest.to_owned())),
            Some(("agent", rest)) => rest
                .strip_suffix("/tasks")
                .and_then(id)
                .map(Topic::AgentTasks),
            Some(("state", rest)) => {
                let (agent, interface) = rest.split_once('/')?;
                (!interface.is_empty()).then_some(())?;
                Some(Topic::State {
                    agent: id(agent)?,
                    interface: interface.to_owned(),
                })
            }
            Some(("latest", rest)) => {
                let (agent, interface) = rest.split_once('/')?;
                (!interface.is_empty()).then_some(())?;
                Some(Topic::Latest {
                    agent: id(agent)?,
                    interface: interface.to_owned(),
                })
            }
            _ => None,
        }
    }

    pub fn name(&self) -> String {
        match self {
            Topic::TasksMine => "tasks:mine".into(),
            Topic::TasksOrg => "tasks:org".into(),
            Topic::AgentsOrg => "agents:org".into(),
            Topic::Task(id) => format!("task:{id}"),
            Topic::Children(id) => format!("children:{id}"),
            Topic::AgentTasks(id) => format!("agent:{id}/tasks"),
            Topic::State { agent, interface } => format!("state:{agent}/{interface}"),
            Topic::States(id) => format!("states:{id}"),
            Topic::Probe(id) => format!("probe:{id}"),
            Topic::Latest { agent, interface } => format!("latest:{agent}/{interface}"),
        }
    }

    /// The channel its messages come on (`kante::channel::payload_of`).
    pub fn channels(&self) -> &'static [&'static str] {
        match self {
            Topic::TasksMine | Topic::TasksOrg | Topic::Task(_) | Topic::Latest { .. } => {
                &[channels::TASK_EVENT]
            }
            Topic::Children(_) => &[channels::CHILD_TASK],
            Topic::AgentTasks(_) => &[channels::AGENT_TASK],
            Topic::AgentsOrg => &[channels::AGENT_UPDATED],
            Topic::State { .. } => &[channels::PATCH, channels::STATE_UPDATE],
            Topic::States(_) => &[channels::PATCH],
            Topic::Probe(_) => &[channels::PROBE_EVENT],
        }
    }
}

/// A topic a session may follow, with what authorizing it found out.
#[derive(Debug, Clone)]
pub struct Bound {
    pub topic: Topic,
    /// The state's id (`Topic::State`).
    pub state: Option<i64>,
    /// The implementation the agent offers under the interface (`Topic::Latest`); none: the
    /// interface is no action of it, and nothing follows.
    pub implementation: Option<i64>,
    pub groups: Vec<String>,
}

/// A refusal to follow a topic, as a sentence.
#[derive(Debug, thiserror::Error)]
pub enum WatchError {
    #[error("{0}")]
    Refused(String),
    #[error("{0}")]
    Database(#[from] sqlx::Error),
    #[error("{0}")]
    Redis(#[from] redis::RedisError),
}

fn not_visible(what: &str, id: impl std::fmt::Display) -> WatchError {
    // As `scoped_get` says it: a wrong-tenant id reads as a missing one.
    WatchError::Refused(format!("No {what} {id} in this organization."))
}

/// Whether `who` may follow `topic`, and its groups.
pub async fn bind(ctx: &Context, who: &Who, topic: Topic) -> Result<Bound, WatchError> {
    let org = who.organization;
    let mut implementation = None;
    let (state, groups) = match &topic {
        Topic::TasksMine => (None, vec![format!("root_tasks_caller_{}", who.caller)]),
        Topic::TasksOrg => (None, vec![format!("root_tasks_org_{org}")]),
        Topic::AgentsOrg => (None, vec![format!("agents_for_{org}")]),
        Topic::Task(task) | Topic::Children(task) => {
            let visible: Option<bool> = sqlx::query_scalar(
                "SELECT a.organization_id = $2 FROM facade_task t
                   JOIN facade_agent a ON a.id = t.agent_id WHERE t.id = $1",
            )
            .bind(task)
            .bind(org)
            .fetch_optional(&ctx.db)
            .await?;
            if visible != Some(true) {
                return Err(not_visible("Task", task));
            }
            let group = match topic {
                Topic::Task(_) => crate::signals::task_events(*task),
                _ => format!("child_tasks_{task}"),
            };
            (None, vec![group])
        }
        Topic::AgentTasks(agent) | Topic::States(agent) => {
            agent_visible(ctx, *agent, org).await?;
            let group = match topic {
                Topic::AgentTasks(_) => format!("agent_tasks_{agent}"),
                _ => format!("patches_agent_{agent}"),
            };
            (None, vec![group])
        }
        Topic::State { agent, interface } => {
            agent_visible(ctx, *agent, org).await?;
            let state: Option<i64> = sqlx::query_scalar(
                "SELECT id FROM facade_state WHERE agent_id = $1 AND interface = $2",
            )
            .bind(agent)
            .bind(interface)
            .fetch_optional(&ctx.db)
            .await?;
            let Some(state) = state else {
                return Err(WatchError::Refused(format!(
                    "Agent {agent} has no state {interface}."
                )));
            };
            (
                Some(state),
                vec![format!("patches_state_{state}"), format!("state_{state}")],
            )
        }
        Topic::Latest { agent, interface } => {
            agent_visible(ctx, *agent, org).await?;
            implementation = sqlx::query_scalar(
                "SELECT id FROM facade_implementation WHERE agent_id = $1 AND interface = $2",
            )
            .bind(agent)
            .bind(interface)
            .fetch_optional(&ctx.db)
            .await?;
            let groups = implementation
                .map(|id| vec![crate::signals::implementation_task_events(id)])
                .unwrap_or_default();
            (None, groups)
        }
        Topic::Probe(probe) => {
            let Some(found) = probes::store::get(ctx, probe).await? else {
                return Err(WatchError::Refused(format!(
                    "Unknown or expired probe {probe}"
                )));
            };
            if found.get("caller") != Some(&who.caller.to_string()) {
                return Err(WatchError::Refused(
                    "Not authorized to watch this probe (not its caller).".into(),
                ));
            }
            (None, vec![probes::persist::probe_events_topic(probe)])
        }
    };
    Ok(Bound {
        topic,
        state,
        implementation,
        groups,
    })
}

async fn agent_visible(ctx: &Context, agent: i64, org: i64) -> Result<(), WatchError> {
    let visible: Option<bool> =
        sqlx::query_scalar("SELECT organization_id = $2 FROM facade_agent WHERE id = $1")
            .bind(agent)
            .bind(org)
            .fetch_optional(&ctx.db)
            .await?;
    if visible != Some(true) {
        return Err(not_visible("Agent", agent));
    }
    Ok(())
}

// ---------------------------------------------------------------------------------------------
// cursors

/// How far a session has a topic: where a replay starts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Cursor {
    /// A list: no replay, a resume resends its rows.
    List,
    /// A task's events: every non-PROGRESS event up to `confirmed` was sent, and nothing past
    /// `max` was (a PROGRESS between them may have been thinned, which is by design).
    Seq { confirmed: i64, max: i64 },
    /// A state's (or an agent's states') patches in one agent session, up to `rev`.
    State { session: Option<i64>, rev: i64 },
}

// ---------------------------------------------------------------------------------------------
// rows

#[derive(sqlx::FromRow)]
struct TaskRow {
    id: i64,
    reference: String,
    is_done: bool,
    latest_event_kind: String,
    latest_instruct_kind: String,
    action_id: i64,
    implementation_id: Option<i64>,
    agent_id: i64,
    root_id: Option<i64>,
    parent_id: Option<i64>,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
    finished_at: Option<DateTime<Utc>>,
    revision: i64,
}

const TASK_COLUMNS: &str = "t.id, t.reference, t.is_done, t.latest_event_kind, t.latest_instruct_kind,
    t.action_id, t.implementation_id, t.agent_id, t.root_id, t.parent_id, t.created_at,
    t.updated_at, t.finished_at, t.revision::bigint AS revision";

impl TaskRow {
    /// As the feeds carry a task (`TaskChangePayload`).
    fn change(self) -> Value {
        serde_json::to_value(TaskChangePayload {
            id: self.id.to_string(),
            reference: Some(self.reference),
            is_done: self.is_done,
            latest_event_kind: self.latest_event_kind,
            latest_instruct_kind: self.latest_instruct_kind,
            action: self.action_id.to_string(),
            implementation: self.implementation_id.map(|id| id.to_string()),
            agent: Some(self.agent_id.to_string()),
            root: self.root_id.map(|id| id.to_string()),
            parent: self.parent_id.map(|id| id.to_string()),
            created_at: self.created_at,
            updated_at: self.updated_at,
            finished_at: self.finished_at,
            revision: self.revision,
        })
        .unwrap_or(Value::Null)
    }
}

/// One task as the feeds carry it (`TaskChange`), or null for none.
pub async fn task_change(ctx: &Context, task: i64) -> Result<Value, sqlx::Error> {
    let row: Option<TaskRow> = sqlx::query_as(&format!(
        "SELECT {TASK_COLUMNS} FROM facade_task t WHERE t.id = $1"
    ))
    .bind(task)
    .fetch_optional(&ctx.db)
    .await?;
    Ok(row.map(TaskRow::change).unwrap_or(Value::Null))
}

async fn task_rows(
    ctx: &Context,
    filter: &str,
    order: &str,
    key: i64,
    limit: i64,
) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<TaskRow> = sqlx::query_as(&format!(
        "SELECT {TASK_COLUMNS} FROM facade_task t {filter} ORDER BY {order} LIMIT $2"
    ))
    .bind(key)
    .bind(limit)
    .fetch_all(&ctx.db)
    .await?;
    Ok(rows.into_iter().map(TaskRow::change).collect())
}

#[derive(sqlx::FromRow)]
struct EventRow {
    id: i64,
    task_id: i64,
    kind: String,
    message: Option<String>,
    progress: Option<i32>,
    returns: Option<Value>,
    level: Option<String>,
    value: Option<Value>,
    created_at: DateTime<Utc>,
    task_seq: Option<i64>,
}

const EVENT_COLUMNS: &str = "id, task_id, kind, message, progress, returns, level, value,
    created_at, task_seq";

impl EventRow {
    /// As the feeds carry an event (`TaskEventPayload`).
    fn payload(self) -> Value {
        serde_json::to_value(TaskEventPayload {
            id: self.id.to_string(),
            task: self.task_id.to_string(),
            kind: self.kind,
            message: self.message,
            progress: self.progress.map(i64::from),
            returns: self.returns,
            level: self.level.filter(|level| !level.is_empty()),
            value: self.value,
            created_at: self.created_at,
            task_seq: self.task_seq,
        })
        .unwrap_or(Value::Null)
    }
}

/// A task event item's number and kind.
pub fn event_seq_and_kind(item: &Value) -> (Option<i64>, &str) {
    (
        item.get("task_seq").and_then(Value::as_i64),
        item.get("kind").and_then(Value::as_str).unwrap_or(""),
    )
}

/// Keep the last of each run of PROGRESS events (progress is a level: only the newest of a run
/// says anything the next one does not).
pub fn coalesce_progress(events: Vec<Value>) -> Vec<Value> {
    let mut out: Vec<Value> = Vec::with_capacity(events.len());
    for event in events {
        let progress = event_seq_and_kind(&event).1 == "PROGRESS";
        if progress
            && out
                .last()
                .is_some_and(|last| event_seq_and_kind(last).1 == "PROGRESS")
        {
            out.pop();
        }
        out.push(event);
    }
    out
}

/// A task's events numbered after `after`, oldest first, at most `limit` (one more is read to
/// tell whether there were more). `None` when there were more than `limit`.
pub async fn task_events_after(
    ctx: &Context,
    task: i64,
    after: i64,
    limit: i64,
) -> Result<Option<Vec<Value>>, sqlx::Error> {
    let rows: Vec<EventRow> = sqlx::query_as(&format!(
        "SELECT {EVENT_COLUMNS} FROM facade_taskevent
          WHERE task_id = $1 AND task_seq > $2 ORDER BY task_seq LIMIT $3"
    ))
    .bind(task)
    .bind(after)
    .bind(limit + 1)
    .fetch_all(&ctx.db)
    .await?;
    if rows.len() as i64 > limit {
        return Ok(None);
    }
    Ok(Some(rows.into_iter().map(EventRow::payload).collect()))
}

/// What a session missed of a task between `confirmed` and `upto`: every event that is not a
/// PROGRESS, and the newest PROGRESS past `max` (the gap fill, §5.3).
pub async fn task_gap(
    ctx: &Context,
    task: i64,
    confirmed: i64,
    max: i64,
    upto: i64,
) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<EventRow> = sqlx::query_as(&format!(
        "(SELECT {EVENT_COLUMNS} FROM facade_taskevent
           WHERE task_id = $1 AND task_seq > $2 AND task_seq <= $4 AND kind <> 'PROGRESS')
         UNION ALL
         (SELECT {EVENT_COLUMNS} FROM facade_taskevent
           WHERE task_id = $1 AND task_seq > $3 AND task_seq <= $4 AND kind = 'PROGRESS'
           ORDER BY task_seq DESC LIMIT 1)
         ORDER BY task_seq"
    ))
    .bind(task)
    .bind(confirmed)
    .bind(max)
    .bind(upto)
    .fetch_all(&ctx.db)
    .await?;
    Ok(rows.into_iter().map(EventRow::payload).collect())
}

/// The number of a task's newest event.
pub async fn task_event_seq(ctx: &Context, task: i64) -> Result<Option<i64>, sqlx::Error> {
    sqlx::query_scalar("SELECT event_seq::bigint FROM facade_task WHERE id = $1")
        .bind(task)
        .fetch_optional(&ctx.db)
        .await
}

/// A task's snapshot: its record, its newest events (oldest first) and its counter.
pub async fn task_snapshot(
    ctx: &Context,
    task: i64,
    limit: i64,
) -> Result<(Value, Cursor), sqlx::Error> {
    let record = task_change(ctx, task).await?;
    let seq = task_event_seq(ctx, task).await?.unwrap_or(0);
    let mut rows: Vec<EventRow> = sqlx::query_as(&format!(
        "SELECT {EVENT_COLUMNS} FROM facade_taskevent
          WHERE task_id = $1 AND task_seq <= $2 ORDER BY task_seq DESC LIMIT $3"
    ))
    .bind(task)
    .bind(seq)
    .bind(limit)
    .fetch_all(&ctx.db)
    .await?;
    rows.reverse();
    let events: Vec<Value> = rows.into_iter().map(EventRow::payload).collect();
    Ok((
        json!({"task": record, "events": events, "task_seq": seq}),
        Cursor::Seq {
            confirmed: seq,
            max: seq,
        },
    ))
}

// ---------------------------------------------------------------------------------------------
// lists

/// A list topic's rows as they are now (its snapshot).
pub async fn list_snapshot(ctx: &Context, who: &Who, bound: &Bound) -> Result<Value, WatchError> {
    let limit = ctx.settings.session.list_snapshot;
    let rows = match &bound.topic {
        Topic::TasksMine => {
            task_rows(
                ctx,
                "WHERE t.caller_id = $1 AND t.parent_id IS NULL",
                "t.updated_at DESC",
                who.caller,
                limit,
            )
            .await?
        }
        Topic::TasksOrg => {
            task_rows(
                ctx,
                "JOIN facade_caller c ON c.id = t.caller_id
                 WHERE c.organization_id = $1 AND t.parent_id IS NULL",
                "t.updated_at DESC",
                who.organization,
                limit,
            )
            .await?
        }
        Topic::Children(task) => {
            task_rows(
                ctx,
                "WHERE t.root_id = $1 OR t.parent_id = $1",
                "t.id",
                *task,
                limit,
            )
            .await?
        }
        Topic::AgentTasks(agent) => {
            task_rows(
                ctx,
                "WHERE t.agent_id = $1",
                "t.created_at DESC",
                *agent,
                limit,
            )
            .await?
        }
        Topic::AgentsOrg => agent_rows(ctx, who.organization, limit).await?,
        Topic::Latest { .. } => return Ok(latest_snapshot(ctx, bound.implementation).await?),
        Topic::Probe(probe) => {
            let state = probes::store::get(ctx, probe).await?;
            return Ok(json!({ "probe": state }));
        }
        _ => vec![],
    };
    Ok(json!({ "rows": rows }))
}

#[derive(sqlx::FromRow)]
struct AgentRow {
    id: i64,
    name: String,
    display_name: Option<String>,
    kind: String,
    connected: bool,
    blocked: bool,
    last_seen: Option<DateTime<Utc>>,
    client_id: i64,
    user_id: i64,
    organization_id: i64,
    app_id: Option<i64>,
    release_id: Option<i64>,
}

const AGENT_COLUMNS: &str = "id, name, display_name, kind, connected, blocked, last_seen,
    client_id, user_id, organization_id, app_id, release_id";

impl AgentRow {
    /// As the `agents` feed carries an agent (`AgentChange`).
    fn change(self) -> Value {
        let name = self
            .display_name
            .filter(|name| !name.is_empty())
            .unwrap_or(self.name);
        json!({
            "id": self.id.to_string(),
            "name": name,
            "kind": self.kind,
            "connected": self.connected,
            "blocked": self.blocked,
            "last_seen": self.last_seen.map(|at| crate::channel_events::pydantic_datetime(&at)),
            "client": self.client_id.to_string(),
            "user": self.user_id.to_string(),
            "organization": self.organization_id.to_string(),
            "app": self.app_id.map(|id| id.to_string()),
            "release": self.release_id.map(|id| id.to_string()),
        })
    }
}

async fn agent_rows(ctx: &Context, org: i64, limit: i64) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<AgentRow> = sqlx::query_as(&format!(
        "SELECT {AGENT_COLUMNS} FROM facade_agent WHERE organization_id = $1
          ORDER BY last_seen DESC NULLS LAST LIMIT $2"
    ))
    .bind(org)
    .bind(limit)
    .fetch_all(&ctx.db)
    .await?;
    Ok(rows.into_iter().map(AgentRow::change).collect())
}

/// One agent as the `agents` feed carries it, or null when it is gone.
pub async fn agent_change(ctx: &Context, agent: i64) -> Result<Value, sqlx::Error> {
    let row: Option<AgentRow> = sqlx::query_as(&format!(
        "SELECT {AGENT_COLUMNS} FROM facade_agent WHERE id = $1"
    ))
    .bind(agent)
    .fetch_optional(&ctx.db)
    .await?;
    Ok(row.map(AgentRow::change).unwrap_or(Value::Null))
}

/// The kinds a task's record is made of: how far it is, what it returned, how it ended.
const REPORTED_KINDS: &[&str] = &[
    "PROGRESS",
    "YIELD",
    "COMPLETED",
    "CANCELLED",
    "INTERRUPTED",
    "FAILED",
    "CRITICAL",
    "LOST",
    "DENIED",
];

/// The task an implementation last ran as, and the newest event of each kind its record is made
/// of, oldest first (as `latestTask`'s first message: `facade/subscriptions/task.py`).
pub async fn latest_snapshot(
    ctx: &Context,
    implementation: Option<i64>,
) -> Result<Value, sqlx::Error> {
    let Some(implementation) = implementation else {
        return Ok(json!({"implementation": null, "latest": null, "events": []}));
    };
    let latest: Option<TaskRow> = sqlx::query_as(&format!(
        "SELECT {TASK_COLUMNS} FROM facade_task t WHERE t.implementation_id = $1
          ORDER BY t.created_at DESC, t.id DESC LIMIT 1"
    ))
    .bind(implementation)
    .fetch_optional(&ctx.db)
    .await?;
    let Some(latest) = latest else {
        return Ok(json!({"implementation": implementation.to_string(), "latest": null, "events": []}));
    };
    let task = latest.id;
    let mut rows: Vec<EventRow> = sqlx::query_as(&format!(
        "SELECT DISTINCT ON (kind) {EVENT_COLUMNS} FROM facade_taskevent
          WHERE task_id = $1 AND kind = ANY($2) ORDER BY kind, id DESC"
    ))
    .bind(task)
    .bind(REPORTED_KINDS)
    .fetch_all(&ctx.db)
    .await?;
    rows.sort_by_key(|row| row.id);
    let events: Vec<Value> = rows.into_iter().map(EventRow::payload).collect();
    Ok(json!({
        "implementation": implementation.to_string(),
        "latest": latest.change(),
        "events": events,
    }))
}

// ---------------------------------------------------------------------------------------------
// states

#[derive(sqlx::FromRow)]
struct PatchRow {
    id: i64,
    state_id: i64,
    agent_id: Option<i64>,
    interface: String,
    ops: Value,
    global_rev: i32,
    session_id: Option<i64>,
    timestamp: Option<DateTime<Utc>>,
    task_id: Option<i64>,
    lifecycle: Option<String>,
    qualifiers: Option<Value>,
}

impl PatchRow {
    /// As the patch feeds carry a patch (`PatchEvent`).
    fn payload(self) -> Value {
        json!({
            "create": self.id,
            "state": self.state_id,
            "agent": self.agent_id,
            "interface": self.interface,
            "ops": self.ops,
            "global_rev": self.global_rev,
            "session": self.session_id,
            "timestamp": self.timestamp.map(|at| crate::channel_events::pydantic_datetime(&at)),
            "task": self.task_id,
            "lifecycle": self.lifecycle,
            "qualifiers": self.qualifiers.unwrap_or_else(|| json!([])),
        })
    }
}

const PATCH_COLUMNS: &str = "id, state_id, agent_id, interface, ops, global_rev, session_id,
    timestamp, task_id, lifecycle, qualifiers";

/// The agent's current process (its newest state session), if it ever reported state.
pub async fn current_state_session(ctx: &Context, agent: i64) -> Result<Option<i64>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT id FROM facade_session WHERE agent_id = $1 ORDER BY created_at DESC, id DESC LIMIT 1",
    )
    .bind(agent)
    .fetch_optional(&ctx.db)
    .await
}

/// One state in one agent session: its newest snapshot and the patches after it.
async fn state_in_session(
    ctx: &Context,
    state: i64,
    session: i64,
) -> Result<(Value, i64), sqlx::Error> {
    let base: Option<(Value, i32)> = sqlx::query_as(
        "SELECT value, global_rev FROM facade_snapshot
          WHERE state_id = $1 AND session_id = $2 ORDER BY global_rev DESC, id DESC LIMIT 1",
    )
    .bind(state)
    .bind(session)
    .fetch_optional(&ctx.db)
    .await?;
    let (base, base_rev) = base.unwrap_or((Value::Null, 0));
    let patches: Vec<PatchRow> = sqlx::query_as(&format!(
        "SELECT {PATCH_COLUMNS} FROM facade_patch
          WHERE state_id = $1 AND session_id = $2 AND global_rev > $3 ORDER BY global_rev, id"
    ))
    .bind(state)
    .bind(session)
    .bind(base_rev)
    .fetch_all(&ctx.db)
    .await?;
    let rev = patches
        .last()
        .map_or(i64::from(base_rev), |patch| i64::from(patch.global_rev));
    let patches: Vec<Value> = patches.into_iter().map(PatchRow::payload).collect();
    Ok((
        json!({"base": base, "base_rev": base_rev, "patches": patches}),
        rev,
    ))
}

/// A state topic's snapshot (`state:` one state, `states:` all of an agent's): per state its
/// newest snapshot and the patches after it, for the window to apply, and the cursor.
pub async fn state_snapshot(ctx: &Context, bound: &Bound) -> Result<(Value, Cursor), sqlx::Error> {
    let agent = match &bound.topic {
        Topic::State { agent, .. } | Topic::States(agent) => *agent,
        _ => return Ok((Value::Null, Cursor::List)),
    };
    let Some(session) = current_state_session(ctx, agent).await? else {
        return Ok((
            json!({"agent": agent, "session": null, "global_rev": 0, "states": {}}),
            Cursor::State {
                session: None,
                rev: 0,
            },
        ));
    };
    let states: Vec<(i64, String)> = match bound.state {
        Some(state) => sqlx::query_as("SELECT id, interface FROM facade_state WHERE id = $1")
            .bind(state)
            .fetch_all(&ctx.db)
            .await?,
        None => sqlx::query_as("SELECT id, interface FROM facade_state WHERE agent_id = $1")
            .bind(agent)
            .fetch_all(&ctx.db)
            .await?,
    };
    let mut out = serde_json::Map::new();
    let mut rev = 0;
    for (state, interface) in states {
        let (value, state_rev) = state_in_session(ctx, state, session).await?;
        rev = rev.max(state_rev);
        out.insert(interface, json!({"state": state, "value": value}));
    }
    Ok((
        json!({"agent": agent, "session": session, "global_rev": rev, "states": out}),
        Cursor::State {
            session: Some(session),
            rev,
        },
    ))
}

/// The patches of a state topic after `rev` in `session`, oldest first, at most `limit` (`None`
/// when there were more).
pub async fn patches_after(
    ctx: &Context,
    bound: &Bound,
    session: i64,
    rev: i64,
    limit: i64,
) -> Result<Option<Vec<Value>>, sqlx::Error> {
    let (filter, key) = match (&bound.topic, bound.state) {
        (Topic::State { .. }, Some(state)) => ("state_id = $1", state),
        (Topic::States(agent), _) => ("agent_id = $1", *agent),
        _ => return Ok(Some(vec![])),
    };
    let rows: Vec<PatchRow> = sqlx::query_as(&format!(
        "SELECT {PATCH_COLUMNS} FROM facade_patch
          WHERE {filter} AND session_id = $2 AND global_rev > $3 ORDER BY global_rev, id LIMIT $4"
    ))
    .bind(key)
    .bind(session)
    .bind(rev)
    .bind(limit + 1)
    .fetch_all(&ctx.db)
    .await?;
    if rows.len() as i64 > limit {
        return Ok(None);
    }
    Ok(Some(rows.into_iter().map(PatchRow::payload).collect()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_parse_back() {
        for name in [
            "tasks:mine",
            "tasks:org",
            "agents:org",
            "task:12",
            "children:12",
            "agent:3/tasks",
            "state:3/camera/position",
            "states:3",
            "probe:p-abc",
            "latest:3/segment_cells",
        ] {
            assert_eq!(Topic::parse(name).map(|t| t.name()).as_deref(), Some(name));
        }
        for bad in ["", "task:", "task:x", "agent:3", "state:3/", "tasks:all", "nope"] {
            assert_eq!(Topic::parse(bad), None, "{bad}");
        }
    }

    #[test]
    fn a_run_of_progress_keeps_its_last() {
        let e = |seq: i64, kind: &str| json!({"task_seq": seq, "kind": kind});
        let kept = coalesce_progress(vec![
            e(1, "STARTED"),
            e(2, "PROGRESS"),
            e(3, "PROGRESS"),
            e(4, "LOG"),
            e(5, "PROGRESS"),
            e(6, "PROGRESS"),
        ]);
        let seqs: Vec<i64> = kept.iter().map(|e| e["task_seq"].as_i64().unwrap()).collect();
        assert_eq!(seqs, vec![1, 3, 4, 6]);
    }
}

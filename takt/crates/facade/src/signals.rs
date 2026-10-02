//! The change fan-out (`facade/signals.py`, and `facade/transport.py`'s `publish_task_event`).
//!
//! Django fires these from `post_save`/`post_delete` and broadcasts on commit. takt writes
//! with SQL, so it calls them itself, after the writing transaction committed. Each loads what
//! it needs, builds the payload the Python side builds (`facade::channel_events`) and sends it
//! on the same channel to the same groups, where the GraphQL subscriptions and the agent
//! sockets pick it up. A failed broadcast is logged, never returned: the write it reports on
//! already happened.
//!
//! Not ported here: the webhook caller mirror of `publish_task_event`
//! (`_deliver_caller_event_to_webhook`, an HTTP POST to a HookAgent caller), which comes with
//! the hook agents.

use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::Value;

use crate::channel_events::{
    ChildTaskEvent, CrudEvent, PatchEvent, StateUpdateEvent, TaskChangePayload,
    TaskEventCreatedEvent, TaskEventPayload,
};
use crate::channels;
use crate::context::Context;

async fn publish(ctx: &Context, channel: &str, payload: &impl Serialize, groups: Vec<String>) {
    if let Err(e) = kante::channel::broadcast(&ctx.channel_layer, channel, payload, &groups).await {
        tracing::error!(channel, ?groups, "broadcast failed: {e}");
    }
}

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
    caller_id: Option<i64>,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
    finished_at: Option<DateTime<Utc>>,
    revision: i64,
}

impl TaskRow {
    /// `TaskChangePayload.from_task`.
    fn payload(&self) -> TaskChangePayload {
        TaskChangePayload {
            id: self.id.to_string(),
            reference: Some(self.reference.clone()),
            is_done: self.is_done,
            latest_event_kind: self.latest_event_kind.clone(),
            latest_instruct_kind: self.latest_instruct_kind.clone(),
            action: self.action_id.to_string(),
            implementation: self.implementation_id.map(|id| id.to_string()),
            agent: Some(self.agent_id.to_string()),
            root: self.root_id.map(|id| id.to_string()),
            parent: self.parent_id.map(|id| id.to_string()),
            created_at: self.created_at,
            updated_at: self.updated_at,
            finished_at: self.finished_at,
            revision: self.revision,
        }
    }
}

async fn caller_organization(ctx: &Context, caller: i64) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar("SELECT organization_id FROM facade_caller WHERE id = $1")
        .bind(caller)
        .fetch_one(&ctx.db)
        .await
}

/// A task was saved (`task_post_save`): a new root task reaches its caller's and its
/// organization's feeds; every task reaches its agent's feed; a child reaches its parent's and
/// its root's.
pub async fn task_saved(ctx: &Context, task_id: i64, created: bool) {
    if let Err(e) = task_saved_inner(ctx, task_id, created).await {
        tracing::error!(task_id, "task fan-out failed: {e}");
    }
}

async fn task_saved_inner(ctx: &Context, task_id: i64, created: bool) -> Result<(), sqlx::Error> {
    let task: TaskRow = sqlx::query_as(
        "SELECT id, reference, is_done, latest_event_kind, latest_instruct_kind,
                action_id, implementation_id, agent_id, root_id, parent_id, caller_id,
                created_at, updated_at, finished_at, revision
           FROM facade_task WHERE id = $1",
    )
    .bind(task_id)
    .fetch_one(&ctx.db)
    .await?;
    let payload = task.payload();

    if created && task.root_id.is_none() {
        if let Some(caller) = task.caller_id {
            let organization = caller_organization(ctx, caller).await?;
            publish(
                ctx,
                channels::TASK_EVENT,
                &TaskEventCreatedEvent {
                    event: None,
                    create: Some(payload.clone()),
                },
                vec![
                    format!("root_tasks_caller_{caller}"),
                    format!("root_tasks_org_{organization}"),
                ],
            )
            .await;
        }
    }

    let event = if created {
        ChildTaskEvent {
            create: Some(payload),
            update: None,
        }
    } else {
        ChildTaskEvent {
            create: None,
            update: Some(payload),
        }
    };
    publish(
        ctx,
        channels::AGENT_TASK,
        &event,
        vec![format!("agent_tasks_{}", task.agent_id)],
    )
    .await;

    if let Some(parent) = task.parent_id {
        let mut groups = vec![format!("child_tasks_{parent}")];
        if let Some(root) = task.root_id.filter(|root| *root != parent) {
            groups.push(format!("child_tasks_{root}"));
        }
        publish(ctx, channels::CHILD_TASK, &event, groups).await;
    }
    Ok(())
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
    caller_id: Option<i64>,
    root_id: Option<i64>,
}

/// The payload of a persisted task event (`TaskEventPayload.from_event`).
pub async fn task_event_payload(
    ctx: &Context,
    event_id: i64,
) -> Result<(TaskEventPayload, Option<i64>, Option<i64>), sqlx::Error> {
    let event: EventRow = sqlx::query_as(
        "SELECT e.id, e.task_id, e.kind, e.message, e.progress, e.returns, e.level, e.value, e.created_at,
                t.caller_id, t.root_id
           FROM facade_taskevent e JOIN facade_task t ON t.id = e.task_id
          WHERE e.id = $1",
    )
    .bind(event_id)
    .fetch_one(&ctx.db)
    .await?;
    Ok((
        TaskEventPayload {
            id: event.id.to_string(),
            task: event.task_id.to_string(),
            kind: event.kind,
            message: event.message,
            progress: event.progress.map(i64::from),
            returns: event.returns,
            level: event.level.filter(|level| !level.is_empty()),
            value: event.value,
            created_at: event.created_at,
        },
        event.caller_id,
        event.root_id,
    ))
}

/// A task event was recorded (`task_event_post_save` → `publish_task_event`): it reaches its
/// task's caller (`task_caller_{caller}`: the agent sockets and the GraphQL feeds), and, for a
/// root task, the caller's and the organization's root-task feeds.
pub async fn task_event_created(ctx: &Context, event_id: i64) {
    if let Err(e) = task_event_created_inner(ctx, event_id).await {
        tracing::error!(event_id, "task event fan-out failed: {e}");
    }
}

async fn task_event_created_inner(ctx: &Context, event_id: i64) -> Result<(), sqlx::Error> {
    let (payload, caller, root) = task_event_payload(ctx, event_id).await?;
    let Some(caller) = caller else {
        return Ok(());
    };
    tracing::debug!(event_id, caller, "task event fan-out");
    let mut groups = vec![format!("task_caller_{caller}")];
    if root.is_none() {
        let organization = caller_organization(ctx, caller).await?;
        groups.push(format!("root_tasks_caller_{caller}"));
        groups.push(format!("root_tasks_org_{organization}"));
    }
    let mirrored = crate::caller_events::EventLike::from(&payload);
    publish(
        ctx,
        channels::TASK_EVENT,
        &TaskEventCreatedEvent {
            event: Some(payload),
            create: None,
        },
        groups,
    )
    .await;
    crate::transport::deliver_caller_event_to_webhook(ctx, caller, &mirrored).await;
    Ok(())
}

/// An agent was saved (`agent_post_save` → `broadcast_agent_update`): its organization's
/// agent feed re-fetches it.
pub async fn agent_saved(ctx: &Context, agent_id: i64, created: bool) {
    match sqlx::query_scalar::<_, i64>("SELECT organization_id FROM facade_agent WHERE id = $1")
        .bind(agent_id)
        .fetch_one(&ctx.db)
        .await
    {
        Ok(organization) => {
            publish(
                ctx,
                channels::AGENT_UPDATED,
                &CrudEvent::saved(agent_id, created),
                vec![format!("agents_for_{organization}")],
            )
            .await
        }
        Err(e) => tracing::error!(agent_id, "agent fan-out failed: {e}"),
    }
}

/// An agent was deleted (`agent_post_delete`). The row is gone, so its organization is passed in.
pub async fn agent_deleted(ctx: &Context, agent_id: i64, organization: i64) {
    publish(
        ctx,
        channels::AGENT_UPDATED,
        &CrudEvent::deleted(agent_id),
        vec![format!("agents_for_{organization}")],
    )
    .await;
}

#[derive(sqlx::FromRow)]
struct PatchRow {
    id: i64,
    state_id: i64,
    agent_id: Option<i64>,
    interface: String,
    op: String,
    path: String,
    value: Value,
    global_rev: i32,
    session_id: Option<i64>,
    timestamp: DateTime<Utc>,
}

/// A patch was recorded (`patch_post_save`, created only): its state's and its agent's patch feeds.
pub async fn patch_created(ctx: &Context, patch_id: i64) {
    let patch: PatchRow = match sqlx::query_as(
        "SELECT id, state_id, agent_id, interface, op, path, value, global_rev, session_id, timestamp
           FROM facade_patch WHERE id = $1",
    )
    .bind(patch_id)
    .fetch_one(&ctx.db)
    .await
    {
        Ok(patch) => patch,
        Err(e) => {
            tracing::error!(patch_id, "patch fan-out failed: {e}");
            return;
        }
    };
    let mut groups = vec![format!("patches_state_{}", patch.state_id)];
    if let Some(agent) = patch.agent_id {
        groups.push(format!("patches_agent_{agent}"));
    }
    publish(
        ctx,
        channels::PATCH,
        &PatchEvent {
            create: patch.id,
            state: patch.state_id,
            agent: patch.agent_id,
            interface: patch.interface,
            op: patch.op,
            path: patch.path,
            value: patch.value,
            global_rev: i64::from(patch.global_rev),
            session: patch.session_id,
            timestamp: Some(patch.timestamp),
        },
        groups,
    )
    .await;
}

/// A state was saved (`state_post_save`): its detail feed re-fetches it.
pub async fn state_saved(ctx: &Context, state_id: i64) {
    publish(
        ctx,
        channels::STATE_UPDATE,
        &StateUpdateEvent { state: state_id },
        vec![format!("state_{state_id}")],
    )
    .await;
}

fn implementation_groups(id: i64, agent_id: i64) -> Vec<String> {
    vec![
        format!("implementation_{id}"),
        format!("implementations_agent_{agent_id}"),
    ]
}

/// An implementation was saved (`implementation_post_save`): its detail feed and its agent's
/// implementation list.
pub async fn implementation_saved(ctx: &Context, id: i64, created: bool) {
    match sqlx::query_scalar::<_, i64>("SELECT agent_id FROM facade_implementation WHERE id = $1")
        .bind(id)
        .fetch_one(&ctx.db)
        .await
    {
        Ok(agent_id) => {
            publish(
                ctx,
                channels::NEW_IMPLEMENTATION,
                &CrudEvent::saved(id, created),
                implementation_groups(id, agent_id),
            )
            .await
        }
        Err(e) => tracing::error!(id, "implementation fan-out failed: {e}"),
    }
}

/// An implementation was deleted (`implementation_post_del`). The row is gone, so its agent
/// is passed in.
pub async fn implementation_deleted(ctx: &Context, id: i64, agent_id: i64) {
    publish(
        ctx,
        channels::NEW_IMPLEMENTATION,
        &CrudEvent::deleted(id),
        implementation_groups(id, agent_id),
    )
    .await;
}

/// A `post_save` / `post_delete` a transaction's writes fired, published once it commits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Signal {
    AgentSaved { id: i64, created: bool },
    AgentDeleted { id: i64, organization: i64 },
    ImplementationSaved { id: i64, created: bool },
    ImplementationDeleted { id: i64, agent_id: i64 },
    StateSaved { id: i64 },
}

/// The signals of one transaction, in the order Django would have fired them (`on_commit`).
/// A repeat of the same signal is dropped: Django fires one per `save()`, and a registration
/// saves an action several times over, each fan-out telling the feeds the same thing.
#[derive(Debug, Default)]
pub struct OnCommit(Vec<Signal>);

impl OnCommit {
    pub fn push(&mut self, signal: Signal) {
        if !self.0.contains(&signal) {
            self.0.push(signal);
        }
    }

    pub fn signals(&self) -> &[Signal] {
        &self.0
    }

    /// Publish, after the commit.
    pub async fn publish(self, ctx: &Context) {
        for signal in self.0 {
            match signal {
                Signal::AgentSaved { id, created } => agent_saved(ctx, id, created).await,
                Signal::AgentDeleted { id, organization } => {
                    agent_deleted(ctx, id, organization).await
                }
                Signal::ImplementationSaved { id, created } => {
                    implementation_saved(ctx, id, created).await
                }
                Signal::ImplementationDeleted { id, agent_id } => {
                    implementation_deleted(ctx, id, agent_id).await
                }
                Signal::StateSaved { id } => state_saved(ctx, id).await,
            }
        }
    }
}

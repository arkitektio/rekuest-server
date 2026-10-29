//! Work an agent originates over its own socket, as a *caller* (`facade/persist/caller_ops.py`).
//!
//! An agent may assign dependent work and control what it assigned. Roots are not available
//! here: they must trace to an accountable human, so they come only from the GraphQL `assign`
//! mutation (the human-root invariant of `facade::provenance`).

use chrono::Utc;
use sqlx::PgPool;

use crate::backend::{
    self, parse_id, AssignInput, AssignOrigin, Assigned, BackendError, BackendResult, Control,
    HookInput,
};
use crate::caller_context::CallerContext;
use crate::context::Context;
use crate::messages::FromAgent;
use crate::provenance::principal;

/// The durable `Caller` for an agent's own identity: a connection joins `task_caller_{id}` to
/// receive the events of work it assigned (`get_or_create_caller_id`).
pub async fn get_or_create_caller_id(db: &PgPool, agent: i64) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar(
        "INSERT INTO facade_caller (client_id, user_id, organization_id)
         SELECT client_id, user_id, organization_id FROM facade_agent WHERE id = $1
         ON CONFLICT (client_id, user_id, organization_id) DO UPDATE SET client_id = EXCLUDED.client_id
         RETURNING id",
    )
    .bind(agent)
    .fetch_one(db)
    .await
}

/// The agent's identity as the backend reads it, with its caller's persisted roles.
pub async fn caller_context(db: &PgPool, agent: i64, caller: i64) -> BackendResult<CallerContext> {
    let roles = principal::roles_for_caller(db, caller).await?;
    Ok(CallerContext::from_agent(db, agent, roles).await?)
}

/// An `ASSIGN_REQUEST` as the assign it asks for (`AssignInputModel(...)`); `None` for any
/// other frame.
pub fn assign_input(message: &FromAgent<impl Sized>) -> Option<BackendResult<AssignInput>> {
    let FromAgent::AssignRequest {
        reference,
        parent_step,
        call_key,
        args,
        action,
        action_hash,
        implementation,
        agent,
        interface,
        parent,
        dependency,
        method,
        resolution,
        hooks,
        capture,
        step,
        prompt,
        state_requires,
    } = message
    else {
        return None;
    };
    let hooks = hooks
        .as_ref()
        .map(|hooks| {
            hooks
                .iter()
                .map(|hook| serde_json::from_value::<HookInput>(hook.clone()))
                .collect::<Result<Vec<_>, _>>()
        })
        .transpose()
        .map_err(|e| BackendError::Refused(format!("hooks: {e}")));
    Some(hooks.map(|hooks| AssignInput {
        action: action.clone(),
        dependency: dependency.clone(),
        resolution: resolution.clone(),
        implementation: implementation.clone(),
        agent: agent.clone(),
        action_hash: action_hash.clone(),
        method: method.clone(),
        interface: interface.clone(),
        hooks,
        args: args.clone(),
        reference: reference.clone(),
        parent: parent.clone(),
        parent_step: parent_step.map(|s| s as i64),
        call_key: call_key.clone(),
        capture: *capture,
        dependencies: None,
        step: *step,
        not_before: None,
        // An agent never sets its children's policy: they take their parent's.
        policy: None,
        prompt: prompt.clone(),
        state_requires: (!state_requires.is_empty()).then(|| state_requires.clone()),
        // An agent's child runs in its root's space.
        space: None,
        unheld: false,
    }))
}

/// Assign dependent work requested by an agent (`on_caller_assign`). Idempotent like every
/// assign; a parentless (root) request is refused.
pub async fn on_caller_assign(
    ctx: &Context,
    agent: i64,
    input: &AssignInput,
) -> BackendResult<Assigned> {
    let caller = get_or_create_caller_id(&ctx.db, agent).await?;
    if input.parent.is_none() {
        return Err(BackendError::Forbidden(
            "An agent may only assign dependent work: 'parent' is required. Root tasks originate from the GraphQL assign mutation, where the initiator is an accountable human.".into(),
        ));
    }
    allowed_to_call(&ctx.db, agent, input).await?;
    let principal = caller_context(&ctx.db, agent, caller).await?;
    // A dependent task's fate follows its parent: nothing about this connection is recorded.
    backend::assign_with_status(ctx, &principal, input, AssignOrigin::default()).await
}

/// The scope a task's agent needs to call through a dependency its action declares.
pub const DEPENDENCY_CALL_SCOPE: &str = "rekuest_dependency_call";
/// The scope a task's agent needs to call an action found at run time (`dynamic_calls`).
pub const DYNAMIC_CALL_SCOPE: &str = "rekuest_dynamic_call";

/// An agent's child call: only from one of its own tasks. (`dynamic_calls` is a WORKFLOW's only,
/// refused at registration otherwise; that a PLAIN one calls no dependency is the client's
/// rule.) Through a declared
/// dependency (`dependency`), with the agent's token granting `rekuest_dependency_call`;
/// naming its target directly, or carrying hooks that start more work, it calls what it found
/// at run time, which its implementation must declare (`dynamic_calls`) and the token grant
/// (`rekuest_dynamic_call`).
async fn allowed_to_call(db: &PgPool, agent: i64, input: &AssignInput) -> BackendResult<()> {
    let parent = parse_id(input.parent.as_deref().unwrap_or_default())?;
    let row: Option<(Option<i64>, bool)> = sqlx::query_as(
        "SELECT t.agent_id, coalesce(i.dynamic_calls, false)
           FROM facade_task t LEFT JOIN facade_implementation i ON i.id = t.implementation_id
          WHERE t.id = $1",
    )
    .bind(parent)
    .fetch_optional(db)
    .await?;
    let Some((owner, dynamic)) = row else {
        return Err(BackendError::Forbidden(format!(
            "No task {parent} to call from."
        )));
    };
    if owner != Some(agent) {
        return Err(BackendError::Forbidden(format!(
            "Task {parent} is not one of this agent's: an agent calls only from the tasks it runs."
        )));
    }
    let starts_more = input.hooks.as_ref().is_some_and(|hooks| !hooks.is_empty());
    if input.dependency.is_some() && !starts_more {
        if !crate::registration::agent_granted(db, agent, DEPENDENCY_CALL_SCOPE).await? {
            return Err(BackendError::Forbidden(format!(
                "This agent's token does not grant calling its declared dependencies: log the app in with the {DEPENDENCY_CALL_SCOPE} scope."
            )));
        }
        return Ok(());
    }
    let what = if input.dependency.is_some() {
        "called with hooks that start more work"
    } else {
        "called an action it found at run time"
    };
    if !dynamic {
        return Err(BackendError::Forbidden(format!(
            "Task {parent} {what}, which its action does not declare (dynamic_calls): call through a declared dependency, or set dynamic_calls."
        )));
    }
    if !crate::registration::agent_granted(db, agent, DYNAMIC_CALL_SCOPE).await? {
        return Err(BackendError::Forbidden(format!(
            "This agent's token does not grant calling actions found at run time: log the app in with the {DYNAMIC_CALL_SCOPE} scope."
        )));
    }
    Ok(())
}

/// Ownership-check, then run a control op (`_caller_control_sync`): a caller controls only the
/// tasks it assigned.
pub async fn caller_control(
    ctx: &Context,
    agent: i64,
    task: &str,
    control: Control,
) -> BackendResult<i64> {
    let caller = get_or_create_caller_id(&ctx.db, agent).await?;
    let owner: Option<Option<i64>> =
        sqlx::query_scalar("SELECT caller_id FROM facade_task WHERE id = $1")
            .bind(parse_id(task)?)
            .fetch_optional(&ctx.db)
            .await?;
    let Some(owner) = owner else {
        return Err(BackendError::Refused(
            "Task matching query does not exist.".into(),
        ));
    };
    if owner != Some(caller) {
        return Err(BackendError::Forbidden(
            "Not authorized to control this task (not its caller).".into(),
        ));
    }
    backend::request_control(ctx, task, control, Some(caller)).await
}

/// `CANCEL_REQUEST` (`on_caller_cancel`): with `auto_interrupt`, the escalation deadline is that
/// many seconds from now, winning over the global control deadline. A column, not a timer: the
/// reaper's `escalate_due_controls` fires it.
pub async fn on_caller_cancel(
    ctx: &Context,
    agent: i64,
    task: &str,
    auto_interrupt: Option<f64>,
) -> BackendResult<i64> {
    let task = caller_control(ctx, agent, task, Control::Cancel).await?;
    if let Some(seconds) = auto_interrupt {
        let at = Utc::now() + chrono::Duration::milliseconds((seconds * 1000.0) as i64);
        sqlx::query("UPDATE facade_task SET interrupt_at = $2 WHERE id = $1 AND is_done = false")
            .bind(task)
            .bind(at)
            .execute(&ctx.db)
            .await?;
    }
    Ok(task)
}

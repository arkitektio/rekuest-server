//! The postman backend: resolve and persist tasks, then notify the agents (`facade/backend.py`).
//!
//! [`assign_with_status`] creates (or finds) the task of an assign and hands its `ASSIGN` to the
//! agent once the row is committed; the lifecycle controls ([`cancel`], [`interrupt`],
//! [`pause`], [`resume`]) are the request phase of a two-phase op, resolved when the executing
//! agent confirms; [`bounce`], [`kick`], [`block`], [`unblock`] and [`collect`] address agents.
//!
//! Errors carry the Python server's messages: [`BackendError::Refused`] is its `ValueError` /
//! `DoesNotExist`, [`BackendError::Forbidden`] its `PermissionError`.

use std::time::Duration;

use chrono::{DateTime, Utc};
use rand::seq::IndexedRandom;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sqlx::{PgConnection, PgPool};

use rekuest_core::inputs::ArgPortInputModel;
use rekuest_core::pyjson;
use rekuest_core::values::validate_assignment_args;

use crate::caller_context::CallerContext;
use crate::context::Context;
use crate::higher_order::{build_lower_args, build_lower_dependencies};
use crate::messages::{Assign, ToAgent};
use crate::persist::transitions::{insert_event, NewEvent};
use crate::provenance::canonical::args_hash;
use crate::provenance::{mint_token_for_task, MintError, MintTask};
use crate::{signals, transport};

#[derive(Debug, thiserror::Error)]
pub enum BackendError {
    /// A refusal: bad input, an unknown target, a task already terminal (`ValueError`,
    /// `DoesNotExist`).
    #[error("{0}")]
    Refused(String),
    /// Not the caller's to do (`PermissionError`).
    #[error("{0}")]
    Forbidden(String),
    #[error("{0}")]
    Database(#[from] sqlx::Error),
}

impl From<MintError> for BackendError {
    fn from(e: MintError) -> Self {
        match e {
            MintError::Refused(message) => BackendError::Refused(message),
            MintError::Database(e) => BackendError::Database(e),
        }
    }
}

pub type BackendResult<T> = Result<T, BackendError>;

/// `Model.DoesNotExist`'s message.
fn does_not_exist(model: &str) -> BackendError {
    BackendError::Refused(format!("{model} matching query does not exist."))
}

/// A row that must exist: `.get()`.
fn get<T>(row: Option<T>, model: &str) -> BackendResult<T> {
    row.ok_or_else(|| does_not_exist(model))
}

/// An id off the wire, as Django's integer field reads it.
pub fn parse_id(value: &str) -> BackendResult<i64> {
    value.trim().parse().map_err(|_| {
        BackendError::Refused(format!(
            "Field 'id' expected a number but got {}.",
            pyjson::repr_str(value)
        ))
    })
}

/// A hook of an assign (`HookInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HookInput {
    /// `HookKind`: `INIT` or `CLEANUP`.
    pub kind: String,
    pub hash: String,
}

/// An agent a dependency is mapped to (`MappedAgentInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MappedAgentInput {
    pub key: String,
    pub agent: String,
}

/// The caller's resolution of one dependency (`ResolvedDependencyInputModel`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResolvedDependencyInput {
    pub key: String,
    #[serde(default)]
    pub mapped_agents: Vec<MappedAgentInput>,
    #[serde(default)]
    pub auto_resolve: bool,
}

/// What an assign asks for (`AssignInputModel`).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AssignInput {
    #[serde(default)]
    pub action: Option<String>,
    #[serde(default)]
    pub dependency: Option<String>,
    #[serde(default)]
    pub resolution: Option<String>,
    #[serde(default)]
    pub implementation: Option<String>,
    #[serde(default)]
    pub agent: Option<String>,
    #[serde(default)]
    pub action_hash: Option<String>,
    #[serde(default)]
    pub method: Option<String>,
    #[serde(default)]
    pub interface: Option<String>,
    #[serde(default)]
    pub hooks: Option<Vec<HookInput>>,
    #[serde(default)]
    pub args: Map<String, Value>,
    #[serde(default)]
    pub reference: Option<String>,
    #[serde(default)]
    pub parent: Option<String>,
    #[serde(default)]
    pub parent_step: Option<i64>,
    #[serde(default)]
    pub call_key: Option<String>,
    #[serde(default)]
    pub capture: Option<bool>,
    #[serde(default)]
    pub dependencies: Option<Vec<ResolvedDependencyInput>>,
    #[serde(default)]
    pub step: Option<bool>,
    #[serde(default)]
    pub not_before: Option<DateTime<Utc>>,
}

/// What fired a task, server-internally (`assign_with_status`'s keyword arguments).
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct AssignOrigin {
    pub schedule: Option<i64>,
    pub signal: Option<i64>,
    pub trigger: Option<i64>,
    pub trigger_depth: i16,
}

/// The task an assign created or found.
#[derive(Debug, Clone, PartialEq)]
pub struct Assigned {
    pub task: i64,
    pub reference: String,
    /// False for a resend: the task already existed.
    pub created: bool,
}

// ---------------------------------------------------------------------------------------------
// availability and resolution
// ---------------------------------------------------------------------------------------------

/// The earliest heartbeat that still counts as live (`liveness.live_agent_q`).
fn live_cutoff(ctx: &Context) -> DateTime<Utc> {
    Utc::now()
        - chrono::Duration::from_std(ctx.settings.agent_stale_after)
            .unwrap_or(chrono::Duration::MAX)
}

/// `agent_available_q` over `facade_agent a`, the cutoff bound as `$1`: a live websocket, or any
/// webhook agent (which never sets `connected`/`last_seen`).
const AVAILABLE: &str = "(a.kind = 'WEBHOOK' OR (a.connected AND a.last_seen > $1))";

/// Whether an agent can receive work (`agent_is_available`).
pub async fn agent_is_available(ctx: &Context, agent: i64) -> BackendResult<bool> {
    let available: Option<bool> = sqlx::query_scalar(&format!(
        "SELECT {AVAILABLE} FROM facade_agent a WHERE a.id = $2"
    ))
    .bind(live_cutoff(ctx))
    .bind(agent)
    .fetch_optional(&ctx.db)
    .await?;
    get(available, "Agent")
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub(crate) struct ActionRow {
    pub id: i64,
    pub name: String,
    pub hash: String,
    pub args: Value,
    pub allow_probe: bool,
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub(crate) struct ImplementationRow {
    pub id: i64,
    pub interface: String,
    pub action_id: i64,
    pub agent_id: i64,
    pub higher_order_for_id: Option<i64>,
    pub higher_order_config: Value,
    pub code_hash: Option<String>,
}

const IMPLEMENTATION: &str = "i.id, i.interface, i.action_id, i.agent_id, i.higher_order_for_id, \
                              i.higher_order_config, i.code_hash";

async fn action_row(db: &PgPool, id: i64) -> BackendResult<ActionRow> {
    get(
        sqlx::query_as("SELECT id, name, hash, args, allow_probe FROM facade_action WHERE id = $1")
            .bind(id)
            .fetch_optional(db)
            .await?,
        "Action",
    )
}

async fn implementation_row(db: &PgPool, id: i64) -> BackendResult<ImplementationRow> {
    get(
        sqlx::query_as(&format!(
            "SELECT {IMPLEMENTATION} FROM facade_implementation i WHERE i.id = $1"
        ))
        .bind(id)
        .fetch_optional(db)
        .await?,
        "Implementation",
    )
}

/// The first implementation of `action` whose agent is available, by id.
async fn available_implementation(
    ctx: &Context,
    action: &ActionRow,
) -> BackendResult<ImplementationRow> {
    sqlx::query_as(&format!(
        "SELECT {IMPLEMENTATION} FROM facade_implementation i JOIN facade_agent a ON a.id = i.agent_id
          WHERE i.action_id = $2 AND {AVAILABLE} ORDER BY i.id LIMIT 1"
    ))
    .bind(live_cutoff(ctx))
    .bind(action.id)
    .fetch_optional(&ctx.db)
    .await?
    .ok_or_else(|| {
        BackendError::Refused(format!(
            "No active implementation found for action {}",
            action.name
        ))
    })
}

const NOT_AVAILABLE: &str = "Agent is not available (not connected, and not a webhook agent)";

/// `(action, implementation)` from one of the direct target forms (`resolve_direct_target`).
/// Precedence: action → implementation → action_hash → agent+interface.
pub(crate) async fn resolve_direct_target(
    ctx: &Context,
    input: &AssignInput,
    organization: i64,
) -> BackendResult<(ActionRow, ImplementationRow)> {
    if let Some(action) = input.action.as_deref().filter(|a| !a.is_empty()) {
        let action = action_row(&ctx.db, parse_id(action)?).await?;
        let implementation = available_implementation(ctx, &action).await?;
        return Ok((action, implementation));
    }
    if let Some(implementation) = input.implementation.as_deref().filter(|i| !i.is_empty()) {
        let implementation = implementation_row(&ctx.db, parse_id(implementation)?).await?;
        // A higher-order wrapper's agent is checked by the higher-order path.
        if implementation.higher_order_for_id.is_none()
            && !agent_is_available(ctx, implementation.agent_id).await?
        {
            return Err(BackendError::Refused(NOT_AVAILABLE.into()));
        }
        let action = action_row(&ctx.db, implementation.action_id).await?;
        return Ok((action, implementation));
    }
    if let Some(hash) = input.action_hash.as_deref().filter(|h| !h.is_empty()) {
        let action: ActionRow = get(
            sqlx::query_as(
                "SELECT id, name, hash, args, allow_probe FROM facade_action WHERE hash = $1 AND organization_id = $2",
            )
            .bind(hash)
            .bind(organization)
            .fetch_optional(&ctx.db)
            .await?,
            "Action",
        )?;
        let implementation = available_implementation(ctx, &action).await?;
        return Ok((action, implementation));
    }
    if let (Some(agent), Some(interface)) = (
        input.agent.as_deref().filter(|a| !a.is_empty()),
        input.interface.as_deref().filter(|i| !i.is_empty()),
    ) {
        let implementation: ImplementationRow = get(
            sqlx::query_as(&format!(
                "SELECT {IMPLEMENTATION} FROM facade_implementation i WHERE i.agent_id = $1 AND i.interface = $2"
            ))
            .bind(parse_id(agent)?)
            .bind(interface)
            .fetch_optional(&ctx.db)
            .await?,
            "Implementation",
        )?;
        if implementation.higher_order_for_id.is_none()
            && !agent_is_available(ctx, implementation.agent_id).await?
        {
            return Err(BackendError::Refused(NOT_AVAILABLE.into()));
        }
        let action = action_row(&ctx.db, implementation.action_id).await?;
        return Ok((action, implementation));
    }
    Err(BackendError::Refused(
        "You need to provide an action, action_hash, implementation, or agent+interface to create an assignment for an agent".into(),
    ))
}

/// `(action, implementation, dependencies)` from the parent's frozen dependency snapshot
/// (`resolve_dependency_target`): a dependency keeps hitting the peer chosen when the parent
/// was assigned, among those that can receive work right now.
async fn resolve_dependency_target(
    ctx: &Context,
    parent: Option<&str>,
    dependency: &str,
    method: Option<&str>,
) -> BackendResult<(ActionRow, ImplementationRow, Value)> {
    let Some(method) = method.filter(|m| !m.is_empty()) else {
        return Err(BackendError::Refused(
            "Method key must be provided when assigning to a dependency".into(),
        ));
    };
    let Some(parent) = parent.filter(|p| !p.is_empty()) else {
        return Err(BackendError::Refused(
            "Dependency assignments must have a parent task".into(),
        ));
    };
    let dependencies: Option<Value> = get(
        sqlx::query_scalar("SELECT dependencies FROM facade_task WHERE id = $1")
            .bind(parse_id(parent)?)
            .fetch_optional(&ctx.db)
            .await?,
        "Task",
    )?;
    let dependencies = dependencies.unwrap_or(Value::Null);
    let Some(entries) = dependencies.get(dependency) else {
        return Err(BackendError::Refused(format!(
            "Dependency {dependency} not found in parent task dependencies. {}",
            pyjson::repr(&dependencies)
        )));
    };
    let entries: Vec<&Value> = entries
        .as_array()
        .map(|e| e.iter().collect())
        .unwrap_or_default();
    let ids: Vec<i64> = entries
        .iter()
        .filter_map(|entry| entry.get("agent"))
        .filter_map(|agent| match agent {
            Value::String(s) => s.parse().ok(),
            Value::Number(n) => n.as_i64(),
            _ => None,
        })
        .collect();
    let available: Vec<i64> = sqlx::query_scalar(&format!(
        "SELECT a.id FROM facade_agent a WHERE a.id = ANY($2) AND {AVAILABLE}"
    ))
    .bind(live_cutoff(ctx))
    .bind(&ids)
    .fetch_all(&ctx.db)
    .await?;
    let candidates: Vec<&Value> = entries
        .into_iter()
        .filter(|entry| {
            entry
                .get("agent")
                .map(py_str)
                .is_some_and(|agent| available.iter().any(|id| id.to_string() == agent))
        })
        .collect();
    let Some(chosen) = candidates.choose(&mut rand::rng()) else {
        return Err(BackendError::Refused(format!(
            "No agent resolved for dependency {dependency} is available right now"
        )));
    };
    let Some(actions) = chosen.get("actions") else {
        return Err(BackendError::Refused(format!(
            "Dependency {dependency} does not contain an action"
        )));
    };
    let Some(implementation_dep) = actions.get(method) else {
        return Err(BackendError::Refused(format!(
            "Method {method} not found in dependency {dependency} actions"
        )));
    };
    let implementation = implementation_dep
        .get("implementation")
        .map(py_str)
        .unwrap_or_default();
    let implementation = implementation_row(&ctx.db, parse_id(&implementation)?).await?;
    let action = action_row(&ctx.db, implementation.action_id).await?;
    let nested = implementation_dep
        .get("dependencies")
        .cloned()
        .unwrap_or(Value::Null);
    Ok((action, implementation, nested))
}

/// `str(value)` of the Python object `json.loads` would build.
fn py_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        other => pyjson::repr(other),
    }
}

/// The structures an assignment acts on: `identifier:object` for each STRUCTURE arg
/// (`acted_on_from_args`).
fn acted_on_from_args(args: &Map<String, Value>, action_args: &Value) -> Vec<String> {
    let mut acted_on = vec![];
    for port in action_args.as_array().into_iter().flatten() {
        if port.get("kind").and_then(Value::as_str) != Some("STRUCTURE") {
            continue;
        }
        let (Some(identifier), Some(key)) = (
            port.get("identifier")
                .and_then(Value::as_str)
                .filter(|i| !i.is_empty()),
            port.get("key").and_then(Value::as_str),
        ) else {
            continue;
        };
        match args.get(key) {
            Some(Value::Object(reference)) => acted_on.push(format!(
                "{identifier}:{}",
                py_str(reference.get("object").unwrap_or(&Value::Null))
            )),
            Some(Value::String(object)) => acted_on.push(format!("{identifier}:{object}")),
            _ => {}
        }
    }
    acted_on
}

#[derive(Debug, sqlx::FromRow)]
struct DependencyRow {
    key: String,
    action_demands: Value,
    auto_resolvable: bool,
    app_filter: Option<String>,
    min_viable_instances: Option<i32>,
    max_viable_instances: Option<i32>,
}

/// The full set of available agents matching one dependency, by id
/// (`_resolve_dependency_agents`).
async fn resolve_dependency_agents(
    ctx: &Context,
    dependency: &DependencyRow,
    caller: &CallerContext,
    overwrites: &[ResolvedDependencyInput],
) -> BackendResult<Vec<i64>> {
    let by_app = || async {
        sqlx::query_scalar::<_, i64>(&format!(
            "SELECT a.id FROM facade_agent a JOIN authentikate_app app ON app.id = a.app_id
              WHERE app.identifier = $2 AND a.organization_id = $3 AND {AVAILABLE} ORDER BY a.id"
        ))
        .bind(live_cutoff(ctx))
        .bind(&dependency.app_filter)
        .bind(caller.organization)
        .fetch_all(&ctx.db)
        .await
    };
    match overwrites.iter().find(|o| o.key == dependency.key) {
        Some(overwrite) if overwrite.auto_resolve => {
            if !dependency.auto_resolvable {
                return Err(BackendError::Refused(format!(
                    "Dependency {} is not auto resolvable, but was provided with an overwrite that has auto_resolve set to true. Please either set auto_resolve to false for this dependency overwrite, or make the dependency auto resolvable in the system.",
                    dependency.key
                )));
            }
            Ok(by_app().await?)
        }
        Some(overwrite) => {
            let ids = overwrite
                .mapped_agents
                .iter()
                .map(|mapped| parse_id(&mapped.agent))
                .collect::<BackendResult<Vec<i64>>>()?;
            Ok(sqlx::query_scalar(&format!(
                "SELECT a.id FROM facade_agent a WHERE a.id = ANY($2) AND {AVAILABLE} ORDER BY a.id"
            ))
            .bind(live_cutoff(ctx))
            .bind(&ids)
            .fetch_all(&ctx.db)
            .await?)
        }
        None if dependency.auto_resolvable => Ok(by_app().await?),
        None => Err(BackendError::Refused(format!(
            "Dependency {} was not provided with an overwrite, and is not auto resolvable. Please provide a dependency overwrite for this dependency to ensure it can be resolved properly.",
            dependency.key
        ))),
    }
}

/// The per-agent implementation maps of one dependency (`_build_dependency_entries`): each
/// action demand's slot mapped to the agent's implementation of the demanded action. State
/// demands select agents; they have no per-call representation.
async fn build_dependency_entries(
    ctx: &Context,
    dependency: &DependencyRow,
    agents: &[i64],
) -> BackendResult<Vec<Value>> {
    // Slot → action key: the demand's key names the action; the slot key stands in for it
    // when the demand pins none. A later slot for the same action wins, as in the dict.
    let mut action_keys: Vec<(String, String)> = vec![];
    for demand in dependency.action_demands.as_array().into_iter().flatten() {
        let slot = demand
            .get("key")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        let action_key = demand
            .get("demand")
            .and_then(|d| d.get("key"))
            .and_then(Value::as_str)
            .filter(|k| !k.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| slot.clone());
        match action_keys.iter_mut().find(|(key, _)| *key == action_key) {
            Some(entry) => entry.1 = slot,
            None => action_keys.push((action_key, slot)),
        }
    }

    let mut found: Vec<(i64, String, i64, bool)> = vec![];
    if !agents.is_empty() && !action_keys.is_empty() {
        let keys: Vec<&str> = action_keys.iter().map(|(key, _)| key.as_str()).collect();
        found = sqlx::query_as(
            "SELECT i.agent_id, ac.key, i.id,
                    EXISTS (SELECT 1 FROM facade_dependency d WHERE d.implementation_id = i.id)
               FROM facade_implementation i JOIN facade_action ac ON ac.id = i.action_id
              WHERE i.agent_id = ANY($1) AND ac.key = ANY($2)
              ORDER BY i.id",
        )
        .bind(agents)
        .bind(&keys)
        .fetch_all(&ctx.db)
        .await?;
    }

    let mut entries = vec![];
    for agent in agents {
        let mut actions = Map::new();
        for (action_key, slot) in &action_keys {
            let Some((_, _, implementation, nested)) = found
                .iter()
                .rev()
                .find(|(a, key, _, _)| a == agent && key == action_key)
            else {
                return Err(BackendError::Refused(format!(
                    "No implementation found for dependency demand {slot} on agent {agent}"
                )));
            };
            if *nested {
                return Err(BackendError::Refused(
                    "Nested dependencies are not supported yet, but they are coming soon!".into(),
                ));
            }
            actions.insert(
                slot.clone(),
                json!({"implementation": implementation.to_string(), "dependencies": {}}),
            );
        }
        entries.push(json!({"agent": agent.to_string(), "actions": actions}));
    }
    Ok(entries)
}

/// The dependency snapshot frozen onto a task (`build_dependency_dict`).
async fn build_dependency_dict(
    ctx: &Context,
    implementation: i64,
    caller: &CallerContext,
    overwrites: &[ResolvedDependencyInput],
) -> BackendResult<Map<String, Value>> {
    let dependencies: Vec<DependencyRow> = sqlx::query_as(
        "SELECT key, action_demands, auto_resolvable, app_filter, min_viable_instances,
                max_viable_instances
           FROM facade_dependency WHERE implementation_id = $1 ORDER BY id",
    )
    .bind(implementation)
    .fetch_all(&ctx.db)
    .await?;
    let mut dict = Map::new();
    for dependency in &dependencies {
        let mut agents = resolve_dependency_agents(ctx, dependency, caller, overwrites).await?;
        // The full match set is counted before it is capped at max.
        if let Some(min) = dependency.min_viable_instances {
            if (agents.len() as i64) < i64::from(min) {
                return Err(BackendError::Refused(format!(
                    "Not enough agents found for dependency {}. Required at least {min} but found only {}. Please ensure that there are enough agents available to resolve this dependency.",
                    dependency.key,
                    agents.len()
                )));
            }
        }
        if let Some(max) = dependency.max_viable_instances {
            agents.truncate(max.max(0) as usize);
        }
        dict.insert(
            dependency.key.clone(),
            Value::Array(build_dependency_entries(ctx, dependency, &agents).await?),
        );
    }
    Ok(dict)
}

/// The durable `Caller` of an identity (`get_caller_for_context`).
pub async fn get_caller_for_context(
    executor: impl sqlx::PgExecutor<'_>,
    caller: &CallerContext,
) -> BackendResult<i64> {
    let Some(organization) = caller.organization else {
        return Err(BackendError::Refused(
            "Cannot assign without an organization".into(),
        ));
    };
    Ok(sqlx::query_scalar(
        "INSERT INTO facade_caller (client_id, user_id, organization_id) VALUES ($1, $2, $3)
         ON CONFLICT (client_id, user_id, organization_id) DO UPDATE SET client_id = EXCLUDED.client_id
         RETURNING id",
    )
    .bind(caller.client)
    .bind(caller.user)
    .bind(organization)
    .fetch_one(executor)
    .await?)
}

// ---------------------------------------------------------------------------------------------
// assign
// ---------------------------------------------------------------------------------------------

#[derive(Debug, sqlx::FromRow)]
struct ExistingRow {
    id: i64,
    reference: String,
    call_key: Option<String>,
    action_id: i64,
    implementation_id: Option<i64>,
    action_hash: String,
    dependency: Option<String>,
    dependency_method: Option<String>,
}

/// The task an earlier delivery of this very assign created (`_existing_assign`): by
/// `(parent, call_key)`, then `(parent, parent_step)`, then `(caller, reference)`.
async fn existing_assign(
    db: &PgPool,
    caller: i64,
    input: &AssignInput,
    reference: Option<&str>,
) -> BackendResult<Option<ExistingRow>> {
    const EXISTING: &str = "SELECT t.id, t.reference, t.call_key, t.action_id, t.implementation_id,
                                   a.hash AS action_hash, t.dependency, t.dependency_method
                              FROM facade_task t JOIN facade_action a ON a.id = t.action_id";
    let parent = input.parent.as_deref().map(parse_id).transpose()?;
    if let (Some(parent), Some(call_key)) = (parent, input.call_key.as_deref()) {
        let found = sqlx::query_as(&format!(
            "{EXISTING} WHERE t.parent_id = $1 AND t.call_key = $2 ORDER BY t.id LIMIT 1"
        ))
        .bind(parent)
        .bind(call_key)
        .fetch_optional(db)
        .await?;
        if found.is_some() {
            return Ok(found);
        }
    }
    if let (Some(parent), Some(step)) = (parent, input.parent_step) {
        let found = sqlx::query_as(&format!(
            "{EXISTING} WHERE t.parent_id = $1 AND t.parent_step = $2 ORDER BY t.id LIMIT 1"
        ))
        .bind(parent)
        .bind(step)
        .fetch_optional(db)
        .await?;
        if found.is_some() {
            return Ok(found);
        }
    }
    if let Some(reference) = reference {
        return Ok(sqlx::query_as(&format!(
            "{EXISTING} WHERE t.caller_id = $1 AND t.reference = $2 ORDER BY t.id LIMIT 1"
        ))
        .bind(caller)
        .bind(reference)
        .fetch_optional(db)
        .await?);
    }
    Ok(None)
}

/// A call key found again must name the call it named before (`_check_it_is_the_same_call`):
/// a resumed workflow that names something else took another path.
fn check_it_is_the_same_call(existing: &ExistingRow, input: &AssignInput) -> BackendResult<()> {
    let Some(call_key) = input.call_key.as_deref() else {
        return Ok(());
    };
    if existing.call_key.as_deref() != Some(call_key) {
        return Ok(());
    }
    let differs = input
        .action
        .as_deref()
        .is_some_and(|a| existing.action_id.to_string() != a)
        || input.implementation.as_deref().is_some_and(|i| {
            existing
                .implementation_id
                .map_or("None".to_owned(), |id| id.to_string())
                != i
        })
        || input
            .action_hash
            .as_deref()
            .is_some_and(|h| existing.action_hash != h)
        || (input.dependency.is_some()
            && (
                existing.dependency.as_deref(),
                existing.dependency_method.as_deref(),
            ) != (input.dependency.as_deref(), input.method.as_deref()));
    if differs {
        return Err(BackendError::Refused(format!(
            "Nondeterministic workflow: call {} of task {} was made to action {} before, and names something else now.",
            pyjson::repr_str(call_key),
            input.parent.as_deref().unwrap_or("None"),
            existing.action_id
        )));
    }
    Ok(())
}

/// Every column of a new `Task` row: Django fills its defaults, SQL has to name them.
#[derive(Debug, Clone)]
struct NewTask {
    action: i64,
    args: Map<String, Value>,
    reference: String,
    parent: Option<i64>,
    parent_step: Option<i64>,
    call_key: Option<String>,
    root: Option<i64>,
    agent: i64,
    acted_on: Vec<String>,
    capture: bool,
    step: bool,
    implementation: i64,
    code_hash: Option<String>,
    dependency: Option<String>,
    dependency_method: Option<String>,
    resolution: Option<i64>,
    is_higher_order_child: bool,
    hooks: Value,
    dependencies: Value,
    caller: i64,
    dispatched_at: Option<DateTime<Utc>>,
    dispatch_attempts: i16,
    not_before: Option<DateTime<Utc>>,
    schedule: Option<i64>,
    ephemeral: bool,
    signal: Option<i64>,
    trigger: Option<i64>,
    trigger_depth: i16,
}

impl NewTask {
    /// A task with every optional column at its model default.
    fn new(action: i64, implementation: i64, agent: i64, caller: i64, reference: String) -> Self {
        Self {
            action,
            args: Map::new(),
            reference,
            parent: None,
            parent_step: None,
            call_key: None,
            root: None,
            agent,
            acted_on: vec![],
            capture: false,
            step: false,
            implementation,
            code_hash: None,
            dependency: None,
            dependency_method: None,
            resolution: None,
            is_higher_order_child: false,
            hooks: json!([]),
            dependencies: Value::Null,
            caller,
            dispatched_at: None,
            dispatch_attempts: 0,
            not_before: None,
            schedule: None,
            ephemeral: false,
            signal: None,
            trigger: None,
            trigger_depth: 0,
        }
    }

    async fn insert(&self, conn: &mut PgConnection) -> Result<i64, sqlx::Error> {
        sqlx::query_scalar(
            "INSERT INTO facade_task
                (action_id, args, args_hash, reference, parent_id, parent_step, call_key, root_id,
                 agent_id, acted_on, capture, step, implementation_id, code_hash, dependency,
                 dependency_method, resolution_id, is_higher_order_child, is_done,
                 latest_event_kind, latest_instruct_kind, statusmessage, hooks, dependencies,
                 caller_id, dispatched_at, dispatch_attempts, not_before, schedule_id, ephemeral,
                 signal_id, trigger_id, trigger_depth, resumes, revision, created_at, updated_at)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18,
                     false, 'QUEUED', 'ASSIGN', '', $19, $20, $21, $22, $23, $24, $25, $26, $27, $28,
                     $29, 0, 1, now(), now())
             RETURNING id",
        )
        .bind(self.action)
        .bind(sqlx::types::Json(Value::Object(self.args.clone())))
        .bind(args_hash(&self.args))
        .bind(&self.reference)
        .bind(self.parent)
        .bind(self.parent_step)
        .bind(&self.call_key)
        .bind(self.root)
        .bind(self.agent)
        .bind(&self.acted_on)
        .bind(self.capture)
        .bind(self.step)
        .bind(self.implementation)
        .bind(&self.code_hash)
        .bind(&self.dependency)
        .bind(&self.dependency_method)
        .bind(self.resolution)
        .bind(self.is_higher_order_child)
        .bind(sqlx::types::Json(&self.hooks))
        .bind(sqlx::types::Json(&self.dependencies))
        .bind(self.caller)
        .bind(self.dispatched_at)
        .bind(self.dispatch_attempts)
        .bind(self.not_before)
        .bind(self.schedule)
        .bind(self.ephemeral)
        .bind(self.signal)
        .bind(self.trigger)
        .bind(self.trigger_depth)
        .fetch_one(conn)
        .await
    }
}

fn is_unique_violation(e: &sqlx::Error) -> bool {
    matches!(e, sqlx::Error::Database(db) if db.is_unique_violation())
}

/// Hand a committed task's `ASSIGN` to its agent (`_dispatch`). Never fails: a transport failure
/// is recorded as `dispatched_at = NULL` ("never left"), which the pickup watchdog retries.
async fn dispatch(ctx: &Context, task: i64, agent: i64, assign: Assign) -> bool {
    let delivered = match transport::deliver_to_agent(
        ctx,
        agent,
        ToAgent::Assign(Box::new(assign)),
        false,
    )
    .await
    {
        Ok(delivered) => delivered,
        Err(e) => {
            tracing::error!(
                task,
                agent,
                "dispatching task {task} to agent {agent} failed; the pickup watchdog will retry it: {e}"
            );
            false
        }
    };
    if !delivered {
        if let Err(e) = sqlx::query(
            "UPDATE facade_task SET dispatched_at = NULL WHERE id = $1 AND is_done = false",
        )
        .bind(task)
        .execute(&ctx.db)
        .await
        {
            tracing::error!(task, "recording an undelivered dispatch failed: {e}");
        }
    }
    delivered
}

/// Create (or find) the task of an assign (`assign_with_status`). `created` is false for a resend.
///
/// Idempotency is a database guarantee: the lookup up front is a fast path, and of two
/// concurrent retries the unique constraints let one insert through; the loser returns the
/// winner's task without dispatching it and without running its hooks.
pub async fn assign_with_status(
    ctx: &Context,
    principal: &CallerContext,
    input: &AssignInput,
    origin: AssignOrigin,
) -> BackendResult<Assigned> {
    let Some(organization) = principal.organization else {
        return Err(BackendError::Refused(
            "Cannot assign without an organization".into(),
        ));
    };
    let resolution = match input.resolution.as_deref().filter(|r| !r.is_empty()) {
        Some(resolution) => Some(get(
            sqlx::query_scalar::<_, i64>("SELECT id FROM facade_resolution WHERE id = $1")
                .bind(parse_id(resolution)?)
                .fetch_optional(&ctx.db)
                .await?,
            "Resolution",
        )?),
        None => None,
    };
    let caller = get_caller_for_context(&ctx.db, principal).await?;

    if let Some(existing) =
        existing_assign(&ctx.db, caller, input, input.reference.as_deref()).await?
    {
        check_it_is_the_same_call(&existing, input)?;
        return Ok(Assigned {
            task: existing.id,
            reference: existing.reference,
            created: false,
        });
    }

    let (action, implementation, mut dependency_dict) =
        match input.dependency.as_deref().filter(|d| !d.is_empty()) {
            Some(dependency) => {
                let (action, implementation, nested) = resolve_dependency_target(
                    ctx,
                    input.parent.as_deref(),
                    dependency,
                    input.method.as_deref(),
                )
                .await?;
                (action, implementation, Some(nested))
            }
            None => {
                let (action, implementation) =
                    resolve_direct_target(ctx, input, organization).await?;
                (action, implementation, None)
            }
        };

    // The args must fit the action's ports; an action without ports accepts anything.
    if action
        .args
        .as_array()
        .is_some_and(|ports| !ports.is_empty())
    {
        let ports: Vec<ArgPortInputModel> = serde_json::from_value(action.args.clone())
            .map_err(|e| BackendError::Refused(format!("The action's ports do not parse: {e}")))?;
        validate_assignment_args(&ports, &input.args).map_err(BackendError::Refused)?;
    }

    let parent = input.parent.as_deref().map(parse_id).transpose()?;
    let root = match parent {
        Some(parent) => {
            let (parent_root, parent_id): (Option<i64>, i64) = get(
                sqlx::query_as("SELECT root_id, id FROM facade_task WHERE id = $1")
                    .bind(parent)
                    .fetch_optional(&ctx.db)
                    .await?,
                "Task",
            )?;
            Some(parent_root.unwrap_or(parent_id))
        }
        None => None,
    };

    let not_before = input.not_before;
    let delayed = not_before.is_some_and(|at| at > Utc::now());
    let hooks = input.hooks.clone().unwrap_or_default();
    if delayed && !hooks.is_empty() {
        return Err(BackendError::Refused(
            "A delayed task (not_before in the future) cannot carry hooks".into(),
        ));
    }

    if implementation.higher_order_for_id.is_some() {
        if delayed {
            return Err(BackendError::Refused(
                "Higher-order implementations cannot be assigned with a future not_before".into(),
            ));
        }
        return assign_higher_order(ctx, principal, input, &implementation, caller, root, parent)
            .await;
    }

    let acted_on = acted_on_from_args(&input.args, &action.args);
    let reference = input
        .reference
        .clone()
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    if dependency_dict.is_none() {
        dependency_dict = Some(Value::Object(
            build_dependency_dict(
                ctx,
                implementation.id,
                principal,
                input.dependencies.as_deref().unwrap_or_default(),
            )
            .await?,
        ));
    }
    let ephemeral = match origin.schedule {
        Some(schedule) => {
            sqlx::query_scalar("SELECT ephemeral_runs FROM facade_schedule WHERE id = $1")
                .bind(schedule)
                .fetch_one(&ctx.db)
                .await?
        }
        None => false,
    };

    let row = NewTask {
        args: input.args.clone(),
        parent,
        parent_step: parent.and(input.parent_step),
        call_key: parent.and(input.call_key.clone()),
        root,
        acted_on,
        capture: input.capture.unwrap_or(false),
        step: input.step.unwrap_or(false),
        code_hash: implementation.code_hash.clone(),
        dependency: input.dependency.clone(),
        dependency_method: input.method.clone(),
        resolution,
        hooks: serde_json::to_value(&hooks).expect("hooks serialize"),
        dependencies: dependency_dict.unwrap_or(Value::Null),
        dispatched_at: (!delayed).then(Utc::now),
        dispatch_attempts: if delayed { 0 } else { 1 },
        not_before: if delayed { not_before } else { None },
        schedule: origin.schedule,
        ephemeral,
        signal: origin.signal,
        trigger: origin.trigger,
        trigger_depth: origin.trigger_depth,
        ..NewTask::new(
            action.id,
            implementation.id,
            implementation.agent_id,
            caller,
            reference.clone(),
        )
    };

    // Row and token are one transaction: a token the policy refuses rolls the row back.
    let mut tx = ctx.db.begin().await?;
    let task = match row.insert(&mut tx).await {
        Ok(task) => task,
        Err(e) if is_unique_violation(&e) => {
            drop(tx);
            return lost_reference_race(ctx, caller, input, &reference, e).await;
        }
        Err(e) => return Err(e.into()),
    };
    let token = mint_token_for_task(
        &mut tx,
        &ctx.settings,
        &MintTask {
            id: task.to_string(),
            parent_id: parent,
            implementation_id: implementation.id,
            agent_id: implementation.agent_id,
            args: &input.args,
        },
        principal,
    )
    .await?;
    tx.commit().await?;
    signals::task_saved(ctx, task, true).await;

    if delayed {
        return Ok(Assigned {
            task,
            reference,
            created: true,
        });
    }

    dispatch(
        ctx,
        task,
        implementation.agent_id,
        Assign {
            id: None,
            interface: implementation.interface.clone(),
            task: task.to_string(),
            root: root.map(|r| r.to_string()),
            parent: parent.map(|p| p.to_string()),
            resolution: resolution.map(|r| r.to_string()),
            step: input.step,
            probe: false,
            capture: Some(input.capture.unwrap_or(false)),
            reference: Some(reference.clone()),
            args: input.args.clone(),
            message: None,
            user: principal.user_sub.clone(),
            org: principal.organization_slug.clone().unwrap_or_default(),
            action: action.hash.clone(),
            implementation: implementation.id.to_string(),
            token,
            resume: None,
        },
    )
    .await;

    for hook in hooks.iter().filter(|hook| hook.kind == "INIT") {
        let hook_input = AssignInput {
            action_hash: Some(hook.hash.clone()),
            parent: Some(task.to_string()),
            args: json!({"task": task.to_string()})
                .as_object()
                .unwrap()
                .clone(),
            // Scoped to the parent: a constant reference would dedupe across tasks' hooks.
            reference: Some(format!("init_hook_0_{task}")),
            ..AssignInput::default()
        };
        Box::pin(assign_with_status(
            ctx,
            principal,
            &hook_input,
            AssignOrigin::default(),
        ))
        .await?;
    }

    Ok(Assigned {
        task,
        reference,
        created: true,
    })
}

/// The task another backend created for this very assign a moment ago (`_lost_reference_race`),
/// after the insert hit a unique constraint. None means the violation was about something else.
async fn lost_reference_race(
    ctx: &Context,
    caller: i64,
    input: &AssignInput,
    reference: &str,
    error: sqlx::Error,
) -> BackendResult<Assigned> {
    match existing_assign(&ctx.db, caller, input, Some(reference)).await? {
        Some(winner) => Ok(Assigned {
            task: winner.id,
            reference: winner.reference,
            created: false,
        }),
        None => Err(error.into()),
    }
}

/// A higher-order task (`_assign_higher_order`): a virtual wrapper that is never sent to an
/// agent, and a child on the lower implementation's agent that runs it. Its events are unfolded
/// back onto the wrapper (`persist::transitions::unfold_to_higher_order`).
async fn assign_higher_order(
    ctx: &Context,
    principal: &CallerContext,
    input: &AssignInput,
    higher: &ImplementationRow,
    caller: i64,
    root: Option<i64>,
    parent: Option<i64>,
) -> BackendResult<Assigned> {
    let config = &higher.higher_order_config;
    let lower = implementation_row(
        &ctx.db,
        higher
            .higher_order_for_id
            .expect("a higher-order implementation"),
    )
    .await?;
    let lower_action = action_row(&ctx.db, lower.action_id).await?;
    let higher_action = action_row(&ctx.db, higher.action_id).await?;
    if !agent_is_available(ctx, lower.agent_id).await? {
        return Err(BackendError::Refused(format!(
            "Agent for lower implementation {} is not available",
            lower.interface
        )));
    }

    let higher_dependencies = build_dependency_dict(
        ctx,
        higher.id,
        principal,
        input.dependencies.as_deref().unwrap_or_default(),
    )
    .await?;
    let lower_args = build_lower_args(config, &input.args).map_err(BackendError::Refused)?;
    let lower_dependencies =
        build_lower_dependencies(config, &higher_dependencies).map_err(BackendError::Refused)?;
    let reference = input
        .reference
        .clone()
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());

    let wrapper = NewTask {
        args: input.args.clone(),
        parent,
        parent_step: parent.and(input.parent_step),
        call_key: parent.and(input.call_key.clone()),
        root,
        acted_on: acted_on_from_args(&input.args, &higher_action.args),
        capture: input.capture.unwrap_or(false),
        hooks: serde_json::to_value(input.hooks.clone().unwrap_or_default())
            .expect("hooks serialize"),
        dependencies: Value::Object(higher_dependencies),
        ..NewTask::new(
            higher.action_id,
            higher.id,
            higher.agent_id,
            caller,
            reference.clone(),
        )
    };

    // Wrapper, child and the child's token are one transaction: a wrapper without its child
    // could never be finished by anyone. The wrapper carries the reference the constraints guard.
    let mut tx = ctx.db.begin().await?;
    let higher_task = match wrapper.insert(&mut tx).await {
        Ok(task) => task,
        Err(e) if is_unique_violation(&e) => {
            drop(tx);
            return lost_reference_race(ctx, caller, input, &reference, e).await;
        }
        Err(e) => return Err(e.into()),
    };
    let lower_root = root.unwrap_or(higher_task);
    let child = NewTask {
        args: lower_args.clone(),
        reference: uuid::Uuid::new_v4().to_string(),
        parent: Some(higher_task),
        root: Some(lower_root),
        is_higher_order_child: true,
        acted_on: acted_on_from_args(&lower_args, &lower_action.args),
        code_hash: lower.code_hash.clone(),
        step: input.step.unwrap_or(false),
        dependencies: Value::Object(lower_dependencies),
        dispatched_at: Some(Utc::now()),
        dispatch_attempts: 1,
        ..NewTask::new(
            lower.action_id,
            lower.id,
            lower.agent_id,
            caller,
            String::new(),
        )
    };
    let lower_task = child.insert(&mut tx).await?;
    let token = mint_token_for_task(
        &mut tx,
        &ctx.settings,
        &MintTask {
            id: lower_task.to_string(),
            parent_id: Some(higher_task),
            implementation_id: lower.id,
            agent_id: lower.agent_id,
            args: &lower_args,
        },
        principal,
    )
    .await?;
    tx.commit().await?;
    signals::task_saved(ctx, higher_task, true).await;
    signals::task_saved(ctx, lower_task, true).await;

    dispatch(
        ctx,
        lower_task,
        lower.agent_id,
        Assign {
            id: None,
            interface: lower.interface.clone(),
            task: lower_task.to_string(),
            root: Some(lower_root.to_string()),
            parent: Some(higher_task.to_string()),
            resolution: None,
            step: input.step,
            probe: false,
            capture: Some(false),
            reference: Some(child.reference.clone()),
            args: lower_args,
            message: None,
            user: principal.user_sub.clone(),
            org: principal.organization_slug.clone().unwrap_or_default(),
            action: lower_action.hash.clone(),
            implementation: lower.id.to_string(),
            token,
            resume: None,
        },
    )
    .await;

    Ok(Assigned {
        task: higher_task,
        reference,
        created: true,
    })
}

// ---------------------------------------------------------------------------------------------
// lifecycle controls
// ---------------------------------------------------------------------------------------------

/// A lifecycle control: what is instructed, the `-ING` event it records, and the frame it sends.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Control {
    Cancel,
    Interrupt,
    Pause,
    /// With `step`, stop again at the next pausepoint.
    Resume {
        step: bool,
    },
}

impl Control {
    /// `TaskInstructKind`.
    pub fn instruct_kind(self) -> &'static str {
        match self {
            Control::Cancel => "CANCEL",
            Control::Interrupt => "INTERRUPT",
            Control::Pause => "PAUSE",
            Control::Resume { .. } => "RESUME",
        }
    }

    fn inging_kind(self) -> &'static str {
        match self {
            Control::Cancel => "CANCELLING",
            Control::Interrupt => "INTERRUPTING",
            Control::Pause => "PAUSING",
            Control::Resume { .. } => "RESUMING",
        }
    }

    /// What a cancel / interrupt of a still-delayed task settles it as (`_WAITING_OUTCOME`).
    fn waiting_outcome(self) -> Option<&'static str> {
        match self {
            Control::Cancel => Some("CANCELLED"),
            Control::Interrupt => Some("INTERRUPTED"),
            _ => None,
        }
    }

    /// Interrupt reaches every still-running descendant; the others only the task.
    fn propagates(self) -> bool {
        self == Control::Interrupt
    }

    fn message(self, task: String) -> ToAgent {
        match self {
            Control::Cancel => ToAgent::Cancel { task },
            Control::Interrupt => ToAgent::Interrupt { task },
            Control::Pause => ToAgent::Pause { task },
            Control::Resume { step } => ToAgent::Resume { task, step },
        }
    }
}

/// `control_deadline_seconds`, as the deadline a cancel or interrupt arms.
fn control_deadline(ctx: &Context, control: Control) -> Option<DateTime<Utc>> {
    let deadline = ctx.settings.control_deadline;
    (deadline > Duration::ZERO && matches!(control, Control::Cancel | Control::Interrupt))
        .then(|| Utc::now() + chrono::Duration::from_std(deadline).unwrap_or(chrono::Duration::MAX))
}

#[derive(Debug, sqlx::FromRow)]
struct LockedTask {
    is_done: bool,
    not_before: Option<DateTime<Utc>>,
    dispatch_attempts: i16,
    picked_up_at: Option<DateTime<Utc>>,
}

/// The request phase of a two-phase lifecycle op (`_request_control`): per target, under its row
/// lock, `latest_instruct_kind`, the deadline, the `-ING` event and a `TaskInstruct` row, then
/// the control frame, best effort. A delayed task that was never handed over is settled here.
/// `caller` is `None` only on internal paths, which are trusted.
pub async fn request_control(
    ctx: &Context,
    task: &str,
    control: Control,
    caller: Option<i64>,
) -> BackendResult<i64> {
    let task_id = parse_id(task)?;
    let (is_done, organization): (bool, i64) = get(
        sqlx::query_as(
            "SELECT t.is_done, a.organization_id FROM facade_task t
               JOIN facade_agent a ON a.id = t.agent_id WHERE t.id = $1",
        )
        .bind(task_id)
        .fetch_optional(&ctx.db)
        .await?,
        "Task",
    )?;
    if let Some(caller) = caller {
        let caller_organization: i64 =
            sqlx::query_scalar("SELECT organization_id FROM facade_caller WHERE id = $1")
                .bind(caller)
                .fetch_one(&ctx.db)
                .await?;
        if caller_organization != organization {
            return Err(BackendError::Forbidden(format!(
                "Task {task} is not in your organization."
            )));
        }
    }
    if is_done {
        return Err(BackendError::Refused("Task is already terminal".into()));
    }

    let mut targets: Vec<(i64, i64)> = vec![
        sqlx::query_as("SELECT id, agent_id FROM facade_task WHERE id = $1")
            .bind(task_id)
            .fetch_one(&ctx.db)
            .await?,
    ];
    if control.propagates() {
        targets.extend(
            sqlx::query_as::<_, (i64, i64)>(
                "SELECT id, agent_id FROM facade_task WHERE root_id = $1 AND is_done = false ORDER BY id",
            )
            .bind(task_id)
            .fetch_all(&ctx.db)
            .await?,
        );
    }

    let interrupt_at = control_deadline(ctx, control);
    for (target, agent) in targets {
        // Re-read under the row lock: another backend may have finished the task since.
        let mut tx = ctx.db.begin().await?;
        let locked: Option<LockedTask> = sqlx::query_as(
            "SELECT is_done, not_before, dispatch_attempts, picked_up_at FROM facade_task
              WHERE id = $1 FOR UPDATE",
        )
        .bind(target)
        .fetch_optional(&mut *tx)
        .await?;
        let Some(locked) = locked.filter(|l| !l.is_done) else {
            continue;
        };
        let still_delayed = locked.not_before.is_some()
            && locked.dispatch_attempts == 0
            && locked.picked_up_at.is_none();
        if let Some(outcome) = control.waiting_outcome().filter(|_| still_delayed) {
            // Never handed over: no agent has it, nobody to confirm. Settle it, send nothing.
            sqlx::query(
                "UPDATE facade_task SET latest_instruct_kind = $2, latest_event_kind = $3,
                        is_done = true, finished_at = now(), revision = revision + 1, updated_at = now()
                  WHERE id = $1",
            )
            .bind(target)
            .bind(control.instruct_kind())
            .bind(outcome)
            .execute(&mut *tx)
            .await?;
            let event = insert_event(
                &mut *tx,
                target,
                outcome,
                &NewEvent::default()
                    .with_message(Some("Settled before it was due — never dispatched.".into())),
            )
            .await?;
            insert_instruct(&mut tx, target, control, caller).await?;
            tx.commit().await?;
            signals::task_saved(ctx, target, false).await;
            signals::task_event_created(ctx, event).await;
            continue;
        }
        sqlx::query(
            "UPDATE facade_task SET latest_instruct_kind = $2, interrupt_at = $3,
                    revision = revision + 1, updated_at = now()
              WHERE id = $1",
        )
        .bind(target)
        .bind(control.instruct_kind())
        .bind(interrupt_at)
        .execute(&mut *tx)
        .await?;
        let event = insert_event(
            &mut *tx,
            target,
            control.inging_kind(),
            &NewEvent::default(),
        )
        .await?;
        insert_instruct(&mut tx, target, control, caller).await?;
        tx.commit().await?;
        signals::task_saved(ctx, target, false).await;
        signals::task_event_created(ctx, event).await;
        if !transport::broadcast(ctx, agent, control.message(target.to_string()), false).await {
            tracing::error!(
                task = target,
                agent,
                "could not deliver {} for task {target} to agent {agent}",
                control.instruct_kind()
            );
        }
    }
    Ok(task_id)
}

async fn insert_instruct(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    task: i64,
    control: Control,
    caller: Option<i64>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO facade_taskinstruct (task_id, kind, caller_id, created_at) VALUES ($1, $2, $3, now())",
    )
    .bind(task)
    .bind(control.instruct_kind())
    .bind(caller)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

/// Two-phase: CANCELLING now, CANCELLED when the agent confirms. Sent to the task only.
pub async fn cancel(ctx: &Context, task: &str, caller: Option<i64>) -> BackendResult<i64> {
    request_control(ctx, task, Control::Cancel, caller).await
}

/// Forceful: reaches every still-running descendant; each confirms on its own.
pub async fn interrupt(ctx: &Context, task: &str, caller: Option<i64>) -> BackendResult<i64> {
    request_control(ctx, task, Control::Interrupt, caller).await
}

pub async fn pause(ctx: &Context, task: &str, caller: Option<i64>) -> BackendResult<i64> {
    request_control(ctx, task, Control::Pause, caller).await
}

pub async fn resume(
    ctx: &Context,
    task: &str,
    step: bool,
    caller: Option<i64>,
) -> BackendResult<i64> {
    request_control(ctx, task, Control::Resume { step }, caller).await
}

// ---------------------------------------------------------------------------------------------
// agents
// ---------------------------------------------------------------------------------------------

/// An agent inside the requesting organization, or refused as if missing (`_agent_in_org`,
/// `scoped_get`): these ops are destructive to a running deployment.
async fn agent_in_org(ctx: &Context, organization: i64, agent: &str) -> BackendResult<i64> {
    let id = parse_id(agent)?;
    sqlx::query_scalar("SELECT id FROM facade_agent WHERE id = $1 AND organization_id = $2")
        .bind(id)
        .bind(organization)
        .fetch_optional(&ctx.db)
        .await?
        .ok_or_else(|| BackendError::Forbidden(format!("No Agent {agent} in this organization.")))
}

/// Tell the agent to reconnect (`bounce`).
pub async fn bounce(ctx: &Context, organization: i64, agent: &str) -> BackendResult<i64> {
    let agent = agent_in_org(ctx, organization, agent).await?;
    transport::broadcast(ctx, agent, ToAgent::Bounce { duration: None }, false).await;
    Ok(agent)
}

/// Block the agent and kick it (`block`); a blocked agent's `REGISTER` is refused.
pub async fn block(
    ctx: &Context,
    organization: i64,
    agent: &str,
    reason: Option<String>,
) -> BackendResult<i64> {
    let agent = agent_in_org(ctx, organization, agent).await?;
    set_blocked(ctx, agent, true).await?;
    transport::broadcast(ctx, agent, ToAgent::Kick { reason }, false).await;
    Ok(agent)
}

/// Unblock the agent (`unblock`).
pub async fn unblock(ctx: &Context, organization: i64, agent: &str) -> BackendResult<i64> {
    let agent = agent_in_org(ctx, organization, agent).await?;
    set_blocked(ctx, agent, false).await?;
    Ok(agent)
}

async fn set_blocked(ctx: &Context, agent: i64, blocked: bool) -> BackendResult<()> {
    sqlx::query("UPDATE facade_agent SET blocked = $2 WHERE id = $1")
        .bind(agent)
        .bind(blocked)
        .execute(&ctx.db)
        .await?;
    signals::agent_saved(ctx, agent, false).await;
    Ok(())
}

/// Tell the agent to disconnect and stay away (`kick`).
pub async fn kick(ctx: &Context, organization: i64, agent: &str) -> BackendResult<i64> {
    let agent = agent_in_org(ctx, organization, agent).await?;
    transport::broadcast(ctx, agent, ToAgent::Kick { reason: None }, false).await;
    Ok(agent)
}

/// Tell each holding agent to drop the named drawers (`collect`). A drawer is named by its pk
/// or its resource id (`registration.resolve_drawers`), only the organization's are resolved,
/// and each agent is told the reference it uses: the id it minted, else the pk
/// (`registration.collect_reference`).
pub async fn collect(
    ctx: &Context,
    organization: i64,
    drawers: &[String],
) -> BackendResult<Vec<String>> {
    let numeric: Vec<i64> = drawers.iter().filter_map(|d| d.parse().ok()).collect();
    let found: Vec<(i64, i64, Option<String>, bool)> = sqlx::query_as(
        "SELECT s.agent_id, d.id, d.resource_id, d.agent_minted
           FROM facade_memorydrawer d JOIN facade_memoryshelve s ON s.id = d.shelve_id
          WHERE s.organization_id = $1 AND (d.resource_id = ANY($2) OR d.id = ANY($3))
          ORDER BY s.agent_id, d.id",
    )
    .bind(organization)
    .bind(drawers)
    .bind(&numeric)
    .fetch_all(&ctx.db)
    .await?;
    let mut by_agent: std::collections::BTreeMap<i64, std::collections::BTreeSet<String>> =
        Default::default();
    for (agent, id, resource_id, agent_minted) in found {
        let reference = match resource_id.filter(|r| agent_minted && !r.is_empty()) {
            Some(resource_id) => resource_id,
            None => id.to_string(),
        };
        by_agent.entry(agent).or_default().insert(reference);
    }
    for (agent, references) in by_agent {
        tracing::debug!(agent, "collecting {} drawer(s)", references.len());
        transport::broadcast(
            ctx,
            agent,
            ToAgent::Collect {
                drawers: references.into_iter().collect(),
            },
            false,
        )
        .await;
    }
    Ok(drawers.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn acted_on_names_structure_args() {
        let ports = json!([
            {"key": "image", "kind": "STRUCTURE", "identifier": "@mikro/image"},
            {"key": "label", "kind": "STRUCTURE", "identifier": "@mikro/label"},
            {"key": "n", "kind": "INT"},
        ]);
        let args =
            json!({"image": {"__identifier": "@mikro/image", "object": 5}, "label": "7", "n": 1});
        assert_eq!(
            acted_on_from_args(args.as_object().unwrap(), &ports),
            vec!["@mikro/image:5", "@mikro/label:7"]
        );
    }

    #[test]
    fn a_call_key_must_name_the_same_call() {
        let existing = ExistingRow {
            id: 1,
            reference: "r".into(),
            call_key: Some("k".into()),
            action_id: 3,
            implementation_id: Some(4),
            action_hash: "h".into(),
            dependency: None,
            dependency_method: None,
        };
        let same = AssignInput {
            call_key: Some("k".into()),
            parent: Some("9".into()),
            action_hash: Some("h".into()),
            ..AssignInput::default()
        };
        assert!(check_it_is_the_same_call(&existing, &same).is_ok());
        let other = AssignInput {
            action_hash: Some("other".into()),
            ..same
        };
        assert_eq!(
            check_it_is_the_same_call(&existing, &other).unwrap_err().to_string(),
            "Nondeterministic workflow: call 'k' of task 9 was made to action 3 before, and names something else now."
        );
    }
}

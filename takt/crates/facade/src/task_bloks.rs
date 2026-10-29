//! Bloks a task shows a person: with its own hold (`HELD` with a `blok`, an ask) or on its own
//! (`INFORM`). Each is made for the task alone: a materialization of scope `TASK`, owned by the
//! task, never listed, gone with the task.
//!
//! The blok is named by exactly one of
//! - `key`: one of the asks or informs the task's implementation declares
//!   (`ImplementationInteraction` of that role, made at registration);
//! - `id`: any blok of the organization;
//! - `source`: BSX, parsed and checked as a declared blok is (`BlokImplementationInputModel`,
//!   then its catalog, whose findings are stored on it and refuse nothing), and kept as a blok
//!   the task owns. It always has the dependency `self`.
//!
//! The last two are made at run time: shown only when the implementation declares it may
//! (`dynamic_asks`, `dynamic_informs`) and the agent's token grants the matching scope.
//!
//! Its dependencies are bound as `agent_mappings` says, and any it leaves out that the sending
//! agent satisfies to that agent. What cannot be made is refused with the reason, for the agent
//! (`BLOK_REFUSED`): an LLM writing BSX can read it and send a better one.

use serde_json::{json, Map, Value};
use sqlx::PgConnection;

use rekuest_core::inputs::{ArgPortInputModel, BlokImplementationInputModel, ReturnPortInputModel};
use rekuest_core::values::validate_assignment_args;

use crate::catalog_validation::{
    dump_diagnostics, module_unknown, validate_blok_against_catalog, Catalog, Diagnostic,
};
use crate::messages::TaskBlok;
use crate::mutations::blok::sync_blok_dependencies;
use crate::registration::{stored_components, stored_demo, stored_list};

/// Why a task's blok shows: the task holds until a person answers it, or carries on.
pub const ASK: &str = "ASK";
pub const INFORM: &str = "INFORM";

/// The dependency every BSX blok a task sends has: the agent that sent it.
const SELF_DEPENDENCY: &str = "self";

/// The catalog a BSX blok naming none renders against (as registration's).
const STANDARD_CATALOG_NAME: &str = "orkestrator";

/// The kind of a task's blok in the vocabulary's root matrix: it reads `self` (the sending
/// agent), `deps` (its dependencies, mapped), `args` and `task`.
const TASK_KIND: &str = "task";

#[derive(Debug, thiserror::Error)]
pub enum TaskBlokError {
    /// The blok cannot be made as sent; the reason goes back to the agent.
    #[error("{0}")]
    Refused(String),
    #[error(transparent)]
    Db(#[from] sqlx::Error),
}

fn refused(reason: impl Into<String>) -> TaskBlokError {
    TaskBlokError::Refused(reason.into())
}

#[derive(Debug, sqlx::FromRow)]
struct Sender {
    id: i64,
    user_id: i64,
    organization_id: i64,
    app_identifier: String,
    version: String,
}

#[derive(Debug, sqlx::FromRow)]
struct Dependency {
    id: i64,
    key: String,
    app_filter: Option<String>,
    version_filter: Option<String>,
    optional: bool,
}

/// Whether an agent of `app` at `version` satisfies a dependency's filters.
fn satisfies(dependency: &Dependency, app: &str, version: &str) -> bool {
    dependency
        .app_filter
        .as_deref()
        .is_none_or(|f| f.is_empty() || f == app)
        && dependency
            .version_filter
            .as_deref()
            .is_none_or(|f| f.is_empty() || f == "*" || f == version)
}

async fn agent_of(conn: &mut PgConnection, agent: i64) -> Result<Option<Sender>, sqlx::Error> {
    sqlx::query_as(
        "SELECT a.id, a.user_id, a.organization_id, app.identifier AS app_identifier, r.version
           FROM facade_agent a
           JOIN authentikate_app app ON app.id = a.app_id
           JOIN authentikate_release r ON r.id = a.release_id
          WHERE a.id = $1",
    )
    .bind(agent)
    .fetch_optional(conn)
    .await
}

/// A blok made from `source` for `task`: parsed, checked against its catalog, kept as a blok
/// the task owns. Its id and returns.
async fn blok_from_source(
    conn: &mut PgConnection,
    sender: &Sender,
    task: i64,
    role: &str,
    source: &str,
    sent: &TaskBlok,
) -> Result<(i64, Vec<ReturnPortInputModel>), TaskBlokError> {
    let mut dependencies = sent.dependencies.clone().unwrap_or_default();
    if !dependencies
        .iter()
        .any(|d| d.get("key").and_then(Value::as_str) == Some(SELF_DEPENDENCY))
    {
        dependencies.push(
            json!({"key": SELF_DEPENDENCY, "description": "The agent that showed this blok."}),
        );
    }
    let name = sent
        .name
        .clone()
        .filter(|n| !n.is_empty())
        .unwrap_or_else(|| format!("Task {task} {}", role.to_lowercase()));
    let mut model: BlokImplementationInputModel = serde_json::from_value(json!({
        "key": name,
        "vocab": sent.vocab.unwrap_or(2),
        "source": source,
        "dependencies": dependencies,
        "returns": sent.returns.clone().unwrap_or_default(),
        "catalogs": sent.catalogs.clone().unwrap_or_default(),
        "module": sent.module,
        "locals": sent.locals.clone().unwrap_or_default(),
        "demo": sent.demo,
        "queries": sent.queries.clone().unwrap_or_default(),
        "subscriptions": sent.subscriptions.clone().unwrap_or_default(),
    }))
    .map_err(|e| refused(format!("The blok is not a blok: {e}")))?;
    model
        .validate_as(TASK_KIND)
        .map_err(|e| refused(format!("The blok does not hold: {e}")))?;

    let catalog_name = model
        .module
        .as_deref()
        .filter(|c| !c.is_empty())
        .unwrap_or(STANDARD_CATALOG_NAME);
    let catalog = Catalog::get_or_create(conn, catalog_name, sender.organization_id)
        .await
        .map_err(refused)?;
    let checked = validate_blok_against_catalog(
        &catalog,
        model.components.as_deref(),
        &model.scope(TASK_KIND),
    )
    .map_err(|e| {
        refused(format!(
            "The blok does not fit the catalog {catalog_name:?}: {e}"
        ))
    })?;
    // Stored as sent, its locals as resolved beside (`resolved_locals`).
    let mut diagnostics: Vec<Diagnostic> = module_unknown(conn, &catalog, sender.organization_id)
        .await?
        .into_iter()
        .collect();
    diagnostics.extend(checked.diagnostics);

    let id: i64 = sqlx::query_scalar(
        "INSERT INTO facade_blok (name, components, diagnostics, creator_id, organization_id, module_id,
                                  context, args, returns, owner_task_id, vocab, catalogs, locals, resolved_locals,
                                  demo, queries, subscriptions)
         VALUES ($1, $2, $3, $4, $5, $6, $7, '[]'::jsonb, $8, $9, $10, $11, $12, $13, $14, $15, $16)
         RETURNING id",
    )
    .bind(&model.key)
    .bind(stored_components(model.components.as_deref()))
    .bind(dump_diagnostics(&diagnostics))
    .bind(sender.user_id)
    .bind(sender.organization_id)
    .bind(catalog.id)
    .bind(model.context.value())
    .bind(serde_json::to_value(&model.returns).expect("returns serialize"))
    .bind(task)
    .bind(model.vocab as i16)
    .bind(serde_json::to_value(&model.catalogs).expect("blok catalogs serialize"))
    .bind(stored_list(&model.locals))
    .bind(stored_list(&checked.scope.locals))
    .bind(stored_demo(model.demo.as_ref()))
    .bind(stored_list(&model.queries))
    .bind(stored_list(&model.subscriptions))
    .fetch_one(&mut *conn)
    .await?;
    sync_blok_dependencies(conn, id, &model.dependencies, true).await?;
    Ok((id, model.returns))
}

/// Make the blok `sent` shows for `task` (`role` is [`ASK`] or [`INFORM`]): its materialization,
/// bound and opened on its args. Run it in the transaction that records the hold or the inform,
/// so a refusal leaves nothing behind.
pub async fn make_task_blok(
    conn: &mut PgConnection,
    agent: i64,
    task: i64,
    role: &str,
    sent: &TaskBlok,
) -> Result<i64, TaskBlokError> {
    let Some(sender) = agent_of(conn, agent).await? else {
        return Err(refused(format!("No agent {agent}.")));
    };
    if sent.key.is_none() {
        allowed_at_run_time(conn, agent, task, role).await?;
    }
    let blok = match (
        sent.key.as_deref(),
        sent.id.as_deref(),
        sent.source.as_deref(),
    ) {
        // A key names one of the running action's declared asks or informs, of this role.
        (Some(key), None, None) => sqlx::query_scalar(
            "SELECT ii.blok_id FROM facade_task t
               JOIN facade_implementationinteraction ii ON ii.implementation_id = t.implementation_id
              WHERE t.id = $1 AND ii.key = $2 AND ii.role = $3",
        )
        .bind(task)
        .bind(key)
        .bind(role)
        .fetch_optional(&mut *conn)
        .await?
        .ok_or_else(|| {
            refused(format!(
                "This action declares no {} {key:?}.",
                role.to_lowercase()
            ))
        })?,
        (None, Some(id), None) => {
            let id: i64 = id
                .parse()
                .map_err(|_| refused(format!("No blok {id:?}.")))?;
            sqlx::query_scalar(
                "SELECT id FROM facade_blok WHERE id = $1 AND organization_id = $2 AND owner_task_id IS NULL",
            )
            .bind(id)
            .bind(sender.organization_id)
            .fetch_optional(&mut *conn)
            .await?
            .ok_or_else(|| refused(format!("No blok {id} in this organization.")))?
        }
        (None, None, Some(source)) => {
            blok_from_source(conn, &sender, task, role, source, sent)
                .await?
                .0
        }
        _ => {
            return Err(refused(
                "A task's blok is named by exactly one of key, id or source.",
            ))
        }
    };

    let (name, ports): (String, Value) =
        sqlx::query_as("SELECT name, args FROM facade_blok WHERE id = $1")
            .bind(blok)
            .fetch_one(&mut *conn)
            .await?;
    let args = sent.args.clone().unwrap_or_default();
    let ports: Vec<ArgPortInputModel> = serde_json::from_value(ports).unwrap_or_default();
    if !ports.is_empty() {
        validate_assignment_args(&ports, &args)
            .map_err(|e| refused(format!("The blok's args do not fit: {e}")))?;
    }

    let bindings = bind_dependencies(conn, &sender, blok, sent.agent_mappings.as_ref()).await?;

    let materialized: i64 = sqlx::query_scalar(
        "INSERT INTO facade_materializedblok (name, description, blok_id, scope, owner_task_id, task_role, args)
         VALUES ($1, '', $2, 'TASK', $3, $4, $5)
         RETURNING id",
    )
    .bind(sent.name.as_deref().filter(|n| !n.is_empty()).unwrap_or(&name))
    .bind(blok)
    .bind(task)
    .bind(role)
    .bind(Value::Object(args))
    .fetch_one(&mut *conn)
    .await?;
    for (dependency, key, bound) in bindings {
        sqlx::query(
            "INSERT INTO facade_blokagentmapping (key, agent_id, dependency_id, materialized_blok_id) VALUES ($1, $2, $3, $4)",
        )
        .bind(&key)
        .bind(bound)
        .bind(dependency)
        .bind(materialized)
        .execute(&mut *conn)
        .await?;
    }
    Ok(materialized)
}

/// A blok made at run time (`source`) or any of the organization (`id`): shown only when the
/// task's implementation declares it may (`dynamic_asks` for an ask, `dynamic_informs` for an
/// inform) and the agent's token grants the scope that goes with it.
async fn allowed_at_run_time(
    conn: &mut PgConnection,
    agent: i64,
    task: i64,
    role: &str,
) -> Result<(), TaskBlokError> {
    let (flag, scope, what) = if role == ASK {
        (
            "dynamic_asks",
            "rekuest_dynamic_ask",
            "asks through a blok made at ask time",
        )
    } else {
        (
            "dynamic_informs",
            "rekuest_dynamic_inform",
            "shows a blok made at inform time",
        )
    };
    let declared: Option<bool> = sqlx::query_scalar(&format!(
        "SELECT i.{flag} FROM facade_task t JOIN facade_implementation i ON i.id = t.implementation_id WHERE t.id = $1"
    ))
    .bind(task)
    .fetch_optional(&mut *conn)
    .await?;
    if declared != Some(true) {
        return Err(refused(format!(
            "This task {what}, which its action does not declare ({flag}): declare the blok with the action, or set {flag}."
        )));
    }
    if !crate::registration::agent_granted(&mut *conn, agent, scope).await? {
        return Err(refused(format!(
            "This task {what}, which this agent's token does not grant: log the app in with the {scope} scope."
        )));
    }
    Ok(())
}

/// Each dependency of `blok` with the agent it is bound to: the one `mappings` names (of the
/// organization, satisfying it), else the sender when it satisfies it. An optional one nobody
/// satisfies stays unbound; a required one refuses.
async fn bind_dependencies(
    conn: &mut PgConnection,
    sender: &Sender,
    blok: i64,
    mappings: Option<&Map<String, Value>>,
) -> Result<Vec<(i64, String, i64)>, TaskBlokError> {
    let dependencies: Vec<Dependency> = sqlx::query_as(
        "SELECT id, key, app_filter, version_filter, optional FROM facade_blokdependency WHERE blok_id = $1",
    )
    .bind(blok)
    .fetch_all(&mut *conn)
    .await?;
    if let Some(unknown) = mappings
        .into_iter()
        .flat_map(|m| m.keys())
        .find(|key| !dependencies.iter().any(|d| &&d.key == key))
    {
        return Err(refused(format!(
            "The blok declares no dependency {unknown:?}."
        )));
    }
    let mut bound = vec![];
    for dependency in dependencies {
        let mapped = mappings.and_then(|m| m.get(&dependency.key));
        if let Some(mapped) = mapped {
            let id = mapped
                .as_str()
                .map(str::to_owned)
                .or_else(|| mapped.as_i64().map(|i| i.to_string()))
                .and_then(|s| s.parse::<i64>().ok())
                .ok_or_else(|| {
                    refused(format!(
                        "Dependency {:?} is mapped to {mapped}, not an agent id.",
                        dependency.key
                    ))
                })?;
            let agent = agent_of(conn, id)
                .await?
                .filter(|a| a.organization_id == sender.organization_id)
                .ok_or_else(|| refused(format!("No agent {id} in this organization.")))?;
            if !satisfies(&dependency, &agent.app_identifier, &agent.version) {
                return Err(refused(format!(
                    "Agent {id} ({} {}) does not satisfy dependency {:?}.",
                    agent.app_identifier, agent.version, dependency.key
                )));
            }
            bound.push((dependency.id, dependency.key, agent.id));
        } else if satisfies(&dependency, &sender.app_identifier, &sender.version) {
            bound.push((dependency.id, dependency.key, sender.id));
        } else if !dependency.optional {
            return Err(refused(format!(
                "Dependency {:?} is required, and this agent does not satisfy it: map it to one that does.",
                dependency.key
            )));
        }
    }
    Ok(bound)
}

/// The returns a person answers an ask through: those of the blok its materialization shows.
pub async fn returns_of(
    conn: &mut PgConnection,
    materialized: i64,
) -> Result<Vec<ReturnPortInputModel>, sqlx::Error> {
    let returns: Option<Value> = sqlx::query_scalar(
        "SELECT b.returns FROM facade_materializedblok m JOIN facade_blok b ON b.id = m.blok_id WHERE m.id = $1",
    )
    .bind(materialized)
    .fetch_optional(conn)
    .await?;
    Ok(returns
        .and_then(|r| serde_json::from_value(r).ok())
        .unwrap_or_default())
}

/// Forget a blok made for a hold that did not take (the task was no longer the agent's to hold).
pub async fn discard(conn: &mut PgConnection, materialized: i64) -> Result<(), sqlx::Error> {
    sqlx::query(
        "WITH gone AS (DELETE FROM facade_materializedblok WHERE id = $1 RETURNING blok_id)
         DELETE FROM facade_blok b USING gone WHERE b.id = gone.blok_id AND b.owner_task_id IS NOT NULL",
    )
    .bind(materialized)
    .execute(conn)
    .await?;
    Ok(())
}

/// Make the blok `sent` shows for `task` in a transaction of its own: its materialization's id,
/// or `None` when it was refused, after telling the agent why (`BLOK_REFUSED`).
pub async fn shown(
    ctx: &crate::context::Context,
    agent: i64,
    task: i64,
    role: &str,
    sent: &TaskBlok,
) -> crate::persist::PersistResult<Option<i64>> {
    let mut tx = ctx.db.begin().await?;
    match make_task_blok(&mut tx, agent, task, role, sent).await {
        Ok(materialized) => {
            tx.commit().await?;
            Ok(Some(materialized))
        }
        Err(TaskBlokError::Refused(reason)) => {
            tx.rollback().await?;
            tracing::info!(agent, task, role, "refused a task's blok: {reason}");
            crate::transport::broadcast(
                ctx,
                agent,
                crate::messages::ToAgent::BlokRefused {
                    task: task.to_string(),
                    reason,
                },
                false,
            )
            .await;
            Ok(None)
        }
        Err(TaskBlokError::Db(e)) => Err(e.into()),
    }
}

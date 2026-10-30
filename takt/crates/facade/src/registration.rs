//! An agent's identity and its declaration (`facade/registration.py`).

use sqlx::PgPool;

/// The agent for `(client, user, organization)`, created with its memory shelve if it did not
/// exist (`ensure_agent`). A new agent is named after its client and belongs to the client's
/// release (and its app); an existing agent is not touched.
pub async fn ensure_agent(
    db: &PgPool,
    client: i64,
    user: i64,
    organization: i64,
) -> Result<i64, sqlx::Error> {
    let (agent, name): (i64, String) = sqlx::query_as(
        "WITH client AS (
             SELECT c.client_id, c.release_id, r.app_id
               FROM authentikate_client c JOIN authentikate_release r ON r.id = c.release_id
              WHERE c.id = $1
         )
         INSERT INTO facade_agent
             (hash, name, kind, app_id, release_id, client_id, user_id, organization_id)
         SELECT '', client.client_id, 'WEBSOCKET', client.app_id, client.release_id, $1, $2, $3
           FROM client
         ON CONFLICT (client_id, user_id, organization_id) DO UPDATE SET name = facade_agent.name
         RETURNING id, name",
    )
    .bind(client)
    .bind(user)
    .bind(organization)
    .fetch_one(db)
    .await?;

    sqlx::query(
        "INSERT INTO facade_memoryshelve (name, description, agent_id, creator_id, organization_id)
         VALUES ($1, '', $2, $3, $4)
         ON CONFLICT (agent_id) DO NOTHING",
    )
    .bind(format!("{name} memory shelve"))
    .bind(agent)
    .bind(user)
    .bind(organization)
    .execute(db)
    .await?;
    Ok(agent)
}

/// Record the supervisor the agent stated on this registration (none clears it). A hint for
/// pickers, never authority; written only when it changed.
pub async fn set_supervisor(
    db: &PgPool,
    agent: i64,
    supervisor: Option<&str>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE facade_agent SET supervisor = $2 WHERE id = $1 AND supervisor IS DISTINCT FROM $2",
    )
    .bind(agent)
    .bind(supervisor)
    .execute(db)
    .await?;
    Ok(())
}

/// Hardware as the action query reads it: the GPU values are derived here, once.
pub fn stored_hardware(hardware: &crate::messages::AgentHardware) -> serde_json::Value {
    let mut stored = serde_json::to_value(hardware).expect("hardware serializes");
    stored["gpu_count"] = hardware.gpus.len().into();
    stored["gpu_memory_mib"] = hardware
        .gpus
        .iter()
        .filter_map(|gpu| gpu.memory_mib)
        .max()
        .into();
    stored
}

/// Record what the agent's machine has, as it said on this registration (none clears it).
pub async fn set_hardware(
    db: &PgPool,
    agent: i64,
    hardware: Option<&crate::messages::AgentHardware>,
) -> Result<(), sqlx::Error> {
    let stored = hardware.map(stored_hardware);
    sqlx::query(
        "UPDATE facade_agent SET hardware = $2 WHERE id = $1 AND hardware IS DISTINCT FROM $2",
    )
    .bind(agent)
    .bind(stored)
    .execute(db)
    .await?;
    Ok(())
}

/// Record what the agent said on this registration it implements beyond the base protocol (none
/// clears it): whether a task with `state_requires` may be sent to it. Written only when it
/// changed.
pub async fn set_features(db: &PgPool, agent: i64, features: &[String]) -> Result<(), sqlx::Error> {
    let features = (!features.is_empty()).then_some(sqlx::types::Json(features));
    sqlx::query(
        "UPDATE facade_agent SET features = $2 WHERE id = $1 AND features IS DISTINCT FROM $2",
    )
    .bind(agent)
    .bind(features)
    .execute(db)
    .await?;
    Ok(())
}

/// The scopes of the token the agent registers with: what it may do beyond what it declared
/// (`dynamic_asks`/`dynamic_calls`/`dynamic_informs` are refused without theirs).
pub async fn set_granted_scopes(
    db: &PgPool,
    agent: i64,
    scopes: &[&str],
) -> Result<(), sqlx::Error> {
    let scopes: Vec<&str> = scopes.iter().copied().filter(|s| !s.is_empty()).collect();
    sqlx::query(
        "UPDATE facade_agent SET granted_scopes = $2 WHERE id = $1 AND granted_scopes IS DISTINCT FROM $2",
    )
    .bind(agent)
    .bind(sqlx::types::Json(scopes))
    .execute(db)
    .await?;
    Ok(())
}

/// The scope that stands for every other one of this service.
pub const ALL_SCOPE: &str = "rekuest_all";

/// Whether the agent's token granted `scope` (or every scope, `rekuest_all`).
pub async fn agent_granted(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
    scope: &str,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT coalesce(granted_scopes ? $2 OR granted_scopes ? $3, false) FROM facade_agent WHERE id = $1",
    )
    .bind(agent)
    .bind(scope)
    .bind(ALL_SCOPE)
    .fetch_optional(executor)
    .await
    .map(|granted| granted.unwrap_or(false))
}

/// Forget every drawer on the agent's shelve: a process that just started holds nothing.
pub async fn clear_drawers(
    executor: impl sqlx::PgExecutor<'_>,
    agent: i64,
) -> Result<u64, sqlx::Error> {
    Ok(sqlx::query(
        "DELETE FROM facade_memorydrawer d USING facade_memoryshelve s
          WHERE d.shelve_id = s.id AND s.agent_id = $1",
    )
    .bind(agent)
    .execute(executor)
    .await?
    .rows_affected())
}

/// Record that `agent` holds `resource_id` (an `identifier`) in memory (`shelve`): upsert the
/// drawer on its shelve, keyed by `resource_id`. `agent_minted` (a numbered SHELVE) marks a
/// drawer the agent references by that id; a later plain upsert never unsets it. The drawer's id.
pub async fn shelve(
    db: &PgPool,
    agent: i64,
    identifier: &str,
    resource_id: &str,
    label: Option<&str>,
    description: Option<&str>,
    agent_minted: bool,
) -> Result<i64, sqlx::Error> {
    let shelve: i64 = sqlx::query_scalar(
        "INSERT INTO facade_memoryshelve (name, description, agent_id, creator_id, organization_id)
         SELECT a.name || ' memory shelve', '', a.id, a.user_id, a.organization_id
           FROM facade_agent a WHERE a.id = $1
         ON CONFLICT (agent_id) DO UPDATE SET agent_id = EXCLUDED.agent_id
         RETURNING id",
    )
    .bind(agent)
    .fetch_one(db)
    .await?;
    sqlx::query_scalar(
        "INSERT INTO facade_memorydrawer (shelve_id, resource_id, identifier, label, description, agent_minted)
         VALUES ($1, $2, $3, $4, $5, $6)
         ON CONFLICT (shelve_id, resource_id) WHERE resource_id IS NOT NULL
         DO UPDATE SET identifier = EXCLUDED.identifier, label = EXCLUDED.label, description = EXCLUDED.description,
                       agent_minted = facade_memorydrawer.agent_minted OR EXCLUDED.agent_minted
         RETURNING id",
    )
    .bind(shelve)
    .bind(resource_id)
    .bind(identifier)
    .bind(label)
    .bind(description)
    .bind(agent_minted)
    .fetch_one(db)
    .await
}

/// Why an unshelve was refused.
#[derive(Debug, thiserror::Error)]
pub enum UnshelveError {
    #[error("Unknown drawer {0:?}")]
    Unknown(String),
    #[error("This drawer does not belong to this agent.")]
    Foreign,
    #[error("database: {0}")]
    Database(#[from] sqlx::Error),
}

/// Drop the drawer `drawer` from `agent`'s shelve (`unshelve`). With `by_resource_id` (a
/// numbered UNSHELVE) it is first looked up as a resource id on the agent's own shelve, then
/// as a primary key.
pub async fn unshelve(
    db: &PgPool,
    agent: i64,
    drawer: &str,
    by_resource_id: bool,
) -> Result<(), UnshelveError> {
    if by_resource_id {
        let own = sqlx::query(
            "DELETE FROM facade_memorydrawer d USING facade_memoryshelve s
              WHERE d.shelve_id = s.id AND s.agent_id = $1 AND d.id = (
                    SELECT d2.id FROM facade_memorydrawer d2 JOIN facade_memoryshelve s2 ON s2.id = d2.shelve_id
                     WHERE s2.agent_id = $1 AND d2.resource_id = $2 ORDER BY d2.id LIMIT 1)",
        )
        .bind(agent)
        .bind(drawer)
        .execute(db)
        .await?
        .rows_affected();
        if own > 0 {
            return Ok(());
        }
    }
    let Some(id) = drawer
        .parse::<i64>()
        .ok()
        .filter(|_| drawer.chars().all(|c| c.is_ascii_digit()))
    else {
        return Err(UnshelveError::Unknown(drawer.to_owned()));
    };
    let owner: Option<i64> = sqlx::query_scalar(
        "SELECT s.agent_id FROM facade_memorydrawer d JOIN facade_memoryshelve s ON s.id = d.shelve_id WHERE d.id = $1",
    )
    .bind(id)
    .fetch_optional(db)
    .await?;
    match owner {
        None => Err(UnshelveError::Unknown(drawer.to_owned())),
        Some(owner) if owner != agent => Err(UnshelveError::Foreign),
        Some(_) => {
            sqlx::query("DELETE FROM facade_memorydrawer WHERE id = $1")
                .bind(id)
                .execute(db)
                .await?;
            Ok(())
        }
    }
}

use std::collections::HashMap;

use rekuest_core::blok::infer::ArgPorts;
use rekuest_core::inputs::{
    blok_kind, AgentDependencyInputModel, AskBlokInputModel, BlokDemoInputModel,
    BlokImplementationInputModel, ComponentNodeInputModel, DefinitionInputModel,
    ImplementAgentInputModel, InformBlokInputModel, StateImplementationInputModel,
};
use sqlx::PgConnection;

use crate::catalog_validation::{
    dump_diagnostics, module_unknown, validate_blok_against_catalog, validate_blok_with_arg_ports,
    Catalog, Diagnostic,
};
use crate::deletion::{
    delete_implementations, delete_materialized_bloks, delete_states, gc_action_bloks,
    gc_provided_bloks,
};
use crate::mutations::blok::sync_blok_dependencies;
use crate::mutations::implementation::{
    check_blok_arg_descriptor_keys, create_implementation, create_ports_level_by_level,
    AgentIdentity, Prefetch,
};
use crate::mutations::Refusal;
use crate::registration_lock::lock_organization;
use crate::signals::{OnCommit, Signal};
use crate::unique::{
    hash_action_blok, hash_blok_definition, hash_interaction_blok, hash_state_definition,
};

/// What a registration did: the diagnostics to hand back on `INIT`, the signals to publish
/// once it committed, and the hash the agent now holds.
#[derive(Debug)]
pub struct Implemented {
    pub hash: String,
    pub diagnostics: Vec<Diagnostic>,
    pub on_commit: OnCommit,
}

/// Upsert one of the agent's states: `key` defaults to the interface, `app_identifier` to the
/// agent's app (`_register_state`).
async fn register_state(
    conn: &mut PgConnection,
    agent: &AgentIdentity,
    app_identifier: &str,
    state: &StateImplementationInputModel,
) -> Result<i64, sqlx::Error> {
    let definition: i64 = sqlx::query_scalar(
        "INSERT INTO facade_statedefinition (name, hash, ports, description, organization_id)
         VALUES ($1, $2, $3, 'A state definition', $4)
         ON CONFLICT (organization_id, hash) DO UPDATE SET name = excluded.name, ports = excluded.ports,
                                                           description = excluded.description
         RETURNING id",
    )
    .bind(&state.definition.name)
    .bind(hash_state_definition(&state.definition))
    .bind(serde_json::to_value(&state.definition.ports).expect("ports serialize"))
    .bind(agent.organization)
    .fetch_one(&mut *conn)
    .await?;
    sqlx::query_scalar(
        "INSERT INTO facade_state (interface, key, app_identifier, agent_id, definition_id)
         VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT (interface, agent_id) DO UPDATE SET definition_id = excluded.definition_id, key = excluded.key,
                                                        app_identifier = excluded.app_identifier, updated_at = now()
         RETURNING id",
    )
    .bind(&state.interface)
    .bind(state.key.as_deref().filter(|k| !k.is_empty()).unwrap_or(&state.interface))
    .bind(state.app.as_deref().filter(|a| !a.is_empty()).unwrap_or(app_identifier))
    .bind(agent.id)
    .bind(definition)
    .fetch_one(conn)
    .await
}

/// The catalog a blok naming none is checked against: the one the Orkestrator app registers.
/// The client's offline check and Django's `createBlok` use the same name.
const STANDARD_CATALOG_NAME: &str = "orkestrator";

/// The fields whose value is a JSON literal of the author's: their nulls are data.
const LITERAL_FIELDS: [&str; 3] = ["static_value", "value_literal", "default"];

/// A model's dump without the fields it leaves unset, as Django stores a blok's parts
/// (pydantic's `exclude_none`): a null field goes, a null inside a literal stays.
pub fn without_nulls(value: serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) => serde_json::Value::Object(
            map.into_iter()
                .filter(|(_, v)| !v.is_null())
                .map(|(k, v)| {
                    if LITERAL_FIELDS.contains(&k.as_str()) {
                        (k, v)
                    } else {
                        (k, without_nulls(v))
                    }
                })
                .collect(),
        ),
        serde_json::Value::Array(items) => {
            serde_json::Value::Array(items.into_iter().map(without_nulls).collect())
        }
        other => other,
    }
}

/// A blok's tree as stored: one root, without its nulls.
pub fn stored_components(components: Option<&[ComponentNodeInputModel]>) -> serde_json::Value {
    without_nulls(
        serde_json::to_value(components.unwrap_or_default()).expect("components serialize"),
    )
}

/// A blok's locals, queries or subscriptions as stored.
pub fn stored_list<T: serde::Serialize>(items: &[T]) -> serde_json::Value {
    without_nulls(serde_json::to_value(items).expect("blok declarations serialize"))
}

/// A blok's demo as stored: `{}` when it has none.
pub fn stored_demo(demo: Option<&BlokDemoInputModel>) -> serde_json::Value {
    let Some(serde_json::Value::Object(fields)) =
        demo.map(|demo| serde_json::to_value(demo).expect("blok demo serializes"))
    else {
        return serde_json::json!({});
    };
    // Every field of a demo is the author's JSON: only an unset one goes.
    serde_json::Value::Object(fields.into_iter().filter(|(_, v)| !v.is_null()).collect())
}

/// One declared blok: its module, manifest check, the app's template for this content (shared
/// by every agent of the app declaring the same, `hash_blok_definition`), and the agent's slot
/// for the blok's key re-pointed at it, with a mapping per dependency. Its diagnostics.
async fn register_blok(
    conn: &mut PgConnection,
    agent: &AgentIdentity,
    blok: &BlokImplementationInputModel,
    self_arg_ports: &ArgPorts,
) -> Result<Vec<Diagnostic>, Refusal> {
    let catalog_name = blok
        .module
        .as_deref()
        .filter(|c| !c.is_empty())
        .unwrap_or(STANDARD_CATALOG_NAME);
    let catalog = Catalog::get_or_create(conn, catalog_name, agent.organization).await?;
    let checked = validate_blok_with_arg_ports(
        &catalog,
        blok.components.as_deref(),
        &blok.scope(blok_kind(blok.context)),
        self_arg_ports,
    )?;
    // What is hashed and stored is the blok as sent; its locals as resolved are stored beside
    // (`resolved_locals`), recomputed on every registration.
    let mut diagnostics: Vec<Diagnostic> = module_unknown(conn, &catalog, agent.organization)
        .await?
        .into_iter()
        .collect();
    diagnostics.extend(checked.diagnostics);
    check_blok_arg_descriptor_keys(conn, &blok.key, &blok.args).await?;

    // Same content, same row: only the findings (which depend on the catalog as it is now) are
    // rewritten. A user's blok of the same name is custom and never matches.
    // `xmax = 0` only on a row this statement inserted: an existing template already has its port rows.
    let (id, name, description, inserted): (i64, String, Option<String>, bool) = sqlx::query_as(
        "INSERT INTO facade_blok (name, description, components, diagnostics, creator_id, organization_id, module_id,
                                  context, for_model, for_node, app_id, declaration_hash, args, returns,
                                  vocab, catalogs, locals, resolved_locals, demo, queries, subscriptions)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21)
         ON CONFLICT (organization_id, app_id, name, declaration_hash) WHERE app_id IS NOT NULL DO UPDATE SET
             diagnostics = excluded.diagnostics, resolved_locals = excluded.resolved_locals
         RETURNING id, name, description, (xmax = 0)",
    )
    .bind(&blok.key)
    .bind(&blok.description)
    .bind(stored_components(blok.components.as_deref()))
    .bind(dump_diagnostics(&diagnostics))
    .bind(agent.user)
    .bind(agent.organization)
    .bind(catalog.id)
    .bind(blok.context.value())
    .bind(&blok.for_model)
    .bind(&blok.for_node)
    .bind(agent.app)
    .bind(hash_blok_definition(blok))
    .bind(serde_json::to_value(&blok.args).expect("blok args serialize"))
    .bind(serde_json::to_value(&blok.returns).expect("blok returns serialize"))
    .bind(blok.vocab as i16)
    .bind(serde_json::to_value(&blok.catalogs).expect("blok catalogs serialize"))
    .bind(stored_list(&blok.locals))
    .bind(stored_list(&checked.scope.locals))
    .bind(stored_demo(blok.demo.as_ref()))
    .bind(stored_list(&blok.queries))
    .bind(stored_list(&blok.subscriptions))
    .fetch_one(&mut *conn)
    .await?;
    if inserted {
        // The template's content is its hash, args included: its rows never change after this.
        create_ports_level_by_level(conn, &blok.args, "blok_id", id, "facade_blokargport").await?;
    }
    let dependencies = sync_blok_dependencies(conn, id, &blok.dependencies, true).await?;

    if blok.default {
        // At most one default per agent and context: a newly declared default replaces the previous one.
        sqlx::query(
            "UPDATE facade_materializedblok m SET is_default = false
               FROM facade_blok b
              WHERE b.id = m.blok_id AND m.declared_by_id = $1 AND m.is_default AND b.context = $2 AND m.name <> $3",
        )
        .bind(agent.id)
        .bind(blok.context.value())
        .bind(&name)
        .execute(&mut *conn)
        .await?;
    }

    // The agent's slot for this key keeps its id across content changes, so what was placed
    // on it stays; the template it held is collected by the caller once nothing holds it.
    let materialized: i64 = sqlx::query_scalar(
        "INSERT INTO facade_materializedblok (name, description, blok_id, declared_by_id, is_default)
         VALUES ($3, $4, $1, $2, $5)
         ON CONFLICT (declared_by_id, name) WHERE declared_by_id IS NOT NULL DO UPDATE SET
             blok_id = excluded.blok_id, description = excluded.description, is_default = excluded.is_default,
             updated_at = now()
         RETURNING id",
    )
    .bind(id)
    .bind(agent.id)
    .bind(&name)
    .bind(description.as_deref().unwrap_or(""))
    .bind(blok.default)
    .fetch_one(&mut *conn)
    .await?;

    let keys: Vec<&str> = dependencies.iter().map(|(_, key)| key.as_str()).collect();
    sqlx::query("DELETE FROM facade_blokagentmapping WHERE materialized_blok_id = $1 AND NOT (key = ANY($2))")
        .bind(materialized)
        .bind(&keys)
        .execute(&mut *conn)
        .await?;
    for (dependency, key) in dependencies {
        sqlx::query(
            "INSERT INTO facade_blokagentmapping (key, agent_id, dependency_id, materialized_blok_id)
             VALUES ($1, $2, $3, $4)
             ON CONFLICT (materialized_blok_id, key) DO UPDATE SET dependency_id = excluded.dependency_id,
                                                                  agent_id = excluded.agent_id, updated_at = now()",
        )
        .bind(&key)
        .bind(agent.id)
        .bind(dependency)
        .bind(materialized)
        .execute(&mut *conn)
        .await?;
    }
    Ok(diagnostics)
}

/// An action's own bloks, as its definition declares them now (`ActionBlok`): each checked
/// against its catalog, written as a blok of the agent's app held by actions only
/// (`held_by_action`), content-addressed like an agent's template and shared by every action of
/// the app declaring the same, and bound to the action for its context. A context the definition
/// no longer has is let go, and a blok no action holds any more is collected. Their diagnostics.
pub async fn sync_action_bloks(
    conn: &mut PgConnection,
    agent: &AgentIdentity,
    action: i64,
    definition: &DefinitionInputModel,
    dependencies: &[AgentDependencyInputModel],
) -> Result<Vec<Diagnostic>, Refusal> {
    let arg_keys: Vec<String> = definition.args.iter().map(|a| a.key.clone()).collect();
    let held: Vec<i64> =
        sqlx::query_scalar("SELECT blok_id FROM facade_actionblok WHERE action_id = $1")
            .bind(action)
            .fetch_all(&mut *conn)
            .await?;
    let mut diagnostics = vec![];
    let mut contexts: Vec<&str> = vec![];
    for blok in &definition.bloks {
        let catalog_name = blok
            .module
            .as_deref()
            .filter(|c| !c.is_empty())
            .unwrap_or(STANDARD_CATALOG_NAME);
        let catalog = Catalog::get_or_create(conn, catalog_name, agent.organization).await?;
        let checked = validate_blok_against_catalog(
            &catalog,
            blok.components.as_deref(),
            &blok.scope(dependencies, Some(arg_keys.clone())),
        )?;
        let mut found: Vec<Diagnostic> = module_unknown(conn, &catalog, agent.organization)
            .await?
            .into_iter()
            .collect();
        found.extend(checked.diagnostics);
        // Its name only says whose it is: an action's blok is never listed or looked up by it.
        let name = format!("{}:{}", definition.key, blok.context.value().to_lowercase());
        let id: i64 = sqlx::query_scalar(
            "INSERT INTO facade_blok (name, description, components, diagnostics, creator_id, organization_id,
                                      module_id, context, app_id, declaration_hash, held_by_action,
                                      vocab, catalogs, locals, resolved_locals, demo, queries, subscriptions)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, true, $11, $12, $13, $14, $15, $16, $17)
             ON CONFLICT (organization_id, app_id, name, declaration_hash) WHERE app_id IS NOT NULL DO UPDATE SET
                 diagnostics = excluded.diagnostics, resolved_locals = excluded.resolved_locals
             RETURNING id",
        )
        .bind(&name)
        .bind(&blok.description)
        .bind(stored_components(blok.components.as_deref()))
        .bind(dump_diagnostics(&found))
        .bind(agent.user)
        .bind(agent.organization)
        .bind(catalog.id)
        .bind(blok.context.value())
        .bind(agent.app)
        .bind(hash_action_blok(blok))
        .bind(blok.vocab as i16)
        .bind(serde_json::to_value(&blok.catalogs).expect("blok catalogs serialize"))
        .bind(stored_list(&blok.locals))
        .bind(stored_list(&checked.scope.locals))
        .bind(stored_demo(blok.demo.as_ref()))
        .bind(stored_list(&blok.queries))
        .bind(stored_list(&blok.subscriptions))
        .fetch_one(&mut *conn)
        .await?;
        sqlx::query(
            "INSERT INTO facade_actionblok (action_id, context, blok_id) VALUES ($1, $2, $3)
             ON CONFLICT (action_id, context) DO UPDATE SET blok_id = excluded.blok_id, updated_at = now()",
        )
        .bind(action)
        .bind(blok.context.value())
        .bind(id)
        .execute(&mut *conn)
        .await?;
        contexts.push(blok.context.value());
        diagnostics.extend(found);
    }
    sqlx::query("DELETE FROM facade_actionblok WHERE action_id = $1 AND NOT (context = ANY($2))")
        .bind(action)
        .bind(&contexts)
        .execute(&mut *conn)
        .await?;
    gc_action_bloks(conn, &held).await?;
    Ok(diagnostics)
}

/// An implementation's declared asks and informs, as it declares them now
/// (`ImplementationInteraction`, told apart by `role`): each made as a task blok and checked
/// against its catalog, written as a blok of the agent's app held by implementations only
/// (`held_by_action`: never listed), content-addressed and shared by every implementation of the
/// app declaring the same, and bound to the implementation under its key. A key it no longer
/// declares is let go, and a blok nothing holds or shows any more is collected. Their
/// diagnostics.
pub async fn sync_implementation_interactions(
    conn: &mut PgConnection,
    agent: &AgentIdentity,
    implementation: i64,
    interface: &str,
    asks: &[AskBlokInputModel],
    informs: &[InformBlokInputModel],
) -> Result<Vec<Diagnostic>, Refusal> {
    let held: Vec<i64> = sqlx::query_scalar(
        "SELECT blok_id FROM facade_implementationinteraction WHERE implementation_id = $1",
    )
    .bind(implementation)
    .fetch_all(&mut *conn)
    .await?;
    let mut diagnostics = vec![];
    let mut keys: Vec<String> = vec![];
    // An inform is stored as an ask that answers nothing; its role tells them apart.
    let declared = asks
        .iter()
        .map(|ask| (crate::task_bloks::ASK, ask.clone()))
        .chain(
            informs
                .iter()
                .map(|inform| (crate::task_bloks::INFORM, inform.as_ask())),
        );
    for (role, ask) in declared {
        let what = role.to_lowercase();
        let mut blok = ask.to_blok();
        blok.validate_as(rekuest_core::inputs::TASK_BLOK_KIND)
            .map_err(|e| {
                Refusal::Invalid(format!(
                    "The {what} {:?} of {interface:?} does not hold: {e}",
                    ask.key
                ))
            })?;
        let catalog_name = blok
            .module
            .as_deref()
            .filter(|c| !c.is_empty())
            .unwrap_or(STANDARD_CATALOG_NAME);
        let catalog = Catalog::get_or_create(conn, catalog_name, agent.organization).await?;
        let checked = validate_blok_against_catalog(
            &catalog,
            blok.components.as_deref(),
            &blok.scope(rekuest_core::inputs::TASK_BLOK_KIND),
        )?;
        let mut found: Vec<Diagnostic> = module_unknown(conn, &catalog, agent.organization)
            .await?
            .into_iter()
            .collect();
        found.extend(checked.diagnostics);
        check_blok_arg_descriptor_keys(conn, &ask.key, &ask.args).await?;
        // Its name only says whose it is: it is looked up by its implementation and key.
        let name = format!("{interface}:{}", ask.key);
        let (id, inserted): (i64, bool) = sqlx::query_as(
            "INSERT INTO facade_blok (name, description, components, diagnostics, creator_id, organization_id,
                                      module_id, context, app_id, declaration_hash, held_by_action, args, returns,
                                      vocab, catalogs, locals, resolved_locals, demo, queries, subscriptions)
             VALUES ($1, $2, $3, $4, $5, $6, $7, 'HTML', $8, $9, true, $10, $11, $12, $13, $14, $15, $16, $17, $18)
             ON CONFLICT (organization_id, app_id, name, declaration_hash) WHERE app_id IS NOT NULL DO UPDATE SET
                 diagnostics = excluded.diagnostics, resolved_locals = excluded.resolved_locals
             RETURNING id, (xmax = 0)",
        )
        .bind(&name)
        .bind(&ask.description)
        .bind(stored_components(blok.components.as_deref()))
        .bind(dump_diagnostics(&found))
        .bind(agent.user)
        .bind(agent.organization)
        .bind(catalog.id)
        .bind(agent.app)
        .bind(hash_interaction_blok(role, &ask))
        .bind(serde_json::to_value(&ask.args).expect("interaction args serialize"))
        .bind(serde_json::to_value(&ask.returns).expect("interaction returns serialize"))
        .bind(ask.vocab as i16)
        .bind(serde_json::to_value(&blok.catalogs).expect("blok catalogs serialize"))
        .bind(stored_list(&blok.locals))
        .bind(stored_list(&checked.scope.locals))
        .bind(stored_demo(blok.demo.as_ref()))
        .bind(stored_list(&blok.queries))
        .bind(stored_list(&blok.subscriptions))
        .fetch_one(&mut *conn)
        .await?;
        if inserted {
            // The template's content is its hash, args included: its rows never change after this.
            create_ports_level_by_level(conn, &ask.args, "blok_id", id, "facade_blokargport")
                .await?;
        }
        sync_blok_dependencies(conn, id, &blok.dependencies, true).await?;
        sqlx::query(
            "INSERT INTO facade_implementationinteraction (implementation_id, key, role, description, blok_id, created_at, updated_at)
             VALUES ($1, $2, $3, $4, $5, now(), now())
             ON CONFLICT (implementation_id, key) DO UPDATE SET blok_id = excluded.blok_id,
                 role = excluded.role, description = excluded.description, updated_at = now()",
        )
        .bind(implementation)
        .bind(&ask.key)
        .bind(role)
        .bind(&ask.description)
        .bind(id)
        .execute(&mut *conn)
        .await?;
        keys.push(ask.key.clone());
        diagnostics.extend(found);
    }
    sqlx::query(
        "DELETE FROM facade_implementationinteraction WHERE implementation_id = $1 AND NOT (key = ANY($2))",
    )
    .bind(implementation)
    .bind(&keys)
    .execute(&mut *conn)
    .await?;
    gc_action_bloks(conn, &held).await?;
    Ok(diagnostics)
}

/// Reconcile an agent's declared implementations, states, locks and bloks in one transaction
/// (`implement_agent`): either the whole declared set lands or none of it. The caller commits,
/// then publishes [`Implemented::on_commit`].
pub async fn implement_agent(
    conn: &mut PgConnection,
    agent: i64,
    payload: &ImplementAgentInputModel,
) -> Result<Implemented, Refusal> {
    let (organization, user, client_id, release, app, app_identifier): (
        i64,
        i64,
        String,
        i64,
        i64,
        String,
    ) = sqlx::query_as(
        "SELECT a.organization_id, a.user_id, c.client_id, c.release_id, r.app_id, app.identifier
           FROM facade_agent a
           JOIN authentikate_client c ON c.id = a.client_id
           JOIN authentikate_release r ON r.id = c.release_id
           JOIN authentikate_app app ON app.id = r.app_id
          WHERE a.id = $1",
    )
    .bind(agent)
    .fetch_one(&mut *conn)
    .await?;

    // Before any row is read or written: the org-shared rows are written interleaved with the
    // agent's own, so concurrent registrations of one fleet would deadlock without it.
    lock_organization(conn, organization).await?;

    let identity = AgentIdentity {
        id: agent,
        app,
        user,
        organization,
    };
    let mut on_commit = OnCommit::default();
    let hash = payload
        .hash
        .clone()
        .filter(|h| !h.is_empty())
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    // A register without a description keeps the agent's. `name` is the declared name (the
    // client id when none is declared); a user's rename lives in `display_name`, which no
    // registration writes.
    sqlx::query(
        "UPDATE facade_agent SET name = $2, app_id = $3, release_id = $4, hash = $5, description = coalesce($6, description)
          WHERE id = $1",
    )
    .bind(agent)
    .bind(payload.name.as_deref().filter(|n| !n.is_empty()).unwrap_or(&client_id))
    .bind(app)
    .bind(release)
    .bind(&hash)
    .bind(&payload.description)
    .execute(&mut *conn)
    .await?;
    on_commit.push(Signal::AgentSaved {
        id: agent,
        created: false,
    });

    for lock in payload.locks.iter().flatten() {
        sqlx::query(
            "INSERT INTO facade_lock (key, description, agent_id) VALUES ($2, $3, $1)
             ON CONFLICT (agent_id, key) DO UPDATE SET description = excluded.description, updated_at = now()",
        )
        .bind(agent)
        .bind(&lock.key)
        .bind(&lock.definition.description)
        .execute(&mut *conn)
        .await?;
    }

    let mut declared: Vec<_> = payload.implementations.iter().flatten().collect();
    declared.sort_by(|a, b| {
        (&a.definition.key, &a.definition.version).cmp(&(&b.definition.key, &b.definition.version))
    });
    // Each implementation's logo, by blok key, in the order the implementations are written.
    let logos: Vec<Option<String>> = declared.iter().map(|i| i.logo.clone()).collect();
    let mut diagnostics = vec![];
    let mut created_implementations = vec![];
    if !declared.is_empty() {
        let wanted: Vec<(String, String)> = declared
            .iter()
            .map(|i| (i.definition.key.clone(), i.definition.version.clone()))
            .collect();
        let mut prefetch = Prefetch::load(conn, &identity, &wanted).await?;
        for implementation in declared {
            let registered = create_implementation(
                conn,
                implementation,
                &identity,
                &mut prefetch,
                &mut on_commit,
            )
            .await?;
            created_implementations.push(registered.id);
            diagnostics.extend(registered.diagnostics);
        }
    }

    let mut created_states = vec![];
    for state in payload.states.iter().flatten() {
        let id = register_state(conn, &identity, &app_identifier, state).await?;
        created_states.push(id);
        on_commit.push(Signal::StateSaved { id });
    }

    // Reap what the agent no longer declares; implementations still running tasks are kept, and
    // so are the wrappers deployed onto it (`createHigherOrderImplementation`), which it never
    // declares.
    let stale_states: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM facade_state WHERE agent_id = $1 AND NOT (id = ANY($2))",
    )
    .bind(agent)
    .bind(&created_states)
    .fetch_all(&mut *conn)
    .await?;
    delete_states(conn, &stale_states).await?;
    let stale: Vec<(i64, bool)> = sqlx::query_as(
        "SELECT i.id, EXISTS (SELECT 1 FROM facade_task t WHERE t.implementation_id = i.id AND NOT t.is_done)
           FROM facade_implementation i
          WHERE i.agent_id = $1 AND NOT (i.id = ANY($2)) AND i.higher_order_for_id IS NULL ORDER BY i.id",
    )
    .bind(agent)
    .bind(&created_implementations)
    .fetch_all(&mut *conn)
    .await?;
    let live = stale.iter().filter(|(_, live)| *live).count();
    if live > 0 {
        tracing::warn!(
            "Keeping {live} undeclared implementation(s) for agent {agent}: still running tasks."
        );
    }
    let reaped: Vec<i64> = stale
        .iter()
        .filter(|(_, live)| !live)
        .map(|(id, _)| *id)
        .collect();
    for deleted in delete_implementations(conn, &reaped).await? {
        on_commit.push(Signal::ImplementationDeleted {
            id: deleted.id,
            agent_id: deleted.agent_id,
        });
    }

    // The templates the agent's slots hold before: the ones it re-points or drops may be left
    // without a holder.
    let held: Vec<i64> =
        sqlx::query_scalar("SELECT blok_id FROM facade_materializedblok WHERE declared_by_id = $1")
            .bind(agent)
            .fetch_all(&mut *conn)
            .await?;
    // A local passed whole to one of the agent's own actions takes that action's full arg port,
    // as the SDK resolves it (`self_arg_ports`): a demand's arg match has no choices or units.
    let self_arg_ports: ArgPorts = HashMap::from([(
        "self".to_owned(),
        payload
            .implementations
            .iter()
            .flatten()
            .map(|i| (i.interface.clone(), i.definition.args.clone()))
            .collect(),
    )]);
    let mut declared_bloks = vec![];
    for blok in payload.bloks.iter().flatten() {
        diagnostics.extend(register_blok(conn, &identity, blok, &self_arg_ports).await?);
        declared_bloks.push(blok.key.clone());
    }
    let dropped: Vec<i64> = sqlx::query_scalar(
        "SELECT id FROM facade_materializedblok WHERE declared_by_id = $1 AND NOT (name = ANY($2))",
    )
    .bind(agent)
    .bind(&declared_bloks)
    .fetch_all(&mut *conn)
    .await?;
    delete_materialized_bloks(conn, &dropped).await?;
    gc_provided_bloks(conn, &held).await?;

    // A logo is a blok this registration has just written: link each implementation to the
    // template its agent's slot holds (or to none, when it no longer names one).
    for (implementation, logo) in created_implementations.iter().zip(&logos) {
        sqlx::query(
            "UPDATE facade_implementation
                SET logo_id = (SELECT blok_id FROM facade_materializedblok WHERE declared_by_id = $2 AND name = $3)
              WHERE id = $1",
        )
        .bind(implementation)
        .bind(agent)
        .bind(logo.as_deref())
        .execute(&mut *conn)
        .await?;
    }
    // Each action keeps the logo of its implementation most recently registered with one
    // (`Action.latest_logo`): what clients draw it by.
    sqlx::query(
        "UPDATE facade_action a
            SET latest_logo_id = (SELECT i.logo_id FROM facade_implementation i
                                   WHERE i.action_id = a.id AND i.logo_id IS NOT NULL
                                   ORDER BY i.updated_at DESC, i.id DESC LIMIT 1)
          WHERE a.id IN (SELECT action_id FROM facade_implementation WHERE id = ANY($1))",
    )
    .bind(&created_implementations)
    .execute(&mut *conn)
    .await?;

    // The names the agent gives its files. The files themselves are the organization's
    // `facade_asset` rows of the same ref, which the agent uploads after it registered,
    // or, for a file it only names a source for, an admin does.
    let mut declared_assets = vec![];
    for asset in payload.assets.iter().flatten() {
        sqlx::query(
            "INSERT INTO facade_agentasset (agent_id, name, kind, ref, size, source) VALUES ($1, $2, $3, $4, $5, $6)
             ON CONFLICT (agent_id, name) DO UPDATE SET kind = excluded.kind, ref = excluded.ref,
                                                        size = excluded.size, source = excluded.source,
                                                        updated_at = now()",
        )
        .bind(agent)
        .bind(&asset.name)
        .bind(asset.kind.value())
        .bind(&asset.reference)
        .bind(asset.size)
        .bind(&asset.source)
        .execute(&mut *conn)
        .await?;
        declared_assets.push(asset.name.clone());
    }
    sqlx::query("DELETE FROM facade_agentasset WHERE agent_id = $1 AND NOT (name = ANY($2))")
        .bind(agent)
        .bind(&declared_assets)
        .execute(&mut *conn)
        .await?;

    Ok(Implemented {
        hash,
        diagnostics,
        on_commit,
    })
}
